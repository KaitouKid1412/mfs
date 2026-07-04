"""Tests for Stage 2 — combined Phase 1 + Phase 2 re-rank.

Stage 2 takes the top-N per category from Stage 1 and re-ranks every pool
fund with the Stage 2 weights (Phase 1 weights downshifted to make room for
active_share + style_drift + PTR/AUM soft penalties).

D3 (A2-4): disclosure nulls are soft-neutral — nobody is dropped; missing
disclosures are flagged (``partial_disclosure_flag`` / ``missing_disclosures``),
the missing positive weight renormalizes across the present inputs, and a
calibrated fixed penalty applies once per missing metric that is live in the
fund's category pool.

A2-5: Phase 1 z-scores are carried from Stage 1's full universe (never
re-z-scored inside the pool); only the pool-scoped Phase 2 metrics are
z-scored here, with tiny pools (< MIN_COHORT_FOR_Z) getting neutral z.

A2-6: log-ramp AUM-impact penalty + the contract's liquidity_flag string
enum (OK | HIGH | SEVERE at 30/90 days-to-exit).
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from mfs.config import get_pipeline_config
from mfs.rank.score import (
    MISSING_DISCLOSURE_PENALTY_METRICS,
    composite_score_stage2,
)
from mfs.rank.stage2 import (
    DISCLOSURE_METRICS,
    LIQUIDITY_FLAG_HIGH_DAYS,
    LIQUIDITY_FLAG_SEVERE_DAYS,
    MIN_COHORT_FOR_Z,
    apply_stage2,
    disclosure_metrics_for_category,
    run,
)

# Carried Stage-1 z-score columns (A2-5: these arrive from Stage 1's
# full-universe statistics and must never be recomputed inside the pool).
PHASE1_Z_COLS = (
    "z_ret_3y_median",
    "z_ret_3y_p25",
    "z_ret_5y_median",
    "z_ret_5y_p25",
    "z_alpha_3y_annualized",
    "z_sortino_3y",
    "z_info_ratio_3y",
    "z_capture_efficiency",
)


def _missing_penalty() -> float:
    return float(get_pipeline_config().soft_penalties.missing_disclosure.penalty)


def _scored_row(
    scheme_code: str,
    category: str = "Flexi Cap",
    composite_score: float = 0.5,
    z: float = 0.0,
    **overrides,
):
    """Stage 1 scored row shape with full Phase 2 metrics and carried Stage 1
    z-scores by default. ``z`` seeds all eight carried Phase-1 z columns;
    overrides null out specific metrics or set individual z values."""
    base = {
        "as_of_date": date(2026, 5, 15),
        "canonical_category": category,
        "rank": 1,
        "scheme_code": scheme_code,
        "scheme_name": f"Test {scheme_code}",
        "composite_score": composite_score,
        # Carried Stage 1 z-scores (full-universe).
        **{c: z for c in PHASE1_Z_COLS},
        # Phase 1 raw metrics.
        "ret_3y_median": 0.18,
        "ret_3y_p25": 0.12,
        "ret_5y_median": 0.16,
        "ret_5y_p25": 0.10,
        "alpha_3y_annualized": 0.03,
        "sortino_3y": 1.2,
        "info_ratio_3y": 0.7,
        "capture_efficiency": 1.20,
        # Phase 2 metrics — default to filled so a row is fully disclosed
        # unless the test explicitly nulls one out.
        "style_drift_3y": 0.10,
        "active_share_median_1y": 0.45,
        "ptr_latest": 0.85,
        "aum_impact_cost_days": 0.30,
        "source_amc": "hdfc",
    }
    base.update(overrides)
    return base


def _df(rows):
    return pl.DataFrame(rows)


def _by_code(df: pl.DataFrame) -> dict[str, dict]:
    return {r["scheme_code"]: r for r in df.iter_rows(named=True)}


# ---------------------------------------------------------------------------
# DISCLOSURE_METRICS
# ---------------------------------------------------------------------------


def test_disclosure_metrics_match_for_every_category():
    for cat in ("Large Cap", "Flexi Cap", "Mid Cap", "Small Cap", "ELSS"):
        assert set(disclosure_metrics_for_category(cat)) == set(DISCLOSURE_METRICS)


def test_disclosure_metrics_cover_active_share_and_exclude_removed_columns():
    metrics = disclosure_metrics_for_category("Mid Cap")
    assert "stress_test_days_50pct" not in metrics
    assert "manager_tenure_years" not in metrics
    # D3: active_share IS a disclosure metric (flagged when null; it joins
    # the penalty set automatically once live in a pool).
    assert "active_share_median_1y" in metrics


def test_penalty_set_mirrors_disclosure_metrics():
    """score.MISSING_DISCLOSURE_PENALTY_METRICS is kept as a separate tuple
    to avoid a circular import — it must stay equal to DISCLOSURE_METRICS."""
    assert set(MISSING_DISCLOSURE_PENALTY_METRICS) == set(DISCLOSURE_METRICS)


# ---------------------------------------------------------------------------
# apply_stage2: pool size; D3 — nobody drops
# ---------------------------------------------------------------------------


def test_apply_stage2_pool_caps_at_top_n():
    """30 funds → only top-20 considered for Stage 2."""
    rows = [
        _scored_row(f"S{i:03d}", composite_score=1.0 - i * 0.01)
        for i in range(30)
    ]
    survivors, dropped, coverage = apply_stage2(
        _df(rows), aum_map={}, pool_size=20, final_size=5,
    )
    assert survivors.height == 5
    assert dropped.height == 0
    assert coverage.row(0, named=True)["n_considered"] == 20


def test_missing_ptr_kept_flagged_and_penalized():
    """D3: a fund with a null required-era metric is KEPT, flagged, and its
    composite is exactly its disclosed twin's minus the calibrated penalty."""
    rows = [
        _scored_row(f"F{i}", composite_score=0.9 - i * 0.01) for i in range(4)
    ]
    rows.append(_scored_row("D_twin", composite_score=0.5))
    rows.append(_scored_row("M_twin", composite_score=0.49, ptr_latest=None))
    survivors, dropped, _ = apply_stage2(_df(rows), aum_map={}, final_size=10)
    assert dropped.height == 0
    by = _by_code(survivors)
    assert "M_twin" in by  # kept, not dropped
    assert by["M_twin"]["partial_disclosure_flag"] is True
    assert by["M_twin"]["missing_disclosures"] == "ptr_latest"
    assert by["D_twin"]["partial_disclosure_flag"] is False
    assert by["D_twin"]["missing_disclosures"] == ""
    # PTR is live in this pool (5/6 disclosed) → fixed penalty, exactly once.
    assert by["M_twin"]["composite_score"] == pytest.approx(
        by["D_twin"]["composite_score"] - _missing_penalty(), abs=1e-12,
    )


def test_missing_disclosures_column_lists_every_null():
    rows = [_scored_row(f"F{i}") for i in range(5)]
    rows.append(_scored_row(
        "S_three_missing",
        ptr_latest=None,
        aum_impact_cost_days=None,
        active_share_median_1y=None,
    ))
    survivors, dropped, _ = apply_stage2(_df(rows), aum_map={}, final_size=10)
    assert dropped.height == 0
    assert survivors.height == 6
    miss = _by_code(survivors)["S_three_missing"]["missing_disclosures"]
    assert "ptr_latest" in miss
    assert "aum_impact_cost_days" in miss
    assert "active_share_median_1y" in miss
    assert "style_drift_3y" not in miss


def test_apply_stage2_active_share_null_flags_but_never_drops():
    """A fund with only active_share null (the current universe-wide state)
    survives; the flag is set regardless of pool liveness."""
    rows = [_scored_row("S_only_as_null", active_share_median_1y=None)]
    survivors, dropped, _ = apply_stage2(_df(rows), aum_map={}, final_size=5)
    assert survivors.height == 1
    assert dropped.height == 0
    row = survivors.row(0, named=True)
    assert row["partial_disclosure_flag"] is True
    assert row["missing_disclosures"] == "active_share_median_1y"


# ---------------------------------------------------------------------------
# Re-rank behavior: Stage 2 composite reorders survivors
# ---------------------------------------------------------------------------


def test_active_share_can_promote_in_stage2_rerank():
    """A fund whose carried Stage 1 z-scores are weaker but whose Phase 2
    active_share is much higher than peers should move up in the Stage 2
    ordering (pool >= MIN_COHORT_FOR_Z so live z-scoring runs)."""
    rows = [
        _scored_row("A", composite_score=0.50, z=-0.2, active_share_median_1y=0.90),
        _scored_row("B", composite_score=0.90, z=1.0, active_share_median_1y=0.50),
        _scored_row("C", composite_score=0.60, z=-0.5, active_share_median_1y=0.55),
        _scored_row("D", composite_score=0.70, z=0.0, active_share_median_1y=0.50),
        _scored_row("E", composite_score=0.65, z=0.0, active_share_median_1y=0.50),
    ]
    survivors, _, _ = apply_stage2(_df(rows), aum_map={}, final_size=5)
    codes = survivors.sort("stage2_rank")["scheme_code"].to_list()
    assert codes[0] == "B"
    assert codes.index("A") < codes.index("C")


def test_stage1_score_preserved_under_distinct_name():
    rows = [_scored_row("S1", composite_score=0.77)]
    survivors, _, _ = apply_stage2(_df(rows), aum_map={}, final_size=5)
    row = survivors.row(0, named=True)
    assert row["composite_score_stage1"] == 0.77
    assert "composite_score" in survivors.columns


# ---------------------------------------------------------------------------
# D3 scoring semantics (composite_score_stage2 direct)
# ---------------------------------------------------------------------------


def _z_frame(rows):
    """Frame for direct composite_score_stage2 calls: z columns are set
    explicitly by the caller (no zscore pass)."""
    return pl.DataFrame(rows, strict=False)


def _direct_row(code, *, z=0.4, z_active=0.0, active=0.5, ptr=0.5,
                style=0.10, z_style=0.0, aum_days=1.0):
    return {
        "scheme_code": code,
        "canonical_category": "Flexi Cap",
        **{c: z for c in PHASE1_Z_COLS},
        "z_active_share_median_1y": z_active,
        "z_style_drift_3y": z_style,
        "active_share_median_1y": active,
        "style_drift_3y": style,
        "ptr_latest": ptr,
        "aum_impact_cost_days": aum_days,
    }


def test_renormalization_scales_positive_weights():
    """A fund missing active_share in a live-active_share cohort has its
    positive z terms scaled by sum_all_pos/present_pos (0.85/0.75 with the
    live weight constants) — and pays the fixed penalty because the metric
    is live in the pool."""
    cfg = get_pipeline_config()
    w = cfg.composite_weights_stage2
    pos_all = sum(v for v in w.values() if v > 0)          # 0.85
    pos_phase1 = pos_all - w["active_share"]               # 0.75
    rows = [
        _direct_row("X", z=0.4, z_active=float("nan"), active=None),
        _direct_row("Y", z=0.4, z_active=0.0, active=0.50),
        _direct_row("Z1", z=0.0, z_active=1.0, active=0.60),
        _direct_row("Z2", z=0.0, z_active=-1.0, active=0.40),
        _direct_row("Z3", z=0.0, z_active=0.0, active=0.50),
    ]
    scored = _by_code(composite_score_stage2(_z_frame(rows)))
    pen = _missing_penalty()
    # Y: plain weighted sum, no scaling, no penalty.
    assert scored["Y"]["composite_score"] == pytest.approx(0.4 * pos_phase1, abs=1e-12)
    # X: positive terms scaled by pos_all/pos_phase1, minus the live-metric
    # missing-disclosure penalty (active_share is live: 4/5 disclosed).
    expected_x = 0.4 * pos_phase1 * (pos_all / pos_phase1) - pen
    assert scored["X"]["composite_score"] == pytest.approx(expected_x, abs=1e-12)


def test_dead_metric_no_uniform_penalty():
    """active_share null for the entire cohort (today's universe state) →
    no missing-disclosure penalty and no renormalization difference between
    funds. zscore/tiny-pool both emit neutral 0.0 z for a dead metric."""
    cfg = get_pipeline_config()
    pos_phase1 = sum(v for v in cfg.composite_weights_stage2.values() if v > 0) \
        - cfg.composite_weights_stage2["active_share"]
    rows = [
        _direct_row("A", z=0.4, z_active=0.0, active=None),
        _direct_row("B", z=0.4, z_active=0.0, active=None),
        _direct_row("C", z=-0.1, z_active=0.0, active=None),
    ]
    scored = _by_code(composite_score_stage2(_z_frame(rows)))
    # No penalty (metric dead in pool) and identical treatment of A and B.
    assert scored["A"]["composite_score"] == pytest.approx(0.4 * pos_phase1, abs=1e-12)
    assert scored["A"]["composite_score"] == scored["B"]["composite_score"]


def test_missing_never_better_than_disclosed_bad():
    """Missing scores like average-bad: below the threshold-disclosed twin,
    above the worst-disclosed twin (penalty calibrated to the median
    non-zero PTR penalty)."""
    rows = [
        _direct_row("T_threshold", z=0.0, ptr=1.5),
        _direct_row("M_missing", z=0.0, ptr=None),
        _direct_row("W_worst", z=0.0, ptr=3.0),
    ]
    scored = _by_code(composite_score_stage2(_z_frame(rows)))
    t = scored["T_threshold"]["composite_score"]
    m = scored["M_missing"]["composite_score"]
    w = scored["W_worst"]["composite_score"]
    pen = _missing_penalty()
    ptr_max = float(get_pipeline_config().soft_penalties.ptr.max_penalty)
    assert t > m > w
    assert m == pytest.approx(t - pen, abs=1e-12)
    assert w == pytest.approx(t - ptr_max, abs=1e-12)
    # The calibrated penalty can never exceed the disclosed-bad cap.
    assert pen < ptr_max


# ---------------------------------------------------------------------------
# A2-5: carried Phase-1 z-scores; pool-scoped Phase-2 z
# ---------------------------------------------------------------------------


def test_phase1_z_carried_not_recomputed():
    """The carried z_alpha values are deliberately NOT what pool-local
    re-z-scoring would produce (they don't have mean 0 / sd 1); survivors
    must retain them exactly."""
    carried = {"F0": 0.7, "F1": 0.65, "F2": 0.6, "F3": 0.55, "F4": 0.5, "F5": 0.45}
    rows = [
        _scored_row(code, composite_score=0.9 - i * 0.01,
                    z_alpha_3y_annualized=zval)
        for i, (code, zval) in enumerate(carried.items())
    ]
    survivors, _, _ = apply_stage2(_df(rows), aum_map={}, final_size=10)
    by = _by_code(survivors)
    for code, zval in carried.items():
        assert by[code]["z_alpha_3y_annualized"] == zval


def test_tiny_cohort_phase2_z_neutral():
    """N=2 category (the Manufacturing collapse case): Phase-2 z is neutral
    0.0 for both funds instead of N=2 noise; the carried Phase-1 z keeps
    full-universe information and drives the ordering."""
    assert 2 < MIN_COHORT_FOR_Z
    rows = [
        _scored_row("MEASURED", category="Manufacturing", composite_score=0.9,
                    z_alpha_3y_annualized=1.0, style_drift_3y=0.0405),
        _scored_row("SPARSE", category="Manufacturing", composite_score=0.8,
                    z_alpha_3y_annualized=0.0, style_drift_3y=None),
    ]
    survivors, _, _ = apply_stage2(_df(rows), aum_map={}, final_size=5)
    by = _by_code(survivors)
    assert by["MEASURED"]["z_style_drift_3y"] == 0.0
    assert by["SPARSE"]["z_style_drift_3y"] == 0.0
    # Carried z intact — the measured fund's alpha is not zeroed by the
    # 2-fund nanstd anymore.
    assert by["MEASURED"]["z_alpha_3y_annualized"] == 1.0
    assert by["MEASURED"]["stage2_rank"] == 1


def test_stage1_rank_matches_carried_composite():
    rows = [
        _scored_row(f"S{i}", composite_score=0.9 - i * 0.05) for i in range(6)
    ]
    survivors, _, _ = apply_stage2(_df(rows), aum_map={}, final_size=10)
    ordered = survivors.sort("stage1_rank")
    scores = ordered["composite_score_stage1"].to_list()
    assert scores == sorted(scores, reverse=True)
    assert ordered.row(0, named=True)["scheme_code"] == "S0"


# ---------------------------------------------------------------------------
# A2-6: liquidity_flag tiering
# ---------------------------------------------------------------------------


def test_liquidity_flag_tiers():
    rows = [
        _scored_row("OK_FUND", aum_impact_cost_days=12.0),
        _scored_row("HIGH_FUND", aum_impact_cost_days=45.0),
        _scored_row("HIGH_BOUNDARY", aum_impact_cost_days=LIQUIDITY_FLAG_HIGH_DAYS),
        _scored_row("SEVERE_BOUNDARY", aum_impact_cost_days=LIQUIDITY_FLAG_SEVERE_DAYS),
        _scored_row("SEVERE_FUND", aum_impact_cost_days=924.0),
        _scored_row("NULL_FUND", aum_impact_cost_days=None),
    ]
    survivors, _, _ = apply_stage2(_df(rows), aum_map={}, final_size=10)
    by = _by_code(survivors)
    assert by["OK_FUND"]["liquidity_flag"] == "OK"
    assert by["HIGH_FUND"]["liquidity_flag"] == "HIGH"
    assert by["HIGH_BOUNDARY"]["liquidity_flag"] == "HIGH"
    assert by["SEVERE_BOUNDARY"]["liquidity_flag"] == "SEVERE"
    assert by["SEVERE_FUND"]["liquidity_flag"] == "SEVERE"
    assert by["NULL_FUND"]["liquidity_flag"] is None


def test_severe_liquidity_pays_more_than_low_days_twin():
    """Log ramp discriminates: a 924-day fund carries the full cap while a
    ~12-day twin carries a small but non-zero penalty."""
    rows = [
        _scored_row(f"F{i}") for i in range(4)
    ]
    rows.append(_scored_row("SLOW", composite_score=0.5, aum_impact_cost_days=924.0))
    rows.append(_scored_row("FAST", composite_score=0.49, aum_impact_cost_days=12.0))
    survivors, _, _ = apply_stage2(_df(rows), aum_map={}, final_size=10)
    by = _by_code(survivors)
    acfg = get_pipeline_config().soft_penalties.aum_impact_cost
    expected_fast_pen = acfg.max_penalty * (
        math.log(12.0 / acfg.threshold_days)
        / math.log(acfg.saturation_days / acfg.threshold_days)
    )
    delta = by["FAST"]["composite_score"] - by["SLOW"]["composite_score"]
    # Twins are identical except days; baseline rows carry days=0.30 (no
    # penalty), so the composite gap is exactly pen(924) - pen(12).
    assert delta == pytest.approx(acfg.max_penalty - expected_fast_pen, abs=1e-9)


# ---------------------------------------------------------------------------
# Partial coverage + AUM
# ---------------------------------------------------------------------------


def test_partial_coverage_flag_when_survivors_below_final_size():
    rows = [
        _scored_row("S1", composite_score=0.9),
        _scored_row("S2", composite_score=0.8),
        _scored_row("S3", composite_score=0.7),
    ]
    survivors, _, _ = apply_stage2(_df(rows), aum_map={}, final_size=5)
    assert survivors.height == 3
    assert all(survivors["partial_coverage_flag"].to_list())


def test_no_partial_flag_when_survivors_fill_to_final_size():
    rows = [_scored_row(f"S{i}", composite_score=0.9 - i * 0.01) for i in range(5)]
    survivors, _, _ = apply_stage2(_df(rows), aum_map={}, final_size=5)
    assert survivors.height == 5
    assert not any(survivors["partial_coverage_flag"].to_list())


def test_pool_and_final_none_keep_every_fund():
    """pool_size=None / final_size=None → the full-universe default: no fund is
    trimmed and the 'thin cohort' partial flag never fires."""
    rows = [_scored_row(f"S{i}", composite_score=0.9 - i * 0.01) for i in range(30)]
    survivors, _, _ = apply_stage2(
        _df(rows), aum_map={}, pool_size=None, final_size=None,
    )
    assert survivors.height == 30  # nothing dropped
    assert not any(survivors["partial_coverage_flag"].to_list())
    # stage2_rank is dense 1..N over the whole category.
    assert sorted(survivors["stage2_rank"].to_list()) == list(range(1, 31))


def test_aum_attached_from_aum_map():
    rows = [_scored_row("S_with_aum"), _scored_row("S_no_aum")]
    aum_map = {"S_with_aum": 1234.5}
    survivors, _, _ = apply_stage2(_df(rows), aum_map=aum_map, final_size=5)
    by_code = _by_code(survivors)
    assert by_code["S_with_aum"]["aum_crore"] == 1234.5
    assert by_code["S_no_aum"]["aum_crore"] is None


# ---------------------------------------------------------------------------
# E1 — cohort-size context columns on survivors
# ---------------------------------------------------------------------------


def test_survivors_carry_cohort_size_columns():
    """Every survivor row carries n_considered / n_survived for its own
    category, so 'best of 3' is distinguishable from 'only survivor of 1'
    (the FMCG cohort-of-one case)."""
    rows = [
        _scored_row("L1", category="Large Cap", composite_score=0.9),
        _scored_row("L2", category="Large Cap", composite_score=0.8),
        _scored_row("L3", category="Large Cap", composite_score=0.7),
        _scored_row("F1", category="Flexi Cap", composite_score=0.6),
    ]
    survivors, _, coverage = apply_stage2(_df(rows), aum_map={}, final_size=2)
    by = _by_code(survivors)
    assert by["L1"]["n_considered"] == 3
    assert by["L1"]["n_survived"] == 2
    assert "L3" not in by  # cut by final_size
    assert by["F1"]["n_considered"] == 1
    assert by["F1"]["n_survived"] == 1
    # The survivor columns mirror coverage.csv exactly.
    cov = {
        r["canonical_category"]: r for r in coverage.iter_rows(named=True)
    }
    assert cov["Large Cap"]["n_considered"] == 3
    assert cov["Large Cap"]["n_survived"] == 2
    assert cov["Flexi Cap"]["n_considered"] == 1
    assert cov["Flexi Cap"]["n_survived"] == 1


# ---------------------------------------------------------------------------
# Coverage report shape
# ---------------------------------------------------------------------------


def test_coverage_row_per_category():
    rows = [
        _scored_row("S_LC_1", category="Large Cap", composite_score=0.9),
        _scored_row("S_LC_2", category="Large Cap", composite_score=0.8,
                    ptr_latest=None),
        _scored_row("S_FC_1", category="Flexi Cap", composite_score=0.7),
    ]
    _, _, coverage = apply_stage2(_df(rows), aum_map={})
    cats = set(coverage["canonical_category"].to_list())
    assert cats == {"Large Cap", "Flexi Cap"}
    lc = coverage.filter(pl.col("canonical_category") == "Large Cap").row(0, named=True)
    assert lc["n_considered"] == 2
    assert lc["n_survived"] == 2          # D3: nobody drops
    assert lc["n_dropped"] == 0           # structurally 0
    assert lc["n_partial_disclosure"] == 1
    assert lc["pct_survived"] == 100.0
    assert lc["top_dropped_amc"] is None  # vestigial since D3


def test_coverage_aum_rollup_correct():
    rows = [
        _scored_row("S_full", composite_score=0.9),
        _scored_row("S_partial", composite_score=0.8, ptr_latest=None),
    ]
    aum_map = {"S_full": 100.0, "S_partial": 200.0}
    _, _, coverage = apply_stage2(_df(rows), aum_map=aum_map)
    row = coverage.row(0, named=True)
    # D3: the partial-disclosure fund survives, so its AUM survives too.
    assert row["aum_considered_crore"] == 300.0
    assert row["aum_survived_crore"] == 300.0
    assert row["aum_dropped_crore"] == 0.0


def test_coverage_counts_partial_disclosure_per_category():
    rows = [
        _scored_row("S1", source_amc="amc_a", style_drift_3y=None, composite_score=0.9),
        _scored_row("S2", source_amc="amc_a", ptr_latest=None, composite_score=0.8),
        _scored_row("S3", source_amc="amc_b", aum_impact_cost_days=None, composite_score=0.7),
        _scored_row("S4", source_amc="amc_b", composite_score=0.6),
    ]
    _, _, coverage = apply_stage2(_df(rows), aum_map={})
    row = coverage.row(0, named=True)
    assert row["n_partial_disclosure"] == 3
    assert row["n_dropped"] == 0


# ---------------------------------------------------------------------------
# End-to-end run() writes artifacts
# ---------------------------------------------------------------------------


def test_run_writes_artifacts(tmp_path):
    rows = [
        _scored_row("S1", category="Large Cap", composite_score=0.9),
        _scored_row("S2", category="Large Cap", composite_score=0.8,
                    ptr_latest=None),
        _scored_row("S3", category="Flexi Cap", composite_score=0.7),
    ]
    result = run(_df(rows), aum_map={"S1": 100.0}, output_dir=tmp_path)

    assert Path(result["dropped_file"]).exists()
    assert Path(result["coverage_file"]).exists()
    assert (tmp_path / "stage2" / "Large_Cap.csv").exists()
    assert (tmp_path / "stage2" / "Flexi_Cap.csv").exists()
    assert (tmp_path / "stage2" / "mf_report.csv").exists()
    # D3: every fund survives; dropped.csv is vestigial header-only.
    assert result["n_survivors"] == 3
    assert result["n_dropped"] == 0
    assert pl.read_csv(result["dropped_file"]).height == 0
    # Contract columns flow into the per-category CSVs.
    lc = pl.read_csv(tmp_path / "stage2" / "Large_Cap.csv")
    for col in ("missing_disclosures", "partial_disclosure_flag",
                "liquidity_flag", "aum_impact_cost_days",
                "n_considered", "n_survived"):
        assert col in lc.columns
    # E3 gap-close: the consolidated mf_report carries the contract columns
    # too (it is what stage 3 / the investor report consume).
    report = pl.read_csv(tmp_path / "stage2" / "mf_report.csv")
    for col in ("liquidity_flag", "n_considered", "n_survived"):
        assert col in report.columns


def test_run_handles_empty_input(tmp_path):
    result = run(pl.DataFrame(), aum_map={}, output_dir=tmp_path)
    assert Path(result["dropped_file"]).exists()
    assert Path(result["coverage_file"]).exists()
    assert result["n_survivors"] == 0
    assert result["n_dropped"] == 0


# ---------------------------------------------------------------------------
# A2-11 — deterministic tie-break: (composite desc, scheme_code asc)
# ---------------------------------------------------------------------------


def test_tied_composites_order_by_scheme_code():
    """Two byte-identical funds (same Stage 1 composite, same Phase 2
    metrics → same Stage 2 composite) must rank by scheme_code asc,
    regardless of input row order."""
    twin_a = _scored_row("T1", composite_score=0.5)
    twin_b = _scored_row("T9", composite_score=0.5)
    twin_b["scheme_name"] = twin_a["scheme_name"]  # fully identical but code

    for rows in ([twin_a, twin_b], [twin_b, twin_a]):
        survivors, _, _ = apply_stage2(_df(rows), aum_map={})
        ordered = survivors.sort("stage2_rank")["scheme_code"].to_list()
        assert ordered == ["T1", "T9"]
        # The tie is real: identical Stage 2 composites.
        scores = set(survivors["composite_score"].to_list())
        assert len(scores) == 1


# ---------------------------------------------------------------------------
# C2 — TER breaks an exact composite tie (lower TER wins), display column
# ---------------------------------------------------------------------------


def test_ter_breaks_exact_composite_tie():
    """Two funds with an identical Stage 2 composite are ordered by LOWER TER
    first, overriding the scheme_code backstop. T9 has the LARGER scheme_code
    (would lose on scheme_code) but the LOWER TER (so it must win)."""
    twin_a = _scored_row("T1", composite_score=0.5)  # smaller code, higher TER
    twin_b = _scored_row("T9", composite_score=0.5)  # larger code, lower TER
    twin_b["scheme_name"] = twin_a["scheme_name"]
    ter_map = {"T1": 1.50, "T9": 0.40}
    for rows in ([twin_a, twin_b], [twin_b, twin_a]):
        survivors, _, _ = apply_stage2(_df(rows), aum_map={}, ter_map=ter_map)
        ordered = survivors.sort("stage2_rank")["scheme_code"].to_list()
        assert ordered == ["T9", "T1"]
        by = _by_code(survivors)
        assert by["T9"]["ter_pct"] == 0.40
        assert by["T1"]["ter_pct"] == 1.50


def test_ter_absent_keeps_scheme_code_order():
    """An all-null ter_pct (no ter_map / unmatched) is a no-op: the column is
    present for display but ordering falls through to scheme_code as before."""
    twin_a = _scored_row("T1", composite_score=0.5)
    twin_b = _scored_row("T9", composite_score=0.5)
    twin_b["scheme_name"] = twin_a["scheme_name"]
    survivors, _, _ = apply_stage2(_df([twin_b, twin_a]), aum_map={}, ter_map={})
    assert survivors.sort("stage2_rank")["scheme_code"].to_list() == ["T1", "T9"]
    assert survivors["ter_pct"].null_count() == survivors.height


def _csv_bytes(stage_dir: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(stage_dir.glob("*.csv"))}


def test_run_byte_identical_across_runs(tmp_path):
    """A2-11 acceptance: re-running Stage 2 on the same partition — with the
    input row order perturbed — produces byte-identical CSVs."""
    rows = [
        _scored_row("S1", category="Large Cap", composite_score=0.9),
        _scored_row("S2", category="Large Cap", composite_score=0.5),
        _scored_row("S3", category="Large Cap", composite_score=0.5),  # tie w/ S2
        _scored_row("S4", category="Flexi Cap", composite_score=0.7),
        _scored_row("S5", category="Flexi Cap", composite_score=0.7),  # tie w/ S4
    ]
    aum_map = {c: 100.0 for c in ("S1", "S2", "S3", "S4", "S5")}
    run(_df(rows), aum_map=aum_map, output_dir=tmp_path / "a")
    run(_df(list(reversed(rows))), aum_map=aum_map, output_dir=tmp_path / "b")
    a = _csv_bytes(tmp_path / "a" / "stage2")
    b = _csv_bytes(tmp_path / "b" / "stage2")
    assert set(a) == set(b) and a == b
