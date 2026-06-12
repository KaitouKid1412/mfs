"""D4 — benchmark-fit diagnostic: threshold classification, refit candidate
selection on synthetic series, and the mf_report flag-join helper. No DB."""

from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from mfs.rank.shortlist import (
    LOW_CONFIDENCE_R2_THRESHOLD,
    category_benchmark_fit_flags,
)

_TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "benchmark_fit.py"
_spec = importlib.util.spec_from_file_location("benchmark_fit", _TOOL_PATH)
benchmark_fit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(benchmark_fit)


# ---------------------------------------------------------------------------
# Threshold classification
# ---------------------------------------------------------------------------


def test_low_confidence_threshold_boundary():
    assert LOW_CONFIDENCE_R2_THRESHOLD == 0.80
    assert benchmark_fit.is_low_confidence(0.79) is True
    assert benchmark_fit.is_low_confidence(0.80) is False
    assert benchmark_fit.is_low_confidence(None) is None


def test_category_flags_threshold_and_nulls():
    metrics = pl.DataFrame({
        "canonical_category": ["Bad", "Bad", "Edge", "Good", "NullCat"],
        "r_squared_3y": [0.70, 0.74, 0.80, 0.95, None],
    })
    flags = category_benchmark_fit_flags(metrics)
    d = dict(
        zip(flags["canonical_category"], flags["benchmark_fit_low_confidence"])
    )
    assert d["Bad"] is True          # median 0.72 < 0.80
    assert d["Edge"] is False        # exactly 0.80 -> not flagged
    assert d["Good"] is False
    assert d["NullCat"] is None      # no regressable funds -> null, not false


def test_category_flags_empty_frame():
    flags = category_benchmark_fit_flags(pl.DataFrame())
    assert flags.is_empty()
    assert "benchmark_fit_low_confidence" in flags.columns


# ---------------------------------------------------------------------------
# report mode (pure frame)
# ---------------------------------------------------------------------------


def test_fit_report_sorted_ascending_with_flag():
    metrics = pl.DataFrame({
        "canonical_category": ["Good", "Good", "Bad", "Bad", "Bad"],
        "r_squared_3y": [0.95, 0.93, 0.55, 0.61, 0.58],
        "beta_3y": [0.95, 0.97, 0.60, 0.55, 0.58],
    })
    rep = benchmark_fit.fit_report(metrics)
    assert rep["canonical_category"].to_list() == ["Bad", "Good"]
    bad = rep.row(0, named=True)
    assert bad["n"] == 3
    assert abs(bad["median_r2"] - 0.58) < 1e-12
    assert bad["low_confidence_alpha"] is True
    good = rep.row(1, named=True)
    assert good["low_confidence_alpha"] is False


# ---------------------------------------------------------------------------
# refit mode on synthetic series: fund constructed from indexA must pick
# indexA over an independent indexB by median R².
# ---------------------------------------------------------------------------


def _synthetic_alignments() -> dict[tuple[str, str], pl.DataFrame]:
    rng = np.random.default_rng(7)
    n = int(365.25 * 3.3)
    dates = [date(2021, 1, 1) + timedelta(days=i) for i in range(n)]
    ret_a = rng.normal(0.0004, 0.010, n)
    ret_b = rng.normal(0.0004, 0.010, n)
    frames: dict[tuple[str, str], pl.DataFrame] = {}
    for code in ("F1", "F2"):
        fund = 0.9 * ret_a + rng.normal(0.0, 0.002, n)
        for ticker, bench in (("IDX A", ret_a), ("IDX B", ret_b)):
            frames[(code, ticker)] = pl.DataFrame({
                "date": dates,
                "fund_log_ret": fund,
                "bench_log_ret": bench,
                "rate_daily_log": [0.0002] * n,
            })
    return frames


def test_refit_picks_generating_index():
    frames = _synthetic_alignments()
    table = benchmark_fit.refit_category(
        "Synthetic",
        ["IDX A", "IDX B"],
        scheme_codes=["F1", "F2"],
        align_fn=lambda code, ticker: frames[(code, ticker)],
        available_fn=lambda ticker: True,
        step="1w",
    )
    rows = {r["candidate_ticker"]: r for r in table.iter_rows(named=True)}
    assert rows["IDX A"]["n_regressable"] == 2
    assert rows["IDX A"]["median_r2"] > rows["IDX B"]["median_r2"]
    assert rows["IDX A"]["median_r2"] > 0.85
    assert benchmark_fit.best_candidate(table) == "IDX A"


def test_refit_unavailable_candidate_reported_not_fit():
    frames = _synthetic_alignments()
    table = benchmark_fit.refit_category(
        "Synthetic",
        ["IDX A", "MISSING TRI"],
        scheme_codes=["F1"],
        align_fn=lambda code, ticker: frames[(code, ticker)],
        available_fn=lambda ticker: ticker == "IDX A",
        step="1w",
    )
    missing = table.filter(pl.col("candidate_ticker") == "MISSING TRI").row(
        0, named=True
    )
    assert missing["available"] is False
    assert missing["median_r2"] is None
    # selection never adopts an unavailable candidate
    assert benchmark_fit.best_candidate(
        table.filter(~pl.col("available"))
    ) is None


def test_best_candidate_requires_min_r2():
    table = pl.DataFrame({
        "candidate_ticker": ["T1", "T2"],
        "available": [True, True],
        "n_funds": [4, 4],
        "n_regressable": [4, 4],
        "median_r2": [0.70, 0.66],
        "median_beta": [0.8, 0.7],
        "median_alpha_ann": [0.01, 0.02],
    })
    assert benchmark_fit.best_candidate(table) is None         # nothing >= 0.75
    assert benchmark_fit.best_candidate(table, min_r2=0.65) == "T1"


# ---------------------------------------------------------------------------
# mf_report column join (rank-side): the flag lands on stage2/stage3 rows
# ---------------------------------------------------------------------------


def test_mf_report_flag_join():
    metrics = pl.DataFrame({
        "canonical_category": ["Value", "Value", "Large Cap", "Large Cap"],
        "r_squared_3y": [0.72, 0.74, 0.95, 0.94],
    })
    flags = category_benchmark_fit_flags(metrics)
    report = pl.DataFrame({
        "scheme_code": ["V1", "L1"],
        "canonical_category": ["Value", "Large Cap"],
        "composite_score": [0.5, 0.4],
    }).join(flags, on="canonical_category", how="left")
    d = dict(zip(report["scheme_code"], report["benchmark_fit_low_confidence"]))
    assert d["V1"] is True
    assert d["L1"] is False
