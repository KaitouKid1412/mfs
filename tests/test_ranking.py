"""Tests for hard filters + composite_score_stage1 + composite_score_stage2.

Stage 1 is pure Performance & Consistency (8 metrics, no Phase 2 inputs,
no soft penalties). Stage 2 layers active_share + style_drift + PTR/AUM
soft penalties on top with downshifted Phase 1 weights.
"""

from datetime import date, datetime

import polars as pl

from mfs.rank.filters import apply_hard_filters
from mfs.rank.score import composite_score_stage1, composite_score_stage2
from mfs.rank.zscore import zscore_within_category


def _row(scheme, cat="Large Cap", **overrides):
    base = {
        "as_of_date": date(2026, 5, 15),
        "scheme_code": scheme,
        "canonical_category": cat,
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
        "data_quality_flag": "GOOD",
        "computed_at": datetime.utcnow(),
        "pipeline_version": "v1.0.0",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Hard filters
# ---------------------------------------------------------------------------


def test_filters_drop_insufficient_history():
    df = pl.DataFrame([
        _row("A"),
        _row("B", data_quality_flag="INSUFFICIENT_HISTORY"),
        _row("C", data_quality_flag="POOR"),
    ])
    out = apply_hard_filters(df)
    assert set(out["scheme_code"]) == {"A"}


def test_filters_drop_stale_with_reason_stale_nav():
    """A1-3: a row flagged STALE by the orchestrator staleness gate is dropped
    by apply_hard_filters, with contract-vocabulary reason STALE_NAV."""
    df = pl.DataFrame([
        _row("A"),
        _row("B", data_quality_flag="STALE"),
    ])
    out, excluded = apply_hard_filters(df, with_reasons=True)
    assert set(out["scheme_code"]) == {"A"}
    reason = excluded.filter(pl.col("scheme_code") == "B")["exclusion_reason"][0]
    assert reason == "STALE_NAV"


def test_filters_keep_marginal_metric_schemes():
    """HDFC-flexicap-style: capture < 1.0 and IR ≈ 0 but still informative.
    Floors should NOT drop these."""
    df = pl.DataFrame([
        _row("A"),
        _row("B", capture_efficiency=0.95, info_ratio_3y=0.10, r_squared_3y=0.99),
        _row("C", capture_efficiency=0.97, info_ratio_3y=-0.30),
    ])
    out = apply_hard_filters(df)
    assert set(out["scheme_code"]) == {"A", "B", "C"}


def test_filters_drop_catastrophic_floors():
    """Floor thresholds drop only catastrophically broken funds."""
    df = pl.DataFrame([
        _row("A"),
        _row("B", capture_efficiency=0.30),
        _row("C", info_ratio_3y=-1.50),
        _row("D", r_squared_3y=0.20),
        _row("E", beta_3y=2.10),
        _row("F", beta_3y=0.10),
    ])
    out = apply_hard_filters(df)
    assert set(out["scheme_code"]) == {"A"}


# ---------------------------------------------------------------------------
# composite_score_stage1 — Phase 1 only
# ---------------------------------------------------------------------------


def test_stage1_picks_best_in_category():
    df = pl.DataFrame([
        _row("A", alpha_3y_annualized=0.06, sortino_3y=1.8, info_ratio_3y=1.2),
        _row("B", alpha_3y_annualized=0.02, sortino_3y=0.9, info_ratio_3y=0.6),
        _row("C", alpha_3y_annualized=0.04, sortino_3y=1.4, info_ratio_3y=0.9),
    ])
    z = zscore_within_category(df)
    scored = composite_score_stage1(z)
    top = scored.sort("composite_score", descending=True).head(1)
    assert top["scheme_code"][0] == "A"


def test_stage1_uses_3y_proxy_for_missing_5y():
    """A scheme with no 5y data should reuse its 3y z-score in the 5y slot."""
    df = pl.DataFrame([
        _row("A"),
        _row("B", ret_5y_median=None, ret_5y_p25=None),
        _row("C", ret_5y_median=0.10, ret_5y_p25=0.05),
    ])
    z = zscore_within_category(df)
    scored = composite_score_stage1(z)
    b = scored.filter(pl.col("scheme_code") == "B").row(0, named=True)
    assert b["z_ret_5y_median"] == b["z_ret_3y_median"]
    assert b["z_ret_5y_p25"] == b["z_ret_3y_p25"]
    assert b["composite_score"] is not None


def test_stage1_handles_null_alpha_without_nan():
    """A scheme with null alpha must not produce a NaN composite (polars sorts
    NaN to the top in descending order)."""
    import math

    df = pl.DataFrame([
        _row("A"),
        _row("B", alpha_3y_annualized=None),
        _row("C", alpha_3y_annualized=0.02),
    ])
    z = zscore_within_category(df)
    scored = composite_score_stage1(z)
    b = scored.filter(pl.col("scheme_code") == "B").row(0, named=True)
    assert b["composite_score"] is not None
    assert not math.isnan(b["composite_score"])


def test_stage1_ignores_active_share_and_style_drift():
    """Stage 1 must not factor in Phase 2 metrics. Two schemes identical on
    Phase 1 but different on active_share + style_drift should tie."""
    df = pl.DataFrame([
        _row("A", active_share_median_1y=0.75, style_drift_3y=0.05),
        _row("B", active_share_median_1y=0.45, style_drift_3y=0.40),
    ])
    z = zscore_within_category(df)
    scored = composite_score_stage1(z)
    a = scored.filter(pl.col("scheme_code") == "A").row(0, named=True)
    b = scored.filter(pl.col("scheme_code") == "B").row(0, named=True)
    assert a["composite_score"] == b["composite_score"]


# ---------------------------------------------------------------------------
# composite_score_stage2 — Phase 1 + Phase 2 + soft penalties
# ---------------------------------------------------------------------------


def test_stage2_active_share_rewards_high_value_within_category():
    df = pl.DataFrame([
        _row("A", active_share_median_1y=0.75, style_drift_3y=0.10,
             ptr_latest=0.5, aum_impact_cost_days=1.0),
        _row("B", active_share_median_1y=0.45, style_drift_3y=0.10,
             ptr_latest=0.5, aum_impact_cost_days=1.0),
        _row("C", active_share_median_1y=0.55, style_drift_3y=0.10,
             ptr_latest=0.5, aum_impact_cost_days=1.0),
    ])
    z = zscore_within_category(df)
    scored = composite_score_stage2(z).sort("composite_score", descending=True)
    assert scored["scheme_code"][0] == "A"


def test_stage2_style_drift_acts_as_a_penalty():
    df = pl.DataFrame([
        _row("A", style_drift_3y=0.05, active_share_median_1y=0.6,
             ptr_latest=0.5, aum_impact_cost_days=1.0),
        _row("B", style_drift_3y=0.40, active_share_median_1y=0.6,
             ptr_latest=0.5, aum_impact_cost_days=1.0),
    ])
    z = zscore_within_category(df)
    scored = composite_score_stage2(z).sort("composite_score", descending=True)
    assert scored["scheme_code"][0] == "A"


def test_stage2_style_drift_null_does_not_penalize():
    import math
    df = pl.DataFrame([
        _row("A", style_drift_3y=None, active_share_median_1y=0.5,
             ptr_latest=0.5, aum_impact_cost_days=1.0),
        _row("B", style_drift_3y=0.30, active_share_median_1y=0.5,
             ptr_latest=0.5, aum_impact_cost_days=1.0),
        _row("C", style_drift_3y=0.10, active_share_median_1y=0.5,
             ptr_latest=0.5, aum_impact_cost_days=1.0),
    ])
    z = zscore_within_category(df)
    scored = composite_score_stage2(z)
    a = scored.filter(pl.col("scheme_code") == "A").row(0, named=True)
    assert a["composite_score"] is not None
    assert not math.isnan(a["composite_score"])


def test_stage2_aum_impact_penalty_above_threshold():
    """A scheme with high aum_impact_cost_days (>5) should rank below a peer
    with low impact cost, all else equal."""
    df = pl.DataFrame([
        _row("A", aum_impact_cost_days=15.0,
             active_share_median_1y=0.6, style_drift_3y=0.1, ptr_latest=0.5),
        _row("B", aum_impact_cost_days=1.0,
             active_share_median_1y=0.6, style_drift_3y=0.1, ptr_latest=0.5),
    ])
    z = zscore_within_category(df)
    scored = composite_score_stage2(z)
    a = scored.filter(pl.col("scheme_code") == "A").row(0, named=True)["composite_score"]
    b = scored.filter(pl.col("scheme_code") == "B").row(0, named=True)["composite_score"]
    assert b > a
