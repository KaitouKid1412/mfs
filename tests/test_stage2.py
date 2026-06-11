"""Tests for Stage 2 — combined Phase 1 + Phase 2 re-rank.

Stage 2 takes the top-N per category from Stage 1, drops funds missing any
Phase 2 metric, and **re-ranks** the survivors with the Stage 2 weights
(Phase 1 weights downshifted to make room for active_share + style_drift +
PTR/AUM soft penalties).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl

from mfs.rank.stage2 import (
    REQUIRED_METRICS_ALL,
    apply_stage2,
    required_metrics_for_category,
    run,
)


def _scored_row(
    scheme_code: str,
    category: str = "Flexi Cap",
    composite_score: float = 0.5,
    **overrides,
):
    """Stage 1 scored row shape with full Phase 2 metrics by default. The
    test customizes overrides to null out specific Phase 2 metrics or to
    boost a metric to drive Stage 2 re-rank order."""
    base = {
        "as_of_date": date(2026, 5, 15),
        "canonical_category": category,
        "rank": 1,
        "scheme_code": scheme_code,
        "scheme_name": f"Test {scheme_code}",
        "composite_score": composite_score,
        # Phase 1 raw metrics (also needed by re-z-score during Stage 2).
        "ret_3y_median": 0.18,
        "ret_3y_p25": 0.12,
        "ret_5y_median": 0.16,
        "ret_5y_p25": 0.10,
        "alpha_3y_annualized": 0.03,
        "sortino_3y": 1.2,
        "info_ratio_3y": 0.7,
        "capture_efficiency": 1.20,
        # Phase 2 metrics — default to filled so a row passes unless the
        # test explicitly nulls one out.
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


# ---------------------------------------------------------------------------
# required_metrics_for_category
# ---------------------------------------------------------------------------


def test_required_metrics_match_required_all():
    for cat in ("Large Cap", "Flexi Cap", "Mid Cap", "Small Cap", "ELSS"):
        assert set(required_metrics_for_category(cat)) == set(REQUIRED_METRICS_ALL)


def test_required_metrics_excludes_removed_columns():
    req = required_metrics_for_category("Mid Cap")
    assert "stress_test_days_50pct" not in req
    assert "manager_tenure_years" not in req
    # active_share is a soft signal, not a survival gate (needs >=3 monthly
    # snapshots we don't yet have); it must NOT be required.
    assert "active_share_median_1y" not in req


# ---------------------------------------------------------------------------
# apply_stage2: pool size and survivor selection
# ---------------------------------------------------------------------------


def test_apply_stage2_pool_caps_at_top_n():
    """30 funds → only top-20 considered for Stage 2."""
    rows = [
        _scored_row(f"S{i:03d}", composite_score=1.0 - i * 0.01)
        for i in range(30)
    ]
    survivors, dropped, _ = apply_stage2(
        _df(rows), aum_map={}, pool_size=20, final_size=5,
    )
    # All 20 pool members have full data → re-ranked top-5 survives.
    assert survivors.height == 5
    # Dropped only counts funds inside the pool that failed eligibility.
    assert dropped.height == 0


def test_apply_stage2_drops_fund_with_null_required_metric():
    rows = [
        _scored_row("S_good", composite_score=0.9),
        # active_share is optional → a fund missing only it must SURVIVE.
        _scored_row("S_no_active_share", active_share_median_1y=None, composite_score=0.8),
        # aum_impact is still required → this fund is dropped.
        _scored_row("S_no_aum_impact", aum_impact_cost_days=None, composite_score=0.7),
    ]
    survivors, dropped, _ = apply_stage2(_df(rows), aum_map={}, final_size=5)
    assert set(survivors["scheme_code"].to_list()) == {"S_good", "S_no_active_share"}
    assert set(dropped["scheme_code"].to_list()) == {"S_no_aum_impact"}


def test_apply_stage2_missing_metrics_column_lists_every_null():
    rows = [
        _scored_row(
            "S_two_missing",
            ptr_latest=None,
            aum_impact_cost_days=None,
        ),
    ]
    survivors, dropped, _ = apply_stage2(_df(rows), aum_map={}, final_size=5)
    assert survivors.is_empty() or survivors.height == 0
    miss = dropped.row(0, named=True)["missing_metrics"]
    assert "ptr_latest" in miss
    assert "aum_impact_cost_days" in miss


def test_apply_stage2_active_share_null_does_not_drop():
    """A fund with every required metric present but a NULL active_share
    (the current universe-wide state) must survive Stage 2."""
    rows = [_scored_row("S_only_as_null", active_share_median_1y=None)]
    survivors, dropped, _ = apply_stage2(_df(rows), aum_map={}, final_size=5)
    assert survivors.height == 1
    assert dropped.is_empty() or dropped.height == 0


# ---------------------------------------------------------------------------
# Re-rank behavior: Stage 2 composite reorders survivors
# ---------------------------------------------------------------------------


def test_active_share_can_promote_in_stage2_rerank():
    """A fund whose Stage 1 composite is lower but whose Phase 2 active_share
    is much higher than peers should move up in the Stage 2 ordering.

    A has weaker returns but much higher active_share; B and C have the
    same returns. With Phase 2 weights (active_share=0.10), A should still
    rank above C (worst returns AND mid active_share).
    """
    rows = [
        _scored_row("A", composite_score=0.50,
                    ret_3y_median=0.16, ret_3y_p25=0.10,
                    ret_5y_median=0.14, ret_5y_p25=0.08,
                    active_share_median_1y=0.80),
        _scored_row("B", composite_score=0.90,
                    ret_3y_median=0.22, ret_3y_p25=0.14,
                    ret_5y_median=0.20, ret_5y_p25=0.12,
                    active_share_median_1y=0.50),
        _scored_row("C", composite_score=0.60,
                    ret_3y_median=0.10, ret_3y_p25=0.05,
                    ret_5y_median=0.08, ret_5y_p25=0.03,
                    active_share_median_1y=0.55),
    ]
    survivors, _, _ = apply_stage2(_df(rows), aum_map={}, final_size=5)
    codes = survivors.sort("stage2_rank")["scheme_code"].to_list()
    # B has the best returns → still rank 1. A's active_share lift should
    # put it above C, which is weak on returns.
    assert codes[0] == "B"
    assert codes.index("A") < codes.index("C")


def test_stage1_score_preserved_under_distinct_name():
    """Stage 2 overwrites ``composite_score`` with its own — but Stage 1's
    number should still be queryable as ``composite_score_stage1``."""
    rows = [_scored_row("S1", composite_score=0.77)]
    survivors, _, _ = apply_stage2(_df(rows), aum_map={}, final_size=5)
    row = survivors.row(0, named=True)
    assert row["composite_score_stage1"] == 0.77
    # The new composite is recomputed (≠ the original 0.77).
    assert "composite_score" in survivors.columns


# ---------------------------------------------------------------------------
# Partial coverage + AUM
# ---------------------------------------------------------------------------


def test_partial_coverage_flag_when_survivors_below_final_size():
    rows = [
        _scored_row("S1", composite_score=0.9),
        _scored_row("S2", composite_score=0.8),
        # active_share is optional → S3 survives despite a null active_share.
        _scored_row("S3", composite_score=0.7, active_share_median_1y=None),
        _scored_row("S4", composite_score=0.6, ptr_latest=None),
        _scored_row("S5", composite_score=0.5, style_drift_3y=None),
    ]
    survivors, _, _ = apply_stage2(_df(rows), aum_map={}, final_size=5)
    assert survivors.height == 3  # S1, S2, S3 (S4/S5 dropped on required metrics)
    assert all(survivors["partial_coverage_flag"].to_list())


def test_no_partial_flag_when_survivors_fill_to_final_size():
    rows = [_scored_row(f"S{i}", composite_score=0.9 - i * 0.01) for i in range(5)]
    survivors, _, _ = apply_stage2(_df(rows), aum_map={}, final_size=5)
    assert survivors.height == 5
    assert not any(survivors["partial_coverage_flag"].to_list())


def test_aum_attached_from_aum_map():
    rows = [_scored_row("S_with_aum"), _scored_row("S_no_aum")]
    aum_map = {"S_with_aum": 1234.5}
    survivors, _, _ = apply_stage2(_df(rows), aum_map=aum_map, final_size=5)
    by_code = {r["scheme_code"]: r for r in survivors.iter_rows(named=True)}
    assert by_code["S_with_aum"]["aum_crore"] == 1234.5
    assert by_code["S_no_aum"]["aum_crore"] is None


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
    assert lc["n_survived"] == 1
    assert lc["n_dropped"] == 1
    assert lc["pct_survived"] == 50.0


def test_coverage_aum_rollup_correct():
    rows = [
        _scored_row("S_pass", composite_score=0.9),
        _scored_row("S_drop", composite_score=0.8, ptr_latest=None),
    ]
    aum_map = {"S_pass": 100.0, "S_drop": 200.0}
    _, _, coverage = apply_stage2(_df(rows), aum_map=aum_map)
    row = coverage.row(0, named=True)
    assert row["aum_considered_crore"] == 300.0
    assert row["aum_survived_crore"] == 100.0
    assert row["aum_dropped_crore"] == 200.0


def test_coverage_top_dropped_amc():
    rows = [
        _scored_row("S1", source_amc="amc_a", style_drift_3y=None, composite_score=0.9),
        _scored_row("S2", source_amc="amc_a", ptr_latest=None, composite_score=0.8),
        _scored_row("S3", source_amc="amc_b", aum_impact_cost_days=None, composite_score=0.7),
    ]
    _, _, coverage = apply_stage2(_df(rows), aum_map={})
    assert coverage.row(0, named=True)["top_dropped_amc"] == "amc_a"


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
    assert result["n_survivors"] == 2
    assert result["n_dropped"] == 1


def test_run_handles_empty_input(tmp_path):
    result = run(pl.DataFrame(), aum_map={}, output_dir=tmp_path)
    assert Path(result["dropped_file"]).exists()
    assert Path(result["coverage_file"]).exists()
    assert result["n_survivors"] == 0
    assert result["n_dropped"] == 0
