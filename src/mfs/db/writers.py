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


def upsert_nav_daily(df: pl.DataFrame) -> int:
    """Upsert a chunk of NAV rows. Expected columns:
    scheme_code, isin_growth, isin_idcw, scheme_name, nav, nav_date, amc_name,
    amfi_category."""
    if df.is_empty():
        return 0
    cols = ["scheme_code", "nav_date", "nav", "isin_growth", "isin_idcw",
            "scheme_name", "amc_name", "amfi_category"]
    out = df.select(cols)
    with connect() as c:
        n = _copy_upsert(c, "nav_daily", cols, out.iter_rows(),
                         pk=("scheme_code", "nav_date"))
        # Refresh log-return cache for the affected schemes
        affected_schemes = set(out["scheme_code"].unique().to_list())
    if affected_schemes:
        refresh_fund_log_returns(affected_schemes)
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


def upsert_scheme_master(df: pl.DataFrame) -> int:
    """Replace scheme_master atomically — it's a full snapshot, not incremental."""
    if df.is_empty():
        return 0
    cols = [
        "scheme_code", "isin_growth", "isin_idcw", "scheme_name", "amc_name",
        "amc_code", "plan_type", "option_type", "amfi_category",
        "canonical_category", "benchmark_ticker", "inception_date",
        "base_fund_id", "is_active", "last_seen_date",
    ]
    out = df.select(cols)
    with connect() as c:
        with c.cursor() as cur:
            cur.execute("TRUNCATE scheme_master")
            col_idents = ", ".join(cols)
            with cur.copy(f"COPY scheme_master ({col_idents}) FROM STDIN") as cp:
                for row in out.iter_rows():
                    cp.write_row(row)
        return out.height


_COMPUTED_METRICS_COLS = [
    "as_of_date", "scheme_code", "canonical_category", "benchmark_ticker",
    "ret_3y_median", "ret_3y_p25", "ret_5y_median", "ret_5y_p25",
    "alpha_3y_annualized", "alpha_3y_tstat", "sortino_3y",
    "info_ratio_3y",
    "capture_up", "capture_down", "capture_efficiency",
    "r_squared_3y", "beta_3y",
    # Phase 2 columns (nullable; populated incrementally across 2.0-2.3).
    "beta_3y_std", "r_squared_3y_mean", "style_drift_3y",
    "active_share_median_1y", "ptr_latest", "aum_impact_cost_days",
    "data_quality_flag", "computed_at", "pipeline_version",
]

_PHASE2_COLS = (
    "style_drift_3y",
    "active_share_median_1y",
    "ptr_latest",
    "aum_impact_cost_days",
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
    ``aum_impact_cost_days``). Missing rows are skipped — the caller is
    expected to have run Phase 1 first.
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


# ---------------------------------------------------------------------------
# Phase 2.3: stock ADV / AUM / stress test
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


def refresh_fund_log_returns(scheme_codes: Iterable[str]) -> int:
    """Recompute daily log returns for given schemes from nav_daily and replace
    their rows in fund_log_returns.

    log_return_t = ln(nav_t) - ln(nav_{t-1}). The first row per scheme has no
    prior NAV — filtered out via WHERE log_return IS NOT NULL inside the SELECT."""
    codes = list(scheme_codes)
    if not codes:
        return 0
    with connect() as c:
        with c.cursor() as cur:
            cur.execute(
                "DELETE FROM fund_log_returns WHERE scheme_code = ANY(%s)", (codes,)
            )
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
                    WHERE scheme_code = ANY(%s) AND nav > 0
                ) sub
                WHERE log_return IS NOT NULL
                """,
                (codes,),
            )
            total = cur.rowcount
    log.info("fund_log_returns.refreshed", n_schemes=len(codes), n_rows=total)
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
