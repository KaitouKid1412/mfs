"""Align fund NAVs, benchmark closes, and risk-free rates onto a single trading calendar.

Strategy:
- Master calendar = unique dates in benchmark_daily for ticker NIFTY 50 TRI.
- For each (scheme_code, benchmark_ticker) pair we produce a daily frame with columns:
    date, nav, bench_close, rate_daily, rate_daily_log, fund_log_ret, bench_log_ret
  for the date range [scheme inception, today].

`rate_daily` is the simple per-day risk-free return (1+r_annual)^(1/252)-1, kept
for legacy callers / display. `rate_daily_log = ln(1 + rate_daily)` is the
log-scale equivalent — use this when subtracting from log returns (alpha,
sortino) so units match.

Forward-fill NAV at most 1 day for holiday lag; gaps beyond that stay null.

Data lives in Postgres (see `mfs.db.queries`). Fund log returns are computed
HERE, from the aligned NAV series, on every metrics run — the
`fund_log_returns` DB table is a write-side cache that no compute code reads.

Non-finite guard: a zero/negative NAV (or corrupt benchmark close / risk-free
rate) makes log() emit NaN/±inf, and the 1-day shift propagates it to the next
row too. Downstream `drop_nulls()` calls (sortino, capture, info_ratio, alpha)
do NOT drop NaN/inf, so a single poisoned row would NaN every rolling metric.
`align_scheme` therefore nulls out non-finite log-return values — counted and
logged — so they fall out with the existing null-dropping instead.
"""

from __future__ import annotations

import polars as pl

from mfs.db import queries as q
from mfs.utils.logging import get_logger

log = get_logger(__name__)


def master_calendar() -> pl.DataFrame:
    """Return the canonical trading-date series (sorted dates from NIFTY 50 TRI)."""
    return q.master_calendar()


def _benchmark_series(ticker: str) -> pl.DataFrame:
    """Return columns: date, close for one ticker."""
    return q.benchmark_series(ticker)


def _risk_free_series() -> pl.DataFrame:
    return q.risk_free_series()


def _nav_series(scheme_code: str) -> pl.DataFrame:
    """Return NAV history for a single scheme: date, nav."""
    return q.nav_series(scheme_code)


def align_scheme(scheme_code: str, benchmark_ticker: str) -> pl.DataFrame:
    """Return aligned daily frame for one scheme vs its benchmark.

    Columns: date, nav, bench_close, rate_daily, fund_log_ret, bench_log_ret
    """
    cal = master_calendar()
    if cal.is_empty():
        return pl.DataFrame()
    nav = _nav_series(scheme_code)
    if nav.is_empty():
        return pl.DataFrame()
    bench = _benchmark_series(benchmark_ticker)
    if bench.is_empty():
        return pl.DataFrame()
    rf = _risk_free_series()

    inception = nav["date"].min()
    last = nav["date"].max()
    cal = cal.filter((pl.col("date") >= inception) & (pl.col("date") <= last))

    aligned = (
        cal.join(nav, on="date", how="left")
        .with_columns(pl.col("nav").forward_fill().over(pl.lit(1)).alias("nav_ff"))
        # Limit forward-fill to 1 step: any gap >1 day stays null
    )
    # Cleaner forward-fill of 1 day only: compare to previous non-null
    aligned = aligned.with_columns(
        pl.col("nav").forward_fill(limit=1).alias("nav_ff"),
    ).with_columns(
        pl.col("nav_ff").alias("nav")
    ).drop("nav_ff")

    aligned = aligned.join(bench.rename({"close": "bench_close"}), on="date", how="left")
    if not rf.is_empty():
        aligned = aligned.join(rf, on="date", how="left").with_columns(
            pl.col("rate_daily").forward_fill()
        )
    else:
        aligned = aligned.with_columns(pl.lit(None, dtype=pl.Float64).alias("rate_daily"))

    aligned = aligned.with_columns(
        (pl.col("rate_daily") + 1.0).log().alias("rate_daily_log"),
        (pl.col("nav").log() - pl.col("nav").shift(1).log()).alias("fund_log_ret"),
        (pl.col("bench_close").log() - pl.col("bench_close").shift(1).log()).alias(
            "bench_log_ret"
        ),
    )
    # Non-finite guard (A1-1): zero/negative inputs make log() emit NaN/±inf,
    # which survive downstream drop_nulls() and NaN-poison every rolling
    # metric. Null them out — counted — so they drop with the existing nulls.
    log_ret_cols = ("fund_log_ret", "bench_log_ret", "rate_daily_log")
    n_nonfinite = aligned.select(
        pl.any_horizontal(
            [pl.col(c).is_not_null() & ~pl.col(c).is_finite() for c in log_ret_cols]
        ).sum()
    ).item()
    if n_nonfinite:
        log.warning(
            "alignment.nonfinite_log_returns_nulled",
            scheme_code=scheme_code,
            n_rows=int(n_nonfinite),
        )
        aligned = aligned.with_columns(
            [
                pl.when(pl.col(c).is_finite()).then(pl.col(c)).otherwise(None).alias(c)
                for c in log_ret_cols
            ]
        )
    return aligned
