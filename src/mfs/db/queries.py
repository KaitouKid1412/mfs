"""Read-side helpers that translate Postgres tables into the same polars
DataFrame shapes the compute/rank layers used to get from parquet.

Each function is a thin wrapper around `psycopg.connect().execute()` followed by
`pl.from_records(...)`. The signatures match what `alignment.py`, `freshness.py`,
`shortlist.py`, and `orchestrator.py` expect, so callers swap in one line.
"""

from __future__ import annotations

from datetime import date

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
    "alpha_3y_annualized", "alpha_3y_tstat", "sortino_3y",
    "info_ratio_3y",
    "capture_up", "capture_down", "capture_efficiency",
    "r_squared_3y", "beta_3y",
    # Phase 2 additive columns (nullable until ingestion stages fill them).
    "beta_3y_std", "r_squared_3y_mean", "style_drift_3y",
    "active_share_median_1y", "ptr_latest", "aum_impact_cost_days",
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


def holdings_for_scheme(scheme_code: str) -> pl.DataFrame:
    """All holdings rows for one scheme across all months. Sorted by as_of_month.

    Returned columns: scheme_code, security_name, as_of_month, weight_pct, isin,
    instrument_type. ISIN may be null (factsheet PDFs don't print ISINs).
    """
    with connect() as c:
        rows = c.execute(
            "SELECT scheme_code, security_name, as_of_month, weight_pct, isin, "
            "instrument_type FROM holdings_monthly "
            "WHERE scheme_code = %s ORDER BY as_of_month, security_name",
            (scheme_code,),
        ).fetchall()
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


def portfolio_turnover_for_scheme(scheme_code: str) -> pl.DataFrame:
    """All PTR rows for one scheme, sorted by as_of_month."""
    with connect() as c:
        rows = c.execute(
            "SELECT scheme_code, as_of_month, ptr FROM portfolio_turnover_monthly "
            "WHERE scheme_code = %s ORDER BY as_of_month",
            (scheme_code,),
        ).fetchall()
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
# Phase 2.3: stock ADV / AUM / stress test readers
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


def latest_scheme_aum(scheme_code: str) -> tuple[date, float] | None:
    """Return (as_of_month, aum_crore) for the most recent AUM snapshot.

    AUM comes exclusively from AMFI's quarterly AAUM endpoint
    (source_amc='amfi_aaum'); the per-AMC factsheet path was removed and a
    CHECK constraint enforces the single-source invariant. The latest
    `as_of_month` wins.
    """
    with connect() as c:
        row = c.execute(
            "SELECT as_of_month, aum_crore FROM scheme_aum_monthly "
            "WHERE scheme_code = %s "
            "ORDER BY as_of_month DESC "
            "LIMIT 1",
            (scheme_code,),
        ).fetchone()
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
