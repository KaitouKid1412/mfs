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
