"""Write-side helpers — used by ingest paths to UPSERT data into Postgres.

All functions are idempotent (ON CONFLICT DO UPDATE on primary keys) so they're
safe to call multiple times with overlapping date ranges (NAV daily incremental,
benchmark refresh, T-bill re-walk).
"""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime
from typing import Iterable, Sequence

import polars as pl
import psycopg
from psycopg import sql

from mfs.db.connection import connect
from mfs.utils.logging import get_logger

log = get_logger(__name__)


def _copy_upsert(
    conn: psycopg.Connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[tuple],
    pk: Sequence[str],
) -> int:
    """COPY rows into a uniquely-named temp table, then UPSERT into `table`."""
    rows = list(rows)
    if not rows:
        return 0
    tmp = f"_w_{table}_{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE TEMP TABLE {} (LIKE {} INCLUDING DEFAULTS)").format(
                sql.Identifier(tmp), sql.Identifier(table)
            )
        )
        try:
            col_idents = sql.SQL(",").join(sql.Identifier(c) for c in columns)
            with cur.copy(
                sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(tmp), col_idents)
            ) as cp:
                for row in rows:
                    cp.write_row(row)
            update_cols = [c for c in columns if c not in pk]
            if update_cols:
                set_clause = sql.SQL(", ").join(
                    sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
                    for c in update_cols
                )
                conflict = sql.SQL("DO UPDATE SET ") + set_clause
            else:
                conflict = sql.SQL("DO NOTHING")
            cur.execute(
                sql.SQL(
                    "INSERT INTO {table} ({cols}) SELECT {cols} FROM {tmp} "
                    "ON CONFLICT ({pk}) {conflict}"
                ).format(
                    table=sql.Identifier(table),
                    cols=col_idents,
                    tmp=sql.Identifier(tmp),
                    pk=sql.SQL(",").join(sql.Identifier(c) for c in pk),
                    conflict=conflict,
                )
            )
            affected = cur.rowcount
        finally:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(tmp)))
    return affected


def upsert_nav_daily(df: pl.DataFrame, *, refresh_log_returns: bool = True) -> int:
    """Upsert a chunk of NAV rows. Expected columns:
    scheme_code, isin_growth, isin_idcw, scheme_name, nav, nav_date, amc_name,
    amfi_category.

    C4/B10: the fund_log_returns cache refresh runs ON THE SAME connection /
    transaction as the NAV upsert — a kill between the two can no longer leave
    nav_daily ahead of the derived log-return cache (the old two-transaction
    crash window). The refresh is incremental: per scheme, only dates >= the
    earliest nav_date in this batch are recomputed.

    ``refresh_log_returns=False`` suppresses the refresh — the bulk-backfill
    path uses it per window and runs one ``refresh_fund_log_returns_all()``
    after the final window instead.
    """
    if df.is_empty():
        return 0
    cols = ["scheme_code", "nav_date", "nav", "isin_growth", "isin_idcw",
            "scheme_name", "amc_name", "amfi_category"]
    out = df.select(cols)
    with connect() as c:
        n = _copy_upsert(c, "nav_daily", cols, out.iter_rows(),
                         pk=("scheme_code", "nav_date"))
        if refresh_log_returns:
            watermarks = {
                r["scheme_code"]: r["min_nav_date"]
                for r in out.group_by("scheme_code")
                .agg(pl.col("nav_date").min().alias("min_nav_date"))
                .iter_rows(named=True)
            }
            refresh_fund_log_returns(watermarks, conn=c)
    return n


def upsert_benchmark_daily(df: pl.DataFrame) -> int:
    """Upsert a benchmark series. Expected columns: ticker, date, close;
    optional is_synthetic (defaults False)."""
    if df.is_empty():
        return 0
    if "is_synthetic" not in df.columns:
        df = df.with_columns(pl.lit(False).alias("is_synthetic"))
    else:
        df = df.with_columns(pl.col("is_synthetic").fill_null(False))
    cols = ["ticker", "date", "close", "is_synthetic"]
    out = df.select(cols)
    with connect() as c:
        return _copy_upsert(c, "benchmark_daily", cols, out.iter_rows(),
                            pk=("ticker", "date"))


def upsert_risk_free_daily(df: pl.DataFrame) -> int:
    """Replace risk_free_daily atomically. Expected columns: date, rate_annual,
    rate_daily.

    Risk-free is a recompute-from-source pattern (forward-fill of sparse weekly
    observations across daily calendar), so the cleanest approach is
    truncate-and-load rather than per-row UPSERT.
    """
    if df.is_empty():
        return 0
    cols = ["date", "rate_annual", "rate_daily"]
    out = df.select(cols)
    with connect() as c:
        with c.cursor() as cur:
            cur.execute("TRUNCATE risk_free_daily")
            with cur.copy("COPY risk_free_daily (date, rate_annual, rate_daily) FROM STDIN") as cp:
                for row in out.iter_rows():
                    cp.write_row(row)
        return out.height


# Fraction of currently-active scheme_master rows a single sync may deactivate.
# A truncated/partial NAVAll page looks exactly like a mass departure, so a
# sync above this fraction must halt the run, not deactivate the universe
# (fail-fast invariant).
SCHEME_MASTER_MAX_DEACTIVATION_FRAC = 0.20


def plan_scheme_master_sync(
    snapshot_codes: set[str], active_codes: set[str],
) -> list[str]:
    """Pure diff for the scheme_master sync: the currently-active scheme codes
    absent from today's AMFI snapshot, i.e. the rows to flip is_active=false.

    Raises RuntimeError when the diff would deactivate more than
    ``SCHEME_MASTER_MAX_DEACTIVATION_FRAC`` of the active rows — a partial
    NAVAll response must halt the sync, never mass-deactivate."""
    to_deactivate = sorted(active_codes - snapshot_codes)
    if active_codes:
        frac = len(to_deactivate) / len(active_codes)
        if frac > SCHEME_MASTER_MAX_DEACTIVATION_FRAC:
            raise RuntimeError(
                f"scheme_master sync would deactivate {len(to_deactivate)} of "
                f"{len(active_codes)} active schemes ({frac:.1%} > "
                f"{SCHEME_MASTER_MAX_DEACTIVATION_FRAC:.0%}); refusing — "
                "today's AMFI NAVAll snapshot is likely partial."
            )
    return to_deactivate


def sync_scheme_master(df: pl.DataFrame) -> int:
    """Diff-sync scheme_master against today's AMFI snapshot — never TRUNCATE,
    never DELETE (survivorship containment).

    In ONE transaction:
      1. Every snapshot row UPSERTs (snapshot wins on name/category/benchmark)
         with is_active=true and departed_at=NULL — a re-appearing scheme
         flips back to active and its departure marker clears.
      2. Currently-active rows whose scheme_code is absent from the snapshot
         flip to is_active=false with departed_at=today. Their last_seen_date
         and every other column are preserved; already-inactive rows keep
         their original departed_at.

    Raises RuntimeError BEFORE any write when the diff would deactivate >20%
    of active rows — see ``plan_scheme_master_sync``."""
    if df.is_empty():
        return 0
    cols = [
        "scheme_code", "isin_growth", "isin_idcw", "scheme_name", "amc_name",
        "amc_code", "plan_type", "option_type", "amfi_category",
        "canonical_category", "benchmark_ticker", "inception_date",
        "base_fund_id", "is_active", "last_seen_date", "departed_at",
    ]
    out = df.with_columns(
        pl.lit(True).alias("is_active"),
        pl.lit(None, dtype=pl.Date).alias("departed_at"),
    ).select(cols)
    snapshot_codes = set(out["scheme_code"].to_list())
    with connect() as c:
        active_rows = c.execute(
            "SELECT scheme_code FROM scheme_master WHERE is_active"
        ).fetchall()
        to_deactivate = plan_scheme_master_sync(
            snapshot_codes, {r[0] for r in active_rows}
        )
        n = _copy_upsert(c, "scheme_master", cols, out.iter_rows(),
                         pk=("scheme_code",))
        if to_deactivate:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE scheme_master SET is_active = FALSE, departed_at = %s "
                    "WHERE scheme_code = ANY(%s)",
                    (date.today(), to_deactivate),
                )
    log.info("scheme_master.synced", upserted=n, deactivated=len(to_deactivate))
    return n


# All scheme_master columns, mirrored into scheme_master_history per run.
_SCHEME_MASTER_HISTORY_COLS = [
    "scheme_code", "isin_growth", "isin_idcw", "scheme_name", "amc_name",
    "amc_code", "plan_type", "option_type", "amfi_category",
    "canonical_category", "benchmark_ticker", "inception_date",
    "base_fund_id", "is_active", "last_seen_date", "departed_at",
]


def snapshot_scheme_master(snapshot_date: date) -> int:
    """D2 point-in-time snapshot: copy the CURRENT post-sync scheme_master
    state (including inactive/departed rows — they are part of the
    point-in-time universe) into scheme_master_history under ``snapshot_date``.

    Server-side INSERT ... SELECT, so the 14k-row table never round-trips
    through the process. Re-running the same date replaces the partition —
    DELETE + insert in ONE transaction, so a same-day re-build is idempotent
    and a kill between the two statements rolls both back."""
    cols = sql.SQL(", ").join(sql.Identifier(c) for c in _SCHEME_MASTER_HISTORY_COLS)
    with connect() as c:
        with c.cursor() as cur:
            cur.execute(
                "DELETE FROM scheme_master_history WHERE snapshot_date = %s",
                (snapshot_date,),
            )
            cur.execute(
                sql.SQL(
                    "INSERT INTO scheme_master_history (snapshot_date, {cols}) "
                    "SELECT %s, {cols} FROM scheme_master"
                ).format(cols=cols),
                (snapshot_date,),
            )
            n = cur.rowcount
    log.info("scheme_master_history.snapshotted", snapshot_date=str(snapshot_date), rows=n)
    return n


# rank_history columns — must stay identical to the schema.sql definition and
# to shortlist.RANK_HISTORY_COLS (the frame builder).
_RANK_HISTORY_COLS = [
    "as_of_date", "stage", "scheme_code", "canonical_category",
    "composite_score", "composite_score_stage1", "rank_in_category",
    "z_ret_3y_median", "z_ret_3y_p25", "z_ret_5y_median", "z_ret_5y_p25",
    "z_alpha_3y_annualized", "z_sortino_3y", "z_info_ratio_3y",
    "z_capture_efficiency", "z_active_share_median_1y", "z_style_drift_3y",
    "ptr_latest", "aum_impact_cost_days",
    "included", "exclusion_reason", "run_id",
]


def persist_rank_history(df: pl.DataFrame, as_of: date) -> int:
    """D2 point-in-time snapshot: replace the rank_history partition for
    ``as_of`` atomically with this run's stage 1/2/3 outcomes.

    Expected columns: any subset of ``_RANK_HISTORY_COLS`` including at least
    stage / scheme_code / included (``shortlist.build_rank_history`` produces
    the full set); missing columns are written as NULL. DELETE + insert run in
    ONE transaction so a re-run of the same as_of replaces the partition and a
    kill between the two statements rolls both back."""
    if df.is_empty():
        return 0
    if "as_of_date" not in df.columns:
        df = df.with_columns(pl.lit(as_of).cast(pl.Date).alias("as_of_date"))
    for c in _RANK_HISTORY_COLS:
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Utf8).alias(c))
    out = df.select(_RANK_HISTORY_COLS)
    with connect() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM rank_history WHERE as_of_date = %s", (as_of,))
        n = _copy_upsert(c, "rank_history", _RANK_HISTORY_COLS, out.iter_rows(),
                         pk=("as_of_date", "stage", "scheme_code"))
    log.info("rank_history.persisted", as_of=str(as_of), rows=n)
    return n


_COMPUTED_METRICS_COLS = [
    "as_of_date", "scheme_code", "canonical_category", "benchmark_ticker",
    "ret_3y_median", "ret_3y_p25", "ret_5y_median", "ret_5y_p25",
    "alpha_3y_annualized", "alpha_3y_tstat", "alpha_confidence", "sortino_3y",
    "info_ratio_3y",
    "capture_up", "capture_down", "capture_efficiency",
    "r_squared_3y", "beta_3y",
    # E2 drawdown display columns (never composite-weighted).
    "max_dd_3y_pct", "max_dd_3y_recovery_days",
    "max_dd_5y_pct", "max_dd_5y_recovery_days",
    # Phase 2 columns (nullable; populated incrementally across 2.0-2.3).
    "beta_3y_std", "r_squared_3y_mean", "style_drift_3y",
    "active_share_median_1y", "ptr_latest", "aum_impact_cost_days",
    "adv_unresolved_pct",
    "data_quality_flag", "computed_at", "pipeline_version",
]

_PHASE2_COLS = (
    "style_drift_3y",
    "active_share_median_1y",
    "ptr_latest",
    "aum_impact_cost_days",
    "adv_unresolved_pct",
)


def upsert_computed_metrics(df: pl.DataFrame, as_of: date) -> int:
    """Replace the computed_metrics partition for `as_of_date` atomically.

    Back-compat for callers that produce a full Phase 1 + Phase 2 row in one
    shot (the legacy ``orchestrator.run`` wrapper). New code should call
    ``upsert_computed_metrics_phase1`` + ``update_computed_metrics_phase2``.
    """
    if df.is_empty():
        return 0
    if "as_of_date" not in df.columns:
        df = df.with_columns(pl.lit(as_of).cast(pl.Date).alias("as_of_date"))
    for c in _COMPUTED_METRICS_COLS:
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))
    out = df.select(_COMPUTED_METRICS_COLS)
    with connect() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM computed_metrics WHERE as_of_date = %s", (as_of,))
        return _copy_upsert(c, "computed_metrics", _COMPUTED_METRICS_COLS, out.iter_rows(),
                            pk=("as_of_date", "scheme_code"))


def upsert_computed_metrics_phase1(df: pl.DataFrame, as_of: date) -> int:
    """Write the Phase 1 columns to ``computed_metrics`` for ``as_of``.

    Truncates the partition first (Phase 1 always runs on the full eligible
    set) and writes Phase 2 columns as NULL — they'll be filled later by
    ``update_computed_metrics_phase2`` on the subset that Stage 1 promotes
    to the Stage 2 pool.
    """
    if df.is_empty():
        return 0
    if "as_of_date" not in df.columns:
        df = df.with_columns(pl.lit(as_of).cast(pl.Date).alias("as_of_date"))
    for c in _COMPUTED_METRICS_COLS:
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))
    out = df.select(_COMPUTED_METRICS_COLS)
    with connect() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM computed_metrics WHERE as_of_date = %s", (as_of,))
        return _copy_upsert(c, "computed_metrics", _COMPUTED_METRICS_COLS, out.iter_rows(),
                            pk=("as_of_date", "scheme_code"))


def update_computed_metrics_phase2(df: pl.DataFrame, as_of: date) -> int:
    """UPDATE the Phase 2 columns on existing rows in ``computed_metrics``.

    Expected columns: ``scheme_code`` + any subset of the Phase 2 columns
    (``style_drift_3y``, ``active_share_median_1y``, ``ptr_latest``,
    ``aum_impact_cost_days``, ``adv_unresolved_pct``). Missing rows are
    skipped — the caller is expected to have run Phase 1 first.
    """
    if df.is_empty():
        return 0
    present_phase2 = [c for c in _PHASE2_COLS if c in df.columns]
    if not present_phase2:
        return 0
    set_clause = sql.SQL(", ").join(
        sql.SQL("{c} = %s").format(c=sql.Identifier(col)) for col in present_phase2
    )
    affected = 0
    with connect() as c:
        with c.cursor() as cur:
            for r in df.iter_rows(named=True):
                params = [r.get(col) for col in present_phase2] + [as_of, r["scheme_code"]]
                cur.execute(
                    sql.SQL(
                        "UPDATE computed_metrics SET {set_clause} "
                        "WHERE as_of_date = %s AND scheme_code = %s"
                    ).format(set_clause=set_clause),
                    params,
                )
                affected += cur.rowcount
    return affected


# ---------------------------------------------------------------------------
# Phase 2.2: holdings / index constituents / PTR
# ---------------------------------------------------------------------------


def upsert_holdings(df: pl.DataFrame, conn: psycopg.Connection | None = None) -> int:
    """Upsert holdings_monthly. Expected columns: scheme_code, security_name,
    as_of_month, weight_pct, isin (optional), instrument_type, source_amc,
    computed_at. Primary key (scheme_code, security_name, as_of_month) —
    ISIN is nullable since factsheet PDFs don't print it.

    When ``conn`` is supplied the upsert runs on that connection (no commit), so
    the caller can wrap a preceding ``DELETE`` and this upsert in ONE
    transaction — a kill between them must not leave the (source_amc, month)
    partition empty. With ``conn=None`` it opens its own committed connection.
    """
    if df.is_empty():
        return 0
    cols = [
        "scheme_code", "security_name", "as_of_month", "weight_pct",
        "isin", "instrument_type", "source_amc", "computed_at",
    ]
    # Ensure ISIN column exists (some adapters won't provide it).
    if "isin" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Utf8).alias("isin"))
    out = df.select(cols)
    pk = ("scheme_code", "security_name", "as_of_month")
    if conn is not None:
        return _copy_upsert(conn, "holdings_monthly", cols, out.iter_rows(), pk=pk)
    with connect() as c:
        return _copy_upsert(c, "holdings_monthly", cols, out.iter_rows(), pk=pk)


def upsert_index_constituents(df: pl.DataFrame) -> int:
    """Upsert index_constituents_monthly. Expected columns: ticker, isin,
    as_of_month, weight_pct, security_name."""
    if df.is_empty():
        return 0
    cols = ["ticker", "isin", "as_of_month", "weight_pct", "security_name"]
    out = df.select(cols)
    with connect() as c:
        return _copy_upsert(
            c, "index_constituents_monthly", cols, out.iter_rows(),
            pk=("ticker", "isin", "as_of_month"),
        )


def upsert_portfolio_turnover(df: pl.DataFrame, conn: psycopg.Connection | None = None) -> int:
    """Upsert portfolio_turnover_monthly. Expected columns: scheme_code,
    as_of_month, ptr (as a fraction), source_amc, computed_at.

    When ``conn`` is supplied the upsert runs on that connection (no commit) so
    a preceding ``DELETE`` + this upsert commit atomically — a kill between them
    must not leave the (source_amc, month) PTR rows wiped.
    """
    if df.is_empty():
        return 0
    cols = ["scheme_code", "as_of_month", "ptr", "source_amc", "computed_at"]
    out = df.select(cols)
    pk = ("scheme_code", "as_of_month")
    if conn is not None:
        return _copy_upsert(conn, "portfolio_turnover_monthly", cols, out.iter_rows(), pk=pk)
    with connect() as c:
        return _copy_upsert(c, "portfolio_turnover_monthly", cols, out.iter_rows(), pk=pk)


def upsert_scheme_ter(df: pl.DataFrame) -> int:
    """Upsert scheme_ter_monthly. Expected columns: scheme_code, as_of_month,
    ter_direct_pct, source, computed_at. PK (scheme_code, as_of_month). The DB
    CHECK rejects any ter_direct_pct outside 0 < ter <= 3.0 (no-half-data)."""
    if df.is_empty():
        return 0
    cols = ["scheme_code", "as_of_month", "ter_direct_pct", "source", "computed_at"]
    out = df.select(cols)
    pk = ("scheme_code", "as_of_month")
    with connect() as c:
        return _copy_upsert(c, "scheme_ter_monthly", cols, out.iter_rows(), pk=pk)


# ---------------------------------------------------------------------------
# Phase 2.3: stock ADV / AUM
# ---------------------------------------------------------------------------


def upsert_stock_adv(df: pl.DataFrame) -> int:
    """Upsert NSE bhavcopy rows. Expected columns: isin, symbol, date,
    close, total_traded_value."""
    if df.is_empty():
        return 0
    cols = ["isin", "symbol", "date", "close", "total_traded_value"]
    if "symbol" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Utf8).alias("symbol"))
    if "close" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("close"))
    out = df.select(cols)
    with connect() as c:
        return _copy_upsert(
            c, "stock_adv_daily", cols, out.iter_rows(),
            pk=("isin", "date"),
        )


def upsert_scheme_aum(df: pl.DataFrame) -> int:
    """Upsert scheme_aum_monthly. Expected columns: scheme_code, as_of_month,
    aum_crore, source_amc, computed_at."""
    if df.is_empty():
        return 0
    cols = ["scheme_code", "as_of_month", "aum_crore", "source_amc", "computed_at"]
    out = df.select(cols)
    with connect() as c:
        return _copy_upsert(
            c, "scheme_aum_monthly", cols, out.iter_rows(),
            pk=("scheme_code", "as_of_month"),
        )


# ---------------------------------------------------------------------------
# fund_log_returns cache
# ---------------------------------------------------------------------------


def refresh_fund_log_returns(
    watermarks: dict[str, date],
    conn: psycopg.Connection | None = None,
) -> int:
    """Incrementally recompute fund_log_returns from per-scheme NAV watermarks.

    ``watermarks`` maps scheme_code → the earliest nav_date present in the
    triggering NAV batch. Per scheme, cached rows with date >= watermark are
    deleted and recomputed; rows before the watermark are untouched —
    a batch can only change NAVs it contains, and the day-of-watermark return
    needs exactly one preceding NAV, supplied by a per-scheme lookback to the
    latest positive-NAV date strictly before the watermark.

    log_return_t = ln(nav_t) - ln(nav_{t-1}); the first row per scheme has no
    prior NAV and is filtered via WHERE log_return IS NOT NULL.

    When ``conn`` is supplied the refresh runs on that connection without
    committing — ``upsert_nav_daily`` passes its own connection so the NAV
    upsert and this refresh commit in ONE transaction (closes the C4/B10
    two-transaction crash window). With ``conn=None`` it opens its own
    committed connection.
    """
    if not watermarks:
        return 0
    if conn is not None:
        return _refresh_fund_log_returns_on(conn, watermarks)
    with connect() as c:
        return _refresh_fund_log_returns_on(c, watermarks)


def _refresh_fund_log_returns_on(
    conn: psycopg.Connection, watermarks: dict[str, date],
) -> int:
    """Set-based watermark refresh on an existing connection (no commit)."""
    tmp = f"_w_flr_wm_{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "CREATE TEMP TABLE {} "
                "(scheme_code TEXT PRIMARY KEY, min_date DATE NOT NULL)"
            ).format(sql.Identifier(tmp))
        )
        try:
            with cur.copy(
                sql.SQL("COPY {} (scheme_code, min_date) FROM STDIN").format(
                    sql.Identifier(tmp)
                )
            ) as cp:
                for code, min_date in watermarks.items():
                    cp.write_row((code, min_date))
            cur.execute(
                sql.SQL(
                    "DELETE FROM fund_log_returns f USING {} t "
                    "WHERE f.scheme_code = t.scheme_code AND f.date >= t.min_date"
                ).format(sql.Identifier(tmp))
            )
            cur.execute(
                sql.SQL(
                    """
                    WITH lb AS (
                        SELECT t.scheme_code,
                               t.min_date,
                               COALESCE(
                                   (SELECT MAX(p.nav_date) FROM nav_daily p
                                    WHERE p.scheme_code = t.scheme_code
                                      AND p.nav_date < t.min_date
                                      AND p.nav > 0),
                                   t.min_date) AS start_date
                        FROM {} t
                    )
                    INSERT INTO fund_log_returns (scheme_code, date, log_return)
                    SELECT scheme_code, date, log_return FROM (
                        SELECT
                            n.scheme_code,
                            n.nav_date AS date,
                            lb.min_date,
                            LN(n.nav) - LN(LAG(n.nav) OVER (
                                PARTITION BY n.scheme_code ORDER BY n.nav_date
                            )) AS log_return
                        FROM nav_daily n
                        JOIN lb ON lb.scheme_code = n.scheme_code
                               AND n.nav_date >= lb.start_date
                        WHERE n.nav > 0
                    ) sub
                    WHERE log_return IS NOT NULL AND date >= min_date
                    """
                ).format(sql.Identifier(tmp))
            )
            total = cur.rowcount
        finally:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(tmp)))
    log.info(
        "fund_log_returns.refreshed", n_schemes=len(watermarks), n_rows=total,
    )
    return total


def refresh_fund_log_returns_all() -> int:
    """Recompute log returns for every scheme from scratch. One-shot use after
    backfill."""
    with connect() as c:
        with c.cursor() as cur:
            cur.execute("TRUNCATE fund_log_returns")
            cur.execute(
                """
                INSERT INTO fund_log_returns (scheme_code, date, log_return)
                SELECT scheme_code, date, log_return FROM (
                    SELECT
                        scheme_code,
                        nav_date AS date,
                        LN(nav) - LN(LAG(nav) OVER (
                            PARTITION BY scheme_code ORDER BY nav_date
                        )) AS log_return
                    FROM nav_daily
                    WHERE nav > 0
                ) sub
                WHERE log_return IS NOT NULL
                """
            )
            n = cur.execute("SELECT COUNT(*) FROM fund_log_returns").fetchone()[0]
    log.info("fund_log_returns.refreshed_all", n=n)
    return n
