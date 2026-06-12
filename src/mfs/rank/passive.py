"""D7 — passive-alternative verdict per category.

The pipeline picks the best *active* funds per category but never asked the
prior question: would the investable index have beaten the median fund? This
module builds one row per rankable category comparing the benchmark TRI's
rolling 3y/5y annualized return — computed with the SAME method as fund
returns (`returns.rolling_distribution` fed the benchmark close series as the
'nav' column, so windows / calendar-CAGR annualization / trailing-endpoint
epoch are like-for-like) — against the category's median fund from
computed_metrics at the same as_of, with an explicit ``index_wins`` marker
when the median fund trails on 3y.

Caveats carried in the artifact header (see ``write_csv``):
  * survivor-biased — dead funds are absent from computed_metrics, so the
    median fund is flattered and ``index_wins`` is, if anything, understated;
  * rows reflect the benchmark MAPPING in force when the as_of partition was
    computed — D5's Value/Energy remap applies from the first post-remap run;
  * synthetic hybrid benchmarks (D6, T-bill debt sleeve) are easier to beat
    than the real hybrid index: ``benchmark_is_synthetic`` rows carry the
    note 'synthetic benchmark — hybrid alpha overstated'.

``rank_deep`` writes ``<as_of>/passive_alternative.csv`` and joins
``pick_minus_benchmark_3y`` (pick's ret_3y_median minus the benchmark median
3y) onto the stage-3 rows so every pick shows its margin vs the index.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

import polars as pl

from mfs.compute import returns
from mfs.config import get_pipeline_config, get_thresholds
from mfs.ingest.synthetic_hybrid import SYNTHETIC_BENCHMARK_TICKERS
from mfs.utils.logging import get_logger

log = get_logger(__name__)

SYNTHETIC_NOTE = "synthetic benchmark — hybrid alpha overstated"

#: Output column order of the passive_alternative.csv artifact.
TABLE_COLUMNS = [
    "as_of_date",
    "canonical_category",
    "benchmark_ticker",
    "n_funds",
    "benchmark_ret_3y_median",
    "benchmark_ret_5y_median",
    "fund_ret_3y_median",
    "fund_ret_5y_median",
    "median_fund_excess_3y",
    "median_fund_excess_5y",
    "index_wins",
    "benchmark_is_synthetic",
    "note",
]


def benchmark_rolling_medians(
    close: pl.DataFrame, as_of: date, step: str = "1w",
) -> dict[str, float | None]:
    """Median rolling 3y/5y annualized return of a benchmark close series,
    computed exactly as fund returns are: the close series (truncated to
    ``date <= as_of``) is fed to ``returns.rolling_distribution`` as the
    'nav' column. Missing history yields None, never raises."""
    out: dict[str, float | None] = {"ret_3y_median": None, "ret_5y_median": None}
    if close.is_empty():
        return out
    nav_like = (
        close.rename({"close": "nav"})
        .select(["date", "nav"])
        .filter(pl.col("date") <= as_of)
        .sort("date")
    )
    if nav_like.is_empty():
        return out
    out["ret_3y_median"] = returns.rolling_distribution(
        nav_like, window_years=3, step=step
    )["median"]
    out["ret_5y_median"] = returns.rolling_distribution(
        nav_like, window_years=5, step=step
    )["median"]
    return out


def build_table(
    as_of: date,
    *,
    metrics: pl.DataFrame | None = None,
    benchmark_series_fn: Callable[[str], pl.DataFrame] | None = None,
    step: str | None = None,
    rankable_categories: list[str] | None = None,
) -> pl.DataFrame:
    """One row per rankable category at ``as_of``. Read-only.

    ``metrics`` / ``benchmark_series_fn`` / ``rankable_categories`` are
    injectable for tests; defaults read computed_metrics and benchmark_daily
    from the live DB.
    """
    from mfs.db import queries as q

    if metrics is None:
        metrics = q.computed_metrics_at(as_of)
    if metrics.is_empty():
        return pl.DataFrame()
    benchmark_series_fn = benchmark_series_fn or q.benchmark_series
    step = step or get_pipeline_config().rolling.step
    if rankable_categories is None:
        rankable_categories = list(get_thresholds().get("rankable_categories", []))

    cats = (
        metrics.filter(
            pl.col("canonical_category").is_in(rankable_categories)
        )
        .group_by("canonical_category")
        .agg(
            pl.len().alias("n_funds"),
            pl.col("benchmark_ticker").drop_nulls().first().alias("benchmark_ticker"),
            pl.col("ret_3y_median").median().alias("fund_ret_3y_median"),
            pl.col("ret_5y_median").median().alias("fund_ret_5y_median"),
        )
        .sort("canonical_category")
    )
    if cats.is_empty():
        return pl.DataFrame()

    # One rolling-distribution run per distinct ticker (categories share
    # NIFTY 500 TRI), reused across categories.
    medians_by_ticker: dict[str, dict[str, float | None]] = {}
    for ticker in cats["benchmark_ticker"].drop_nulls().unique().to_list():
        medians_by_ticker[ticker] = benchmark_rolling_medians(
            benchmark_series_fn(ticker), as_of, step=step,
        )

    rows: list[dict] = []
    for r in cats.iter_rows(named=True):
        ticker = r["benchmark_ticker"]
        bench = medians_by_ticker.get(ticker, {"ret_3y_median": None, "ret_5y_median": None})
        b3, b5 = bench["ret_3y_median"], bench["ret_5y_median"]
        f3, f5 = r["fund_ret_3y_median"], r["fund_ret_5y_median"]
        ex3 = (f3 - b3) if (f3 is not None and b3 is not None) else None
        ex5 = (f5 - b5) if (f5 is not None and b5 is not None) else None
        synthetic = ticker in SYNTHETIC_BENCHMARK_TICKERS
        rows.append({
            "as_of_date": as_of,
            "canonical_category": r["canonical_category"],
            "benchmark_ticker": ticker,
            "n_funds": r["n_funds"],
            "benchmark_ret_3y_median": b3,
            "benchmark_ret_5y_median": b5,
            "fund_ret_3y_median": f3,
            "fund_ret_5y_median": f5,
            "median_fund_excess_3y": ex3,
            "median_fund_excess_5y": ex5,
            # Verdict on the 3y horizon (5y shown alongside): the index wins
            # this category when the median fund trails it.
            "index_wins": (ex3 < 0) if ex3 is not None else None,
            "benchmark_is_synthetic": synthetic,
            "note": SYNTHETIC_NOTE if synthetic else "",
        })
    return pl.DataFrame(rows).select(TABLE_COLUMNS)


def attach_pick_excess(
    survivors: pl.DataFrame, table: pl.DataFrame,
) -> pl.DataFrame:
    """Join ``pick_minus_benchmark_3y`` (pick's ret_3y_median minus its
    category benchmark's median rolling 3y return) onto ranked rows, so
    every pick shows its margin vs the investable index."""
    if (
        survivors.is_empty()
        or table.is_empty()
        or "ret_3y_median" not in survivors.columns
    ):
        return survivors
    bench = table.select(["canonical_category", "benchmark_ret_3y_median"])
    return (
        survivors.join(bench, on="canonical_category", how="left")
        .with_columns(
            (pl.col("ret_3y_median") - pl.col("benchmark_ret_3y_median"))
            .alias("pick_minus_benchmark_3y")
        )
        .drop("benchmark_ret_3y_median")
    )


def write_csv(table: pl.DataFrame, path: Path) -> Path:
    """Write the artifact with its caveat header ('#' comment lines, then the
    CSV proper)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# passive_alternative (D7): benchmark TRI rolling 3y/5y median CAGR vs "
        "the category median fund; index_wins = median fund trails on 3y.\n"
        "# CAVEATS: survivor-biased (dead funds absent, median fund flattered); "
        "rows use the benchmark mapping in force at this as_of's compute — the "
        "D5 Value/Energy remap applies from the first post-remap run; "
        "synthetic hybrid benchmarks (T-bill debt sleeve) are easier to beat — "
        "see benchmark_is_synthetic/note columns.\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(table.write_csv())
    log.info("rank.passive_alternative_written", path=str(path), n_rows=table.height)
    return path
