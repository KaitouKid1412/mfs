"""Tests for the PTR soft-penalty wiring (Phase 2.2.H)."""

from __future__ import annotations

from datetime import date, datetime

import polars as pl

from mfs.rank.score import composite_score_stage2 as composite_score
from mfs.rank.zscore import zscore_within_category


def _row(scheme, **overrides):
    base = {
        "as_of_date": date(2026, 5, 15),
        "scheme_code": scheme,
        "canonical_category": "Large Cap",
        "benchmark_ticker": "NIFTY 100 TRI",
        "ret_3y_median": 0.18,
        "ret_3y_p25": 0.12,
        "ret_5y_median": 0.16,
        "ret_5y_p25": 0.10,
        "alpha_3y_annualized": 0.03,
        "alpha_3y_tstat": 1.8,
        "sortino_3y": 1.2,
        "info_ratio_3y": 0.7,
        "capture_up": 1.05,
        "capture_down": 0.85,
        "capture_efficiency": 1.235,
        "r_squared_3y": 0.82,
        "beta_3y": 0.95,
        "beta_3y_std": None,
        "r_squared_3y_mean": None,
        "style_drift_3y": None,
        "active_share_median_1y": None,
        "ptr_latest": None,
        "aum_impact_cost_days": None,
        "stress_test_days_50pct": None,
        "manager_tenure_years": None,
        "data_quality_flag": "GOOD",
        "computed_at": datetime.utcnow(),
        "pipeline_version": "v2.2.0",
    }
    base.update(overrides)
    return base


def _score_with_ptr(df, *, enabled, max_penalty=0.10, threshold=1.50):
    from mfs.config import get_pipeline_config
    cfg = get_pipeline_config()
    saved = (
        cfg.soft_penalties.ptr.enabled,
        cfg.soft_penalties.ptr.max_penalty,
        cfg.soft_penalties.ptr.threshold,
    )
    try:
        cfg.soft_penalties.ptr.enabled = enabled
        cfg.soft_penalties.ptr.max_penalty = max_penalty
        cfg.soft_penalties.ptr.threshold = threshold
        z = zscore_within_category(df)
        return composite_score(z)
    finally:
        (
            cfg.soft_penalties.ptr.enabled,
            cfg.soft_penalties.ptr.max_penalty,
            cfg.soft_penalties.ptr.threshold,
        ) = saved


def test_ptr_below_threshold_no_penalty():
    """PTR < 1.50 should produce zero deduction."""
    df = pl.DataFrame([_row("A", ptr_latest=1.0), _row("B", ptr_latest=1.2)])
    on = _score_with_ptr(df, enabled=True)
    off = _score_with_ptr(df, enabled=False)
    for code in ("A", "B"):
        a = on.filter(pl.col("scheme_code") == code).row(0, named=True)["composite_score"]
        b = off.filter(pl.col("scheme_code") == code).row(0, named=True)["composite_score"]
        assert abs(a - b) < 1e-12


def test_ptr_at_threshold_no_penalty():
    """PTR exactly at threshold → zero deduction (boundary)."""
    df = pl.DataFrame([_row("A", ptr_latest=1.50), _row("B", ptr_latest=1.0)])
    on = _score_with_ptr(df, enabled=True)
    off = _score_with_ptr(df, enabled=False)
    a_on = on.filter(pl.col("scheme_code") == "A").row(0, named=True)["composite_score"]
    a_off = off.filter(pl.col("scheme_code") == "A").row(0, named=True)["composite_score"]
    assert abs(a_on - a_off) < 1e-9


def test_ptr_kicks_in_above_threshold():
    """PTR=2.25 with threshold=1.50, max=0.10: excess=0.75, ramp=0.5, penalty=0.05."""
    df = pl.DataFrame([_row("A", ptr_latest=2.25), _row("B", ptr_latest=1.0)])
    on = _score_with_ptr(df, enabled=True, max_penalty=0.10, threshold=1.50)
    off = _score_with_ptr(df, enabled=False)
    a_on = on.filter(pl.col("scheme_code") == "A").row(0, named=True)["composite_score"]
    a_off = off.filter(pl.col("scheme_code") == "A").row(0, named=True)["composite_score"]
    assert abs((a_off - a_on) - 0.05) < 1e-9


def test_ptr_caps_at_max_penalty():
    """PTR=10.0 (extreme): excess=8.5, ramp=5.67 — would suggest a 0.567 deduction
    but the cap clips it to max_penalty=0.10."""
    df = pl.DataFrame([_row("A", ptr_latest=10.0), _row("B", ptr_latest=1.0)])
    on = _score_with_ptr(df, enabled=True, max_penalty=0.10, threshold=1.50)
    off = _score_with_ptr(df, enabled=False)
    a_on = on.filter(pl.col("scheme_code") == "A").row(0, named=True)["composite_score"]
    a_off = off.filter(pl.col("scheme_code") == "A").row(0, named=True)["composite_score"]
    assert abs((a_off - a_on) - 0.10) < 1e-9


def test_ptr_null_no_penalty():
    """Null PTR (no data) → exempt from penalty."""
    df = pl.DataFrame([_row("A", ptr_latest=None), _row("B", ptr_latest=1.0)])
    on = _score_with_ptr(df, enabled=True)
    off = _score_with_ptr(df, enabled=False)
    a_on = on.filter(pl.col("scheme_code") == "A").row(0, named=True)["composite_score"]
    a_off = off.filter(pl.col("scheme_code") == "A").row(0, named=True)["composite_score"]
    assert abs(a_on - a_off) < 1e-12


def test_ptr_exempt_category_no_penalty():
    """A2-10: Balanced Advantage PTR=3.0 — multi-x turnover is structural
    arbitrage mechanics there, so the ramp must not fire."""
    df = pl.DataFrame([
        _row("A", canonical_category="Balanced Advantage", ptr_latest=3.0),
        _row("B", canonical_category="Balanced Advantage", ptr_latest=1.0),
    ])
    on = _score_with_ptr(df, enabled=True)
    off = _score_with_ptr(df, enabled=False)
    a_on = on.filter(pl.col("scheme_code") == "A").row(0, named=True)["composite_score"]
    a_off = off.filter(pl.col("scheme_code") == "A").row(0, named=True)["composite_score"]
    assert abs(a_on - a_off) < 1e-12


def test_ptr_non_exempt_category_still_penalized():
    """A2-10 control: Mid Cap PTR=3.0 → excess=1.5, ramp=1.0, full
    max_penalty=0.10 — the exemption is category-scoped, not global."""
    df = pl.DataFrame([
        _row("A", canonical_category="Mid Cap", ptr_latest=3.0),
        _row("B", canonical_category="Mid Cap", ptr_latest=1.0),
    ])
    on = _score_with_ptr(df, enabled=True, max_penalty=0.10, threshold=1.50)
    off = _score_with_ptr(df, enabled=False)
    a_on = on.filter(pl.col("scheme_code") == "A").row(0, named=True)["composite_score"]
    a_off = off.filter(pl.col("scheme_code") == "A").row(0, named=True)["composite_score"]
    assert abs((a_off - a_on) - 0.10) < 1e-9


def test_ptr_exempt_categories_match_yaml():
    """A2-10 drift guard: the SoftPenaltiesConfig code default must equal the
    configs/pipeline.yaml value so neither side can silently diverge."""
    from mfs.config import SoftPenaltiesConfig, get_pipeline_config

    expected = ["Balanced Advantage", "Equity Savings"]
    assert SoftPenaltiesConfig().ptr_penalty_exempt_categories == expected
    assert (
        get_pipeline_config().soft_penalties.ptr_penalty_exempt_categories
        == expected
    )


def test_ptr_disabled_flag_zeros_everything():
    """High PTR with disabled flag → no deduction."""
    df = pl.DataFrame([_row("A", ptr_latest=5.0), _row("B", ptr_latest=5.0)])
    on = _score_with_ptr(df, enabled=True)
    off = _score_with_ptr(df, enabled=False)
    a_on = on.filter(pl.col("scheme_code") == "A").row(0, named=True)["composite_score"]
    a_off = off.filter(pl.col("scheme_code") == "A").row(0, named=True)["composite_score"]
    # Both rows have the same PTR; if both get penalized equally, the difference
    # is 0 on enabled. The cleaner check: a_off > a_on when penalty kicks in.
    assert a_off > a_on  # disabled gives higher score (no deduction)
