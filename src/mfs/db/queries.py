"""Read-side helpers that translate Postgres tables into the same polars
DataFrame shapes the compute/rank layers used to get from parquet.

Each function is a thin wrapper around `psycopg.connect().execute()` followed by
`pl.from_records(...)`. The signatures match what `alignment.py`, `freshness.py`,
`shortlist.py`, and `orchestrator.py` expect, so callers swap in one line.
"""

from __future__ import annotations

from datetime import date
from typing import NamedTuple

import polars as pl

from mfs.db.connection import connect


def nav_series(scheme_code: str) -> pl.DataFrame:
    """Return columns: date, nav (sorted, unique)."""
    with connect() as c:
        rows = c.execute(
            "SELECT nav_date AS date, nav FROM nav_daily "
            "WHERE scheme_code = %s ORDER BY nav_date",
            (scheme_code,),
        ).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, schema={"date": pl.Date, "nav": pl.Float64}, orient="row")


def benchmark_series(ticker: str) -> pl.DataFrame:
    """Return columns: date, close (sorted)."""
    with connect() as c:
        rows = c.execute(
            "SELECT date, close FROM benchmark_daily "
            "WHERE ticker = %s ORDER BY date",
            (ticker,),
        ).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(
        rows,
        schema={"date": pl.Date, "close": pl.Float64},
        orient="row",
    )


def risk_free_series() -> pl.DataFrame:
    """Return columns: date, rate_daily (sorted)."""
    with connect() as c:
        rows = c.execute(
            "SELECT date, rate_daily FROM risk_free_daily ORDER BY date"
        ).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(
        rows, schema={"date": pl.Date, "rate_daily": pl.Float64}, orient="row"
    )


def master_calendar() -> pl.DataFrame:
    """Return sorted unique dates from NIFTY 50 TRI as the trading calendar."""
    with connect() as c:
        rows = c.execute(
            "SELECT date FROM benchmark_daily WHERE ticker = 'NIFTY 50 TRI' "
            "ORDER BY date"
        ).fetchall()
    if not rows:
        # Fallback: any benchmark
        with connect() as c:
            rows = c.execute(
                "SELECT DISTINCT date FROM benchmark_daily ORDER BY date"
            ).fetchall()
    if not rows:
        return pl.DataFrame({"date": pl.Series([], dtype=pl.Date)})
    return pl.DataFrame(rows, schema={"date": pl.Date}, orient="row")


def scheme_master(
    *,
    plan_type: str | None = None,
    option_type: str | None = None,
    is_active: bool | None = None,
    require_benchmark: bool = False,
) -> pl.DataFrame:
    """Load the full scheme master, optionally filtered."""
    clauses = []
    args: list = []
    if plan_type is not None:
        clauses.append("plan_type = %s")
        args.append(plan_type)
    if option_type is not None:
        clauses.append("option_type = %s")
        args.append(option_type)
    if is_active is not None:
        clauses.append("is_active = %s")
        args.append(is_active)
    if require_benchmark:
        clauses.append("benchmark_ticker IS NOT NULL")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    schema: dict[str, pl.DataType] = {
        "scheme_code": pl.Utf8,
        "isin_growth": pl.Utf8,
        "isin_idcw": pl.Utf8,
        "scheme_name": pl.Utf8,
        "amc_name": pl.Utf8,
        "amc_code": pl.Utf8,
        "plan_type": pl.Utf8,
        "option_type": pl.Utf8,
        "amfi_category": pl.Utf8,
        "canonical_category": pl.Utf8,
        "benchmark_ticker": pl.Utf8,
        "inception_date": pl.Date,
        "base_fund_id": pl.Utf8,
        "is_active": pl.Boolean,
        "last_seen_date": pl.Date,
    }
    select = ", ".join(schema.keys())
    with connect() as c:
        rows = c.execute(f"SELECT {select} FROM scheme_master {where}", args).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, schema=schema, orient="row")


_COMPUTED_METRICS_COLS = [
    "as_of_date", "scheme_code", "canonical_category", "benchmark_ticker",
    "ret_3y_median", "ret_3y_p25", "ret_5y_median", "ret_5y_p25",
    "alpha_3y_annualized", "alpha_3y_tstat", "alpha_confidence", "sortino_3y",
    "info_ratio_3y",
    "capture_up", "capture_down", "capture_efficiency",
    "r_squared_3y", "beta_3y",
    # Phase 2 additive columns (nullable until ingestion stages fill them).
    "beta_3y_std", "r_squared_3y_mean", "style_drift_3y",
    "active_share_median_1y", "ptr_latest", "aum_impact_cost_days",
    "adv_unresolved_pct",
    "data_quality_flag", "computed_at", "pipeline_version",
]


def computed_metrics_at(as_of: date) -> pl.DataFrame:
    """Load one as_of_date partition of computed_metrics."""
    select = ", ".join(_COMPUTED_METRICS_COLS)
    with connect() as c:
        rows = c.execute(
            f"SELECT {select} FROM computed_metrics WHERE as_of_date = %s",
            (as_of,),
        ).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, schema=_COMPUTED_METRICS_COLS, orient="row")


def computed_metrics_for_schemes(
    as_of: date, scheme_codes: list[str],
) -> pl.DataFrame:
    """Load computed_metrics rows for a specific set of scheme codes within
    one as_of partition. Empty input → empty frame."""
    if not scheme_codes:
        return pl.DataFrame()
    select = ", ".join(_COMPUTED_METRICS_COLS)
    with connect() as c:
        rows = c.execute(
            f"SELECT {select} FROM computed_metrics "
            f"WHERE as_of_date = %s AND scheme_code = ANY(%s)",
            (as_of, list(scheme_codes)),
        ).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, schema=_COMPUTED_METRICS_COLS, orient="row")


# ---------------------------------------------------------------------------
# Phase 2.2: holdings / constituents / PTR readers
# ---------------------------------------------------------------------------


def holdings_for_scheme(
    scheme_code: str, on_or_before: date | None = None,
) -> pl.DataFrame:
    """All holdings rows for one scheme across all months. Sorted by as_of_month.

    Returned columns: scheme_code, security_name, as_of_month, weight_pct, isin,
    instrument_type. ISIN is populated by the Phase-5 SEBI-Excel ingestion
    (100% coverage live) but the column stays nullable for legacy
    factsheet-era rows; active_share matches ISIN-first with a
    normalized-name fallback (A1-8).

    When ``on_or_before`` is given, only months with ``as_of_month`` on or
    before that date are returned (point-in-time reads, A1-12): a month
    published after a historical metric date must not leak into its compute.
    When None, all months are returned (current-run behavior).
    """
    sql = (
        "SELECT scheme_code, security_name, as_of_month, weight_pct, isin, "
        "instrument_type FROM holdings_monthly "
        "WHERE scheme_code = %s "
    )
    params: tuple = (scheme_code,)
    if on_or_before is not None:
        sql += "AND as_of_month <= %s "
        params = (scheme_code, on_or_before)
    sql += "ORDER BY as_of_month, security_name"
    with connect() as c:
        rows = c.execute(sql, params).fetchall()
    if not rows:
        return pl.DataFrame()
    schema = {
        "scheme_code": pl.Utf8,
        "security_name": pl.Utf8,
        "as_of_month": pl.Date,
        "weight_pct": pl.Float64,
        "isin": pl.Utf8,
        "instrument_type": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema, orient="row")


def constituents_for_ticker(ticker: str) -> pl.DataFrame:
    """All index_constituents_monthly rows for one ticker. Sorted by as_of_month."""
    with connect() as c:
        rows = c.execute(
            "SELECT ticker, isin, as_of_month, weight_pct, security_name "
            "FROM index_constituents_monthly "
            "WHERE ticker = %s ORDER BY as_of_month, isin",
            (ticker,),
        ).fetchall()
    if not rows:
        return pl.DataFrame()
    schema = {
        "ticker": pl.Utf8,
        "isin": pl.Utf8,
        "as_of_month": pl.Date,
        "weight_pct": pl.Float64,
        "security_name": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema, orient="row")


def portfolio_turnover_for_scheme(
    scheme_code: str, on_or_before: date | None = None,
) -> pl.DataFrame:
    """All PTR rows for one scheme, sorted by as_of_month.

    ``on_or_before`` bounds the read to months on or before that date
    (point-in-time reads, A1-12); None returns all months.
    """
    sql = (
        "SELECT scheme_code, as_of_month, ptr FROM portfolio_turnover_monthly "
        "WHERE scheme_code = %s "
    )
    params: tuple = (scheme_code,)
    if on_or_before is not None:
        sql += "AND as_of_month <= %s "
        params = (scheme_code, on_or_before)
    sql += "ORDER BY as_of_month"
    with connect() as c:
        rows = c.execute(sql, params).fetchall()
    if not rows:
        return pl.DataFrame()
    schema = {
        "scheme_code": pl.Utf8,
        "as_of_month": pl.Date,
        "ptr": pl.Float64,
    }
    return pl.DataFrame(rows, schema=schema, orient="row")


def latest_holdings_date() -> date | None:
    """Return the most recent as_of_month in holdings_monthly, for freshness."""
    with connect() as c:
        row = c.execute("SELECT MAX(as_of_month) FROM holdings_monthly").fetchone()
    val = row[0] if row else None
    if val is None:
        return None
    return val.date() if hasattr(val, "date") else val


def latest_constituents_date() -> date | None:
    """Return the most recent as_of_month in index_constituents_monthly."""
    with connect() as c:
        row = c.execute(
            "SELECT MAX(as_of_month) FROM index_constituents_monthly"
        ).fetchone()
    val = row[0] if row else None
    if val is None:
        return None
    return val.date() if hasattr(val, "date") else val


def latest_ptr_date() -> date | None:
    """Return the most recent as_of_month in portfolio_turnover_monthly."""
    with connect() as c:
        row = c.execute(
            "SELECT MAX(as_of_month) FROM portfolio_turnover_monthly"
        ).fetchone()
    val = row[0] if row else None
    if val is None:
        return None
    return val.date() if hasattr(val, "date") else val


# ---------------------------------------------------------------------------
# Phase 2.3: stock ADV / AUM readers
# ---------------------------------------------------------------------------


def stock_adv_history(isins: list[str], window_days: int = 90) -> pl.DataFrame:
    """Return recent stock_adv_daily rows for the given ISINs.

    `window_days` should be larger than the rolling median window to allow
    for holidays / non-trading days. Default 90 covers a 60-day median
    safely. Empty input or empty result returns an empty frame.
    """
    if not isins:
        return pl.DataFrame()
    with connect() as c:
        rows = c.execute(
            "SELECT isin, symbol, date, close, total_traded_value "
            "FROM stock_adv_daily "
            "WHERE isin = ANY(%s) AND date >= (CURRENT_DATE - %s::int) "
            "ORDER BY isin, date",
            (list(isins), window_days),
        ).fetchall()
    if not rows:
        return pl.DataFrame()
    schema = {
        "isin": pl.Utf8,
        "symbol": pl.Utf8,
        "date": pl.Date,
        "close": pl.Float64,
        "total_traded_value": pl.Float64,
    }
    return pl.DataFrame(rows, schema=schema, orient="row")


def latest_scheme_aum(
    scheme_code: str, on_or_before: date | None = None
) -> tuple[date, float] | None:
    """Return (as_of_month, aum_crore) for the most recent AUM snapshot.

    AUM comes exclusively from AMFI's quarterly AAUM endpoint
    (source_amc='amfi_aaum'); the per-AMC factsheet path was removed and a
    CHECK constraint enforces the single-source invariant. The latest
    `as_of_month` wins.

    When ``on_or_before`` is given, only quarters whose ``as_of_month`` is on or
    before that date are considered — this is required when attaching AUM to a
    metric stamped at a historical ``as_of`` so a future quarter (published
    after the metric date) cannot leak in. When None, the globally-latest
    quarter is returned (correct for "current AUM" weighting at run time).
    """
    sql = (
        "SELECT as_of_month, aum_crore FROM scheme_aum_monthly "
        "WHERE scheme_code = %s "
    )
    params: tuple = (scheme_code,)
    if on_or_before is not None:
        sql += "AND as_of_month <= %s "
        params = (scheme_code, on_or_before)
    sql += "ORDER BY as_of_month DESC LIMIT 1"
    with connect() as c:
        row = c.execute(sql, params).fetchone()
    if not row:
        return None
    m, a = row
    return (m.date() if hasattr(m, "date") else m, float(a))


def latest_stock_adv_date() -> date | None:
    with connect() as c:
        row = c.execute("SELECT MAX(date) FROM stock_adv_daily").fetchone()
    val = row[0] if row else None
    return (val.date() if val and hasattr(val, "date") else val)


def latest_aum_date() -> date | None:
    with connect() as c:
        row = c.execute("SELECT MAX(as_of_month) FROM scheme_aum_monthly").fetchone()
    val = row[0] if row else None
    return (val.date() if val and hasattr(val, "date") else val)


def latest_computed_metrics_date() -> date | None:
    with connect() as c:
        row = c.execute(
            "SELECT MAX(as_of_date) FROM computed_metrics"
        ).fetchone()
    return row[0] if row else None


def has_factsheet_rows(amc_slug: str, as_of_month: date) -> bool:
    """True if this AMC already has holdings OR PTR rows for the given month.
    Used by the factsheet parse-skip: a skip is only safe when the prior ingest
    is actually present in the DB."""
    with connect() as c:
        h = c.execute(
            "SELECT 1 FROM holdings_monthly "
            "WHERE source_amc = %s AND as_of_month = %s LIMIT 1",
            (amc_slug, as_of_month),
        ).fetchone()
        if h:
            return True
        p = c.execute(
            "SELECT 1 FROM portfolio_turnover_monthly "
            "WHERE source_amc = %s AND as_of_month = %s LIMIT 1",
            (amc_slug, as_of_month),
        ).fetchone()
    return bool(p)


def benchmark_latest_by_ticker() -> dict[str, date]:
    """Per-ticker MAX(date) in benchmark_daily — the incremental-ingest cursor.
    A ticker absent from the result has no rows yet (needs a full backfill)."""
    with connect() as c:
        rows = c.execute(
            "SELECT ticker, MAX(date) FROM benchmark_daily GROUP BY ticker"
        ).fetchall()
    return {t: d for t, d in rows if d is not None}


def latest_dates() -> dict[str, date | None]:
    """For freshness: latest date in each top-level series."""
    with connect() as c:
        nav_max = c.execute("SELECT MAX(nav_date) FROM nav_daily").fetchone()[0]
        rf_max = c.execute("SELECT MAX(date) FROM risk_free_daily").fetchone()[0]
        sm_max = c.execute("SELECT MAX(last_seen_date) FROM scheme_master").fetchone()[0]
        bench = c.execute(
            "SELECT ticker, MAX(date) FROM benchmark_daily GROUP BY ticker"
        ).fetchall()
    return {
        "nav_latest": nav_max,
        "rf_latest": rf_max,
        "scheme_master_latest": sm_max,
        "benchmarks": {t: d for t, d in bench},
    }


def nav_daily_day_counts(n: int) -> list[tuple[date, int]]:
    """Per-day nav_daily row counts for the most recent ``n`` distinct days,
    newest first. B4: the amfi_nav partial-publication guard derives its
    trailing full-day baseline from these pairs."""
    with connect() as c:
        rows = c.execute(
            "SELECT nav_date, COUNT(*) FROM nav_daily "
            "GROUP BY nav_date ORDER BY nav_date DESC LIMIT %s",
            (n,),
        ).fetchall()
    return [
        (d.date() if hasattr(d, "date") else d, int(cnt)) for d, cnt in rows
    ]


def fund_log_returns(scheme_code: str) -> pl.DataFrame:
    """Return cached daily log returns for one scheme: columns date, log_return."""
    with connect() as c:
        rows = c.execute(
            "SELECT date, log_return FROM fund_log_returns "
            "WHERE scheme_code = %s ORDER BY date",
            (scheme_code,),
        ).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(
        rows, schema={"date": pl.Date, "log_return": pl.Float64}, orient="row"
    )


# ---------------------------------------------------------------------------
# D2: point-in-time history readers (scheme_master_history / rank_history)
# ---------------------------------------------------------------------------


_SCHEME_MASTER_HISTORY_SCHEMA: dict[str, pl.DataType] = {
    "snapshot_date": pl.Date,
    "scheme_code": pl.Utf8,
    "isin_growth": pl.Utf8,
    "isin_idcw": pl.Utf8,
    "scheme_name": pl.Utf8,
    "amc_name": pl.Utf8,
    "amc_code": pl.Utf8,
    "plan_type": pl.Utf8,
    "option_type": pl.Utf8,
    "amfi_category": pl.Utf8,
    "canonical_category": pl.Utf8,
    "benchmark_ticker": pl.Utf8,
    "inception_date": pl.Date,
    "base_fund_id": pl.Utf8,
    "is_active": pl.Boolean,
    "last_seen_date": pl.Date,
    "departed_at": pl.Date,
}


def scheme_master_history_at(snapshot_date: date) -> pl.DataFrame:
    """One scheme_master_history partition — the full universe state recorded
    by the build() run on ``snapshot_date``. Empty frame when no snapshot
    exists for that date."""
    select = ", ".join(_SCHEME_MASTER_HISTORY_SCHEMA)
    with connect() as c:
        rows = c.execute(
            f"SELECT {select} FROM scheme_master_history "
            f"WHERE snapshot_date = %s ORDER BY scheme_code",
            (snapshot_date,),
        ).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, schema=_SCHEME_MASTER_HISTORY_SCHEMA, orient="row")


def scheme_master_history_dates() -> list[date]:
    """All snapshot dates present in scheme_master_history, ascending."""
    with connect() as c:
        rows = c.execute(
            "SELECT DISTINCT snapshot_date FROM scheme_master_history "
            "ORDER BY snapshot_date"
        ).fetchall()
    return [r[0] for r in rows]


_RANK_HISTORY_SCHEMA: dict[str, pl.DataType] = {
    "as_of_date": pl.Date,
    "stage": pl.Int32,
    "scheme_code": pl.Utf8,
    "canonical_category": pl.Utf8,
    "composite_score": pl.Float64,
    "composite_score_stage1": pl.Float64,
    "rank_in_category": pl.Int32,
    "z_ret_3y_median": pl.Float64,
    "z_ret_3y_p25": pl.Float64,
    "z_ret_5y_median": pl.Float64,
    "z_ret_5y_p25": pl.Float64,
    "z_alpha_3y_annualized": pl.Float64,
    "z_sortino_3y": pl.Float64,
    "z_info_ratio_3y": pl.Float64,
    "z_capture_efficiency": pl.Float64,
    "z_active_share_median_1y": pl.Float64,
    "z_style_drift_3y": pl.Float64,
    "ptr_latest": pl.Float64,
    "aum_impact_cost_days": pl.Float64,
    "included": pl.Boolean,
    "exclusion_reason": pl.Utf8,
    "run_id": pl.Utf8,
}


def rank_history_at(as_of: date, stage: int | None = None) -> pl.DataFrame:
    """One rank_history partition — every stage 1/2/3 outcome (survivors and
    exclusions) recorded by the rank_deep run for ``as_of``. Optionally
    restricted to one stage."""
    select = ", ".join(_RANK_HISTORY_SCHEMA)
    sql = f"SELECT {select} FROM rank_history WHERE as_of_date = %s"
    params: tuple = (as_of,)
    if stage is not None:
        sql += " AND stage = %s"
        params = (as_of, stage)
    sql += " ORDER BY stage, canonical_category, rank_in_category, scheme_code"
    with connect() as c:
        rows = c.execute(sql, params).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, schema=_RANK_HISTORY_SCHEMA, orient="row")


def rank_history_dates() -> list[date]:
    """All as_of dates present in rank_history, ascending."""
    with connect() as c:
        rows = c.execute(
            "SELECT DISTINCT as_of_date FROM rank_history ORDER BY as_of_date"
        ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# F-3: CLI diagnostics readers (status / db coverage / missing-data / validate)
#
# Pure read-side functions returning structured rows — no typer, no printing.
# The CLI commands are thin formatters over these.
# ---------------------------------------------------------------------------


# Per-table date column used for row-count + date-bound diagnostics. Also the
# allowlist guarding the f-string table interpolation below.
_TABLE_DATE_COLUMNS: dict[str, str] = {
    "nav_daily": "nav_date",
    "benchmark_daily": "date",
    "risk_free_daily": "date",
    "scheme_master": "last_seen_date",
    "computed_metrics": "as_of_date",
    "fund_log_returns": "date",
}

#: Tables reported by `mfs status` / `mfs db status`, in display order.
STATUS_TABLES: list[str] = list(_TABLE_DATE_COLUMNS)


def table_bounds(table: str) -> tuple[int, date | None, date | None]:
    """(row_count, min_date, max_date) for one of the STATUS_TABLES."""
    col = _TABLE_DATE_COLUMNS[table]  # KeyError on unknown table by design
    with connect() as c:
        n, mn, mx = c.execute(
            f"SELECT COUNT(*), MIN({col}), MAX({col}) FROM {table}"
        ).fetchone()
    return n, mn, mx


def table_count(table: str) -> int:
    """Row count for one of the STATUS_TABLES."""
    if table not in _TABLE_DATE_COLUMNS:
        raise KeyError(table)
    with connect() as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def trading_calendar_bounds(since: date) -> tuple[int, date | None, date | None]:
    """(count, first, last) of NIFTY 50 TRI trading days on/after ``since`` —
    the reference calendar for the coverage diagnostics."""
    with connect() as c:
        n, mn, mx = c.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM benchmark_daily "
            "WHERE ticker = 'NIFTY 50 TRI' AND date >= %s",
            (since,),
        ).fetchone()
    return n, mn, mx


def benchmark_gap_report(
    since: date,
) -> list[tuple[str, int, int, int, date | None, date | None]]:
    """Per benchmark ticker: (ticker, expected, actual, missing, first, last)
    vs the NIFTY 50 TRI trading calendar since ``since``. Worst gaps first."""
    with connect() as c:
        return c.execute(
            """
            WITH cal AS (
                SELECT date FROM benchmark_daily
                WHERE ticker = 'NIFTY 50 TRI' AND date >= %s
            ),
            tickers AS (SELECT DISTINCT ticker FROM benchmark_daily)
            SELECT t.ticker,
                   (SELECT COUNT(*) FROM cal) AS expected,
                   COUNT(bd.date) AS actual,
                   (SELECT COUNT(*) FROM cal) - COUNT(bd.date) AS missing,
                   MIN(bd.date) AS first_day, MAX(bd.date) AS last_day
            FROM tickers t
            CROSS JOIN cal c
            LEFT JOIN benchmark_daily bd
              ON bd.ticker = t.ticker AND bd.date = c.date
            GROUP BY t.ticker
            ORDER BY missing DESC, t.ticker;
            """,
            (since,),
        ).fetchall()


class RiskFreeGapReport(NamedTuple):
    """Risk-free trading-day coverage vs the NIFTY 50 TRI calendar."""

    rf_min: date
    rf_max: date
    #: Calendar days before the first rf observation (necessarily unfilled).
    n_pre: int
    #: Calendar days on/after rf_min with no rf row (should be empty —
    #: forward-fill at ingest should cover every trading day).
    missing_dates: list[date]


def risk_free_gap_report(since: date) -> RiskFreeGapReport | None:
    """Risk-free coverage vs the trading calendar since ``since``.
    None when risk_free_daily is empty."""
    with connect() as c:
        rf_min, rf_max = c.execute(
            "SELECT MIN(date), MAX(date) FROM risk_free_daily"
        ).fetchone()
        if rf_min is None:
            return None
        missing = c.execute(
            """
            SELECT c.date FROM (
                SELECT date FROM benchmark_daily
                WHERE ticker = 'NIFTY 50 TRI' AND date >= %s
            ) c
            LEFT JOIN risk_free_daily rf ON rf.date = c.date
            WHERE c.date >= %s AND rf.date IS NULL
            ORDER BY c.date
            """,
            (since, rf_min),
        ).fetchall()
        n_pre = c.execute(
            "SELECT COUNT(*) FROM benchmark_daily "
            "WHERE ticker = 'NIFTY 50 TRI' AND date >= %s AND date < %s",
            (since, rf_min),
        ).fetchone()[0]
    return RiskFreeGapReport(rf_min, rf_max, n_pre, [r[0] for r in missing])


def nav_coverage_report(
    since: date, rankable_categories: list[str], min_inception_year: int,
) -> list[tuple]:
    """Per active rankable Direct+Growth scheme (inception year >=
    ``min_inception_year``): (scheme_code, scheme_name, inception_date,
    last_seen_date, expected_days, actual_days, missing_days) vs the NIFTY 50
    TRI trading calendar since ``since``. Worst coverage first."""
    with connect() as c:
        return c.execute(
            """
            WITH cal AS (
                SELECT date FROM benchmark_daily
                WHERE ticker = 'NIFTY 50 TRI' AND date >= %s
            ),
            eligible AS (
                SELECT scheme_code, scheme_name, inception_date, last_seen_date
                FROM scheme_master
                WHERE is_active
                  AND plan_type = 'DIRECT'
                  AND option_type = 'GROWTH'
                  AND canonical_category = ANY(%s)
                  AND EXTRACT(YEAR FROM inception_date) >= %s
            ),
            expected AS (
                SELECT e.scheme_code,
                       COUNT(*) AS expected_days
                FROM eligible e
                JOIN cal c ON c.date >= e.inception_date
                          AND c.date <= LEAST(e.last_seen_date, (SELECT MAX(date) FROM cal))
                GROUP BY e.scheme_code
            ),
            actual AS (
                SELECT n.scheme_code, COUNT(*) AS actual_days
                FROM nav_daily n
                JOIN cal c ON c.date = n.nav_date
                WHERE n.scheme_code IN (SELECT scheme_code FROM eligible)
                GROUP BY n.scheme_code
            )
            SELECT e.scheme_code, e.scheme_name,
                   e.inception_date, e.last_seen_date,
                   COALESCE(exp.expected_days, 0) AS expected_days,
                   COALESCE(act.actual_days, 0) AS actual_days,
                   COALESCE(exp.expected_days, 0) - COALESCE(act.actual_days, 0) AS missing_days
            FROM eligible e
            LEFT JOIN expected exp ON exp.scheme_code = e.scheme_code
            LEFT JOIN actual   act ON act.scheme_code = e.scheme_code
            ORDER BY missing_days DESC, e.scheme_code
            """,
            (since, list(rankable_categories), min_inception_year),
        ).fetchall()


def benchmark_inventory(
    since: date,
) -> list[tuple[str, int, date | None, date | None]]:
    """Per benchmark ticker since ``since``: (ticker, rows, first, last)."""
    with connect() as c:
        return c.execute(
            "SELECT ticker, COUNT(*), MIN(date), MAX(date) "
            "FROM benchmark_daily WHERE date >= %s GROUP BY ticker ORDER BY ticker",
            (since,),
        ).fetchall()


def nav_per_scheme_inventory(
    since: date,
) -> list[tuple[str, date, date, int, int]]:
    """Per scheme with any NAV since ``since``: (scheme_code, first, last,
    observed_days, expected_bdays) where expected_bdays approximates business
    days in the scheme's own [first, last] window."""
    with connect() as c:
        return c.execute(
            """
            SELECT scheme_code, MIN(nav_date), MAX(nav_date), COUNT(DISTINCT nav_date),
                   CAST((MAX(nav_date) - MIN(nav_date)) / 7.0 * 5 AS INT) AS expected_bdays
            FROM nav_daily WHERE nav_date >= %s GROUP BY scheme_code
            """,
            (since,),
        ).fetchall()


#: Minimum NIFTY 50 TRI CAGR (fraction) below which the series is suspected
#: of being a price-return (PR) index rather than total-return (TRI).
TRI_SANITY_MIN_CAGR = 0.10


class TriCagrSanity(NamedTuple):
    """NIFTY 50 TRI CAGR over [start_date, end_date]; ok=False → SUSPECT."""

    cagr: float
    start_date: date
    end_date: date
    ok: bool


def tri_cagr_sanity(start: date) -> TriCagrSanity | None:
    """CAGR of NIFTY 50 TRI from its first close on/after ``start`` to its
    latest close, classified against TRI_SANITY_MIN_CAGR. None when there is
    no TRI history on/after ``start``."""
    with connect() as c:
        row = c.execute(
            "SELECT MIN(close), MAX(close), MIN(date), MAX(date) "
            "FROM benchmark_daily WHERE ticker = 'NIFTY 50 TRI' AND date >= %s",
            (start,),
        ).fetchone()
        if not row or row[0] is None:
            return None
        _, _, d_start, d_end = row
        # Need the start close (not min), so query the boundary closes
        start_close = c.execute(
            "SELECT close FROM benchmark_daily WHERE ticker = 'NIFTY 50 TRI' "
            "AND date = %s", (d_start,),
        ).fetchone()[0]
        end_close = c.execute(
            "SELECT close FROM benchmark_daily WHERE ticker = 'NIFTY 50 TRI' "
            "AND date = %s", (d_end,),
        ).fetchone()[0]
    years = (d_end - d_start).days / 365.25
    cagr = (end_close / start_close) ** (1 / years) - 1
    return TriCagrSanity(cagr, d_start, d_end, cagr > TRI_SANITY_MIN_CAGR)


def nav_table_counts() -> tuple[int, int]:
    """(total_rows, distinct_schemes) in nav_daily."""
    with connect() as c:
        return c.execute(
            "SELECT COUNT(*), COUNT(DISTINCT scheme_code) FROM nav_daily"
        ).fetchone()
