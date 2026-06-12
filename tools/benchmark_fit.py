#!/usr/bin/env python
"""Benchmark-fit diagnostic (D4). READ-ONLY — never writes Postgres.

A category whose funds regress poorly against their mapped benchmark
(median rolling-3y R² < 0.80) has a benchmark-MAPPING problem: the "alpha"
measured there is mostly unexplained fit residual, not manager skill.
This tool surfaces those categories and produces the refit evidence used
to fix the mapping (D5).

Modes
-----
report [--as-of YYYY-MM-DD]
    Per-category n / median r_squared_3y / median beta_3y from the
    computed_metrics partition at the latest (or given) as_of, sorted
    ascending by median R². Median R² < LOW_CONFIDENCE_R2_THRESHOLD (0.80,
    shared with rank/shortlist.py's benchmark_fit_low_confidence column)
    classifies the category LOW_CONFIDENCE_ALPHA.

refit --category X --candidates 'T1,T2,...' [--as-of YYYY-MM-DD]
    For every fund in the category, align against each candidate ticker
    present in benchmark_daily and run the SAME production rolling-3y
    regression the pipeline uses (compute.alpha.rolling_alpha_beta_r2 over
    compute.alignment.align_scheme) — reports per-candidate median R² /
    beta / annualized alpha across the category's funds. This is the
    evidence base for changing configs/benchmarks.csv (D5 selection rule:
    highest median R², adopt only if >= 0.75).

Usage
-----
    uv run python tools/benchmark_fit.py report
    uv run python tools/benchmark_fit.py refit --category Value \
        --candidates 'NIFTY 500 TRI,NIFTY 500 Value 50 TRI'
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import date, datetime

import numpy as np
import polars as pl

from mfs.rank.shortlist import (
    LOW_CONFIDENCE_R2_THRESHOLD,
    category_benchmark_fit_flags,
)

LOW_CONFIDENCE_LABEL = "LOW_CONFIDENCE_ALPHA"


def is_low_confidence(
    median_r2: float | None, threshold: float = LOW_CONFIDENCE_R2_THRESHOLD,
) -> bool | None:
    """Classify a category's median rolling-3y R². 0.79 → True, 0.80 → False,
    None (no regressable funds) → None."""
    if median_r2 is None:
        return None
    return median_r2 < threshold


def fit_report(metrics: pl.DataFrame) -> pl.DataFrame:
    """Pure: per-category benchmark-fit table from one computed_metrics
    partition. Columns: canonical_category, n (funds with a regressable R²),
    median_r2, median_beta, low_confidence_alpha. Sorted ascending median_r2
    (worst fits first)."""
    if metrics.is_empty():
        return pl.DataFrame()
    base = (
        metrics.filter(
            pl.col("canonical_category").is_not_null()
            & pl.col("r_squared_3y").is_not_null()
        )
        .group_by("canonical_category")
        .agg(
            pl.len().alias("n"),
            pl.col("r_squared_3y").median().alias("median_r2"),
            pl.col("beta_3y").median().alias("median_beta"),
        )
    )
    flags = category_benchmark_fit_flags(metrics).rename(
        {"benchmark_fit_low_confidence": "low_confidence_alpha"}
    )
    return (
        base.join(flags, on="canonical_category", how="left")
        .sort("median_r2", nulls_last=True)
    )


def refit_category(
    category: str,
    candidates: list[str],
    as_of: date | None = None,
    *,
    step: str | None = None,
    scheme_codes: list[str] | None = None,
    align_fn: Callable[[str, str], pl.DataFrame] | None = None,
    available_fn: Callable[[str], bool] | None = None,
) -> pl.DataFrame:
    """Per-candidate median R²/beta/alpha across every fund in ``category``.

    Production-faithful: per (fund, candidate) the regression is
    ``alpha.rolling_alpha_beta_r2(align_scheme(code, ticker))`` — exactly the
    Phase-1 path. ``scheme_codes`` / ``align_fn`` / ``available_fn`` are
    injectable for tests; defaults read the live DB (read-only).

    Returns columns: candidate_ticker, available, n_funds, n_regressable,
    median_r2, median_beta, median_alpha_ann. Candidates absent from
    benchmark_daily are reported with available=False and null medians.
    """
    from mfs.compute import alignment, alpha
    from mfs.config import get_pipeline_config
    from mfs.db import queries as q

    step = step or get_pipeline_config().rolling.step

    if scheme_codes is None:
        if as_of is None:
            as_of = q.latest_computed_metrics_date()
        if as_of is None:
            raise RuntimeError("No computed_metrics partitions in the DB")
        part = q.computed_metrics_at(as_of)
        scheme_codes = (
            part.filter(pl.col("canonical_category") == category)["scheme_code"]
            .unique()
            .sort()
            .to_list()
        )
    if not scheme_codes:
        raise RuntimeError(f"No funds found for category {category!r}")

    align_fn = align_fn or alignment.align_scheme
    available_fn = available_fn or (
        lambda ticker: not q.benchmark_series(ticker).is_empty()
    )

    rows: list[dict] = []
    for ticker in candidates:
        if not available_fn(ticker):
            rows.append({
                "candidate_ticker": ticker,
                "available": False,
                "n_funds": len(scheme_codes),
                "n_regressable": 0,
                "median_r2": None,
                "median_beta": None,
                "median_alpha_ann": None,
            })
            continue
        r2s: list[float] = []
        betas: list[float] = []
        alphas: list[float] = []
        for code in scheme_codes:
            aligned = align_fn(code, ticker)
            if aligned.is_empty():
                continue
            res = alpha.rolling_alpha_beta_r2(aligned, window_years=3, step=step)
            if res["r2"] is None:
                continue
            r2s.append(res["r2"])
            betas.append(res["beta"])
            alphas.append(res["alpha_ann"])
        rows.append({
            "candidate_ticker": ticker,
            "available": True,
            "n_funds": len(scheme_codes),
            "n_regressable": len(r2s),
            "median_r2": float(np.median(r2s)) if r2s else None,
            "median_beta": float(np.median(betas)) if betas else None,
            "median_alpha_ann": float(np.median(alphas)) if alphas else None,
        })
    return pl.DataFrame(rows).sort(
        "median_r2", descending=True, nulls_last=True,
    )


def best_candidate(refit: pl.DataFrame, min_r2: float = 0.75) -> str | None:
    """D5 selection rule: the available candidate with the highest median R²,
    adopted only if its median R² >= ``min_r2``; else None (keep incumbent +
    rely on the low-confidence flag)."""
    if refit.is_empty():
        return None
    ok = refit.filter(
        pl.col("available") & (pl.col("median_r2") >= min_r2)
    ).sort("median_r2", descending=True)
    if ok.is_empty():
        return None
    return ok["candidate_ticker"][0]


def _cmd_report(args: argparse.Namespace) -> int:
    from mfs.db import queries as q

    as_of = (
        datetime.strptime(args.as_of, "%Y-%m-%d").date()
        if args.as_of else q.latest_computed_metrics_date()
    )
    if as_of is None:
        print("No computed_metrics partitions in the DB", file=sys.stderr)
        return 1
    metrics = q.computed_metrics_at(as_of)
    table = fit_report(metrics)
    if table.is_empty():
        print(f"No metrics rows at as_of={as_of}", file=sys.stderr)
        return 1
    print(f"Benchmark-fit report — computed_metrics as_of={as_of}")
    print(
        f"Classification: median rolling-3y R² < {LOW_CONFIDENCE_R2_THRESHOLD}"
        f" => {LOW_CONFIDENCE_LABEL}\n"
    )
    with pl.Config(tbl_rows=-1, tbl_hide_dataframe_shape=True):
        print(
            table.with_columns(
                pl.col("median_r2").round(3),
                pl.col("median_beta").round(3),
                pl.when(pl.col("low_confidence_alpha"))
                .then(pl.lit(LOW_CONFIDENCE_LABEL))
                .otherwise(pl.lit(""))
                .alias("flag"),
            ).drop("low_confidence_alpha")
        )
    return 0


def _cmd_refit(args: argparse.Namespace) -> int:
    as_of = (
        datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else None
    )
    candidates = [t.strip() for t in args.candidates.split(",") if t.strip()]
    table = refit_category(args.category, candidates, as_of=as_of)
    print(f"Refit evidence — category={args.category!r}, candidates={candidates}")
    print("(median of per-fund production rolling-3y regression medians)\n")
    with pl.Config(tbl_rows=-1, tbl_hide_dataframe_shape=True):
        print(
            table.with_columns(
                pl.col("median_r2").round(4),
                pl.col("median_beta").round(4),
                pl.col("median_alpha_ann").round(4),
            )
        )
    pick = best_candidate(table)
    if pick is None:
        print(
            "\nDecision: NO candidate reaches median R² >= 0.75 — keep the "
            "incumbent mapping and rely on benchmark_fit_low_confidence."
        )
    else:
        print(f"\nDecision: best candidate by median R² (>= 0.75): {pick!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="mode", required=True)

    rp = sub.add_parser("report", help="per-category median R²/beta table")
    rp.add_argument("--as-of", default=None, help="YYYY-MM-DD (default: latest)")
    rp.set_defaults(func=_cmd_report)

    rf = sub.add_parser("refit", help="re-fit one category vs candidate tickers")
    rf.add_argument("--category", required=True)
    rf.add_argument(
        "--candidates", required=True,
        help="comma-separated benchmark tickers, e.g. 'NIFTY 500 TRI,NIFTY 500 Value 50 TRI'",
    )
    rf.add_argument("--as-of", default=None, help="YYYY-MM-DD (default: latest)")
    rf.set_defaults(func=_cmd_refit)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
