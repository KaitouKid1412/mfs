"""Align fund NAVs, benchmark closes, and risk-free rates onto a single trading calendar.

Strategy:
- Master calendar = unique dates in benchmark_daily for ticker NIFTY 50 TRI.
- For each (scheme_code, benchmark_ticker) pair we produce a daily frame with columns:
    date, nav, bench_close, rate_daily, fund_log_ret, bench_log_ret
  for the date range [scheme inception, today].

Forward-fill NAV at most 1 day for holiday lag; gaps beyond that stay null.
"""

from __future__ import annotations

import polars as pl

from mfs import paths
from mfs.io.parquet import read_dataset, read_partition
from mfs.utils.logging import get_logger

log = get_logger(__name__)


def master_calendar() -> pl.DataFrame:
    """Return the canonical trading-date series (sorted dates from NIFTY 50 TRI)."""
    bench = read_dataset(paths.benchmark_daily_dataset())
    if bench.is_empty():
        return pl.DataFrame({"date": pl.Series([], dtype=pl.Date)})
    nifty = bench.filter(pl.col("ticker") == "NIFTY 50 TRI").select(["date"]).unique("date").sort("date")
    if nifty.is_empty():
        # Fallback: any benchmark
        nifty = bench.select(["date"]).unique("date").sort("date")
    return nifty


def _benchmark_series(ticker: str) -> pl.DataFrame:
    """Return columns: date, close, total_return for one ticker."""
    from mfs.ingest.benchmarks import ticker_slug

    df = read_partition(paths.benchmark_daily_dataset(), ticker_slug(ticker), "ticker")
    if df.is_empty():
        return df
    return df.select(["date", "close", "total_return"]).sort("date")


def _risk_free_series() -> pl.DataFrame:
    rf_path = paths.risk_free_daily_file()
    if not rf_path.exists():
        return pl.DataFrame({"date": [], "rate_daily": []})
    return pl.read_parquet(rf_path).select(["date", "rate_daily"]).sort("date")


def _nav_series(scheme_code: str) -> pl.DataFrame:
    """Return NAV history for a single scheme: date, nav. Scans yearly partitions."""
    nav_root = paths.nav_daily_dataset()
    if not nav_root.exists():
        return pl.DataFrame()
    df = read_dataset(nav_root)
    if df.is_empty():
        return df
    out = (
        df.filter(pl.col("scheme_code") == scheme_code)
        .select(["nav_date", "nav"])
        .rename({"nav_date": "date"})
        .unique("date", keep="last")
        .sort("date")
    )
    return out


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
        (pl.col("nav").log() - pl.col("nav").shift(1).log()).alias("fund_log_ret"),
        (pl.col("bench_close").log() - pl.col("bench_close").shift(1).log()).alias(
            "bench_log_ret"
        ),
    )
    return aligned
