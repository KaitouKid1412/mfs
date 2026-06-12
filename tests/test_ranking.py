"""Tests for hard filters + composite_score_stage1 + composite_score_stage2.

Stage 1 is pure Performance & Consistency (8 metrics, no Phase 2 inputs,
no soft penalties). Stage 2 layers active_share + style_drift + PTR/AUM
soft penalties on top with downshifted Phase 1 weights.
"""

from datetime import date, datetime

import polars as pl

from mfs.rank.filters import apply_hard_filters, split_core_complete
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


def test_filters_read_from_config(monkeypatch):
    """A2-1: the floors come from pipeline.yaml's `filters:` block, not from
    code. Tightening capture_efficiency_min in the config must drop a fund
    that passes the relaxed default floor."""
    import mfs.rank.filters as filters_mod
    from mfs.config import get_pipeline_config

    cfg = get_pipeline_config().model_copy(deep=True)
    cfg.filters.capture_efficiency_min = 0.90
    monkeypatch.setattr(filters_mod, "get_pipeline_config", lambda: cfg)

    df = pl.DataFrame([
        _row("A"),                                # capture 1.235 — passes either way
        _row("B", capture_efficiency=0.75),       # passes 0.50, fails 0.90
    ])
    survivors, excluded = apply_hard_filters(df, with_reasons=True)
    assert set(survivors["scheme_code"]) == {"A"}
    reasons = dict(excluded.select("scheme_code", "exclusion_reason").rows())
    assert reasons == {"B": "FILTER:capture_efficiency"}


def test_filters_config_defaults_match_yaml():
    """A2-1 drift guard: FiltersConfig code defaults must equal the values in
    configs/pipeline.yaml so neither side can silently diverge."""
    from mfs.config import FiltersConfig, get_pipeline_config

    assert FiltersConfig().model_dump() == get_pipeline_config().filters.model_dump()


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


# ---------------------------------------------------------------------------
# D2 core-metric gate (A2-2) — split_core_complete + excluded.csv
# ---------------------------------------------------------------------------


def test_null_alpha_fund_excluded_and_listed():
    """D2: a fund with null alpha is hard-dropped (never scored as
    category-average) and listed in the excluded frame with the contract
    reason. Replaces the pre-D2 null-tolerant test
    (test_stage1_handles_null_alpha_without_nan)."""
    df = pl.DataFrame([
        _row("A"),
        _row("B", alpha_3y_annualized=None),
        _row("C", alpha_3y_annualized=0.02),
    ])
    complete, excluded = split_core_complete(df)
    assert set(complete["scheme_code"]) == {"A", "C"}
    b = excluded.filter(pl.col("scheme_code") == "B").row(0, named=True)
    assert b["missing_core_metrics"] == "alpha_3y_annualized"
    assert b["exclusion_reason"] == "MISSING_CORE_METRIC:alpha_3y_annualized"


def test_nan_core_metric_excluded():
    """Missing = null OR NaN: NaN reaches the rank layer via the zero-NAV
    poisoning path and must not slip through null-only checks."""
    df = pl.DataFrame([
        _row("A"),
        _row("B", sortino_3y=float("nan")),
    ])
    complete, excluded = split_core_complete(df)
    assert set(complete["scheme_code"]) == {"A"}
    b = excluded.row(0, named=True)
    assert b["scheme_code"] == "B"
    assert b["missing_core_metrics"] == "sortino_3y"
    assert b["exclusion_reason"] == "MISSING_CORE_METRIC:sortino_3y"


def test_multiple_missing_core_metrics_joined_first_reason():
    """missing_core_metrics is ';'-joined in declaration order; the reason
    carries the FIRST missing metric."""
    df = pl.DataFrame([
        _row("A"),
        _row("B", alpha_3y_annualized=None, sortino_3y=None,
             info_ratio_3y=float("nan")),
    ])
    _, excluded = split_core_complete(df)
    b = excluded.row(0, named=True)
    assert b["missing_core_metrics"] == "alpha_3y_annualized;sortino_3y;info_ratio_3y"
    assert b["exclusion_reason"] == "MISSING_CORE_METRIC:alpha_3y_annualized"


def test_young_fund_with_null_5y_not_excluded():
    """The 5y pair is deliberately NOT core: a young fund with a full 3y set
    but null 5y survives the gate and gets the 5y→3y proxy in scoring."""
    df = pl.DataFrame([
        _row("A"),
        _row("B", ret_5y_median=None, ret_5y_p25=None),
        _row("C", ret_5y_median=0.10, ret_5y_p25=0.05),
    ])
    complete, excluded = split_core_complete(df)
    assert set(complete["scheme_code"]) == {"A", "B", "C"}
    assert excluded.is_empty()
    scored = composite_score_stage1(zscore_within_category(complete))
    b = scored.filter(pl.col("scheme_code") == "B").row(0, named=True)
    assert b["z_ret_5y_median"] == b["z_ret_3y_median"]
    assert b["z_ret_5y_p25"] == b["z_ret_3y_p25"]
    assert b["composite_score"] is not None


def test_excluded_csv_written_with_header_when_empty(tmp_path, monkeypatch):
    """stage1/excluded.csv is ALWAYS written — header-only when no fund was
    dropped — so downstream consumers never hit a missing file."""
    from mfs import paths
    from mfs.rank import shortlist

    monkeypatch.setattr(
        paths, "shortlist_dir", lambda as_of: tmp_path / as_of,
    )
    scored = composite_score_stage1(
        zscore_within_category(pl.DataFrame([_row("A"), _row("B")]))
    )
    shortlist._write_stage1_outputs(scored, pl.DataFrame(), date(2026, 6, 12))
    csv_path = tmp_path / "2026-06-12" / "stage1" / "excluded.csv"
    assert csv_path.exists()
    out = pl.read_csv(csv_path)
    assert out.columns == shortlist.EXCLUDED_OUTPUT_COLS
    assert out.is_empty()


def test_excluded_csv_sorted_with_reasons(tmp_path, monkeypatch):
    """excluded.csv carries the contract columns sorted by
    (canonical_category, scheme_code)."""
    from mfs import paths
    from mfs.rank import shortlist

    monkeypatch.setattr(
        paths, "shortlist_dir", lambda as_of: tmp_path / as_of,
    )
    df = pl.DataFrame([
        _row("Z", cat="Mid Cap", alpha_3y_annualized=None),
        _row("M", capture_efficiency=0.30),
        _row("A", cat="Mid Cap", sortino_3y=None),
    ])
    survivors, excluded = apply_hard_filters(df, with_reasons=True)
    survivors, core_excluded = split_core_complete(survivors)
    excluded = pl.concat([excluded, core_excluded], how="diagonal_relaxed")
    scored = composite_score_stage1(zscore_within_category(survivors))
    shortlist._write_stage1_outputs(scored, excluded, date(2026, 6, 12))
    out = pl.read_csv(tmp_path / "2026-06-12" / "stage1" / "excluded.csv")
    assert out.columns == shortlist.EXCLUDED_OUTPUT_COLS
    assert out["scheme_code"].to_list() == ["M", "A", "Z"]
    assert out["exclusion_reason"].to_list() == [
        "FILTER:capture_efficiency",
        "MISSING_CORE_METRIC:sortino_3y",
        "MISSING_CORE_METRIC:alpha_3y_annualized",
    ]
    assert out["missing_core_metrics"].to_list() == [
        None, "sortino_3y", "alpha_3y_annualized",
    ]


def test_join_scheme_master_carries_isin_growth(monkeypatch):
    """E1: the scheme-master join threads isin_growth through as execution
    metadata; unmatched schemes get a null, never a join error."""
    from mfs.rank import shortlist

    sm = pl.DataFrame({
        "scheme_code": ["A"],
        "scheme_name": ["Fund A"],
        "base_fund_id": ["amc::a"],
        "isin_growth": ["INF000000001"],
    })
    monkeypatch.setattr(shortlist.q, "scheme_master", lambda **kw: sm)
    out = shortlist._join_scheme_master(pl.DataFrame({"scheme_code": ["A", "B"]}))
    by = {r["scheme_code"]: r for r in out.iter_rows(named=True)}
    assert by["A"]["isin_growth"] == "INF000000001"
    assert by["B"]["isin_growth"] is None
    assert "isin_growth" in shortlist.STAGE1_OUTPUT_COLS


def test_join_scheme_master_empty_master_emits_null_isin_column(monkeypatch):
    """Empty-master branch must still emit the isin_growth column (as Utf8
    nulls) so downstream projections never KeyError."""
    from mfs.rank import shortlist

    monkeypatch.setattr(
        shortlist.q, "scheme_master", lambda **kw: pl.DataFrame()
    )
    out = shortlist._join_scheme_master(pl.DataFrame({"scheme_code": ["A"]}))
    assert out["isin_growth"].to_list() == [None]
    assert out["isin_growth"].dtype == pl.Utf8


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
# A2-3 — bonus-option dedupe BEFORE z-scoring
# ---------------------------------------------------------------------------


def test_bonus_duplicate_does_not_contaminate_z_stats():
    """The bonus-option duplicate must be removed BEFORE z-scoring so the
    category nanmean/nanstd derive from distinct funds only: B's z equals the
    value computed on {A, B, C} without the duplicate A'."""
    from mfs.rank.shortlist import _dedupe_legacy_unit_classes

    a = _row("A", ret_3y_median=0.20, base_fund_id="acme::flexi")
    a_bonus = _row("A2", ret_3y_median=0.20, base_fund_id="acme::flexi_bonus")
    b = _row("B", ret_3y_median=0.10, base_fund_id="bcorp::flexi")
    c = _row("C", ret_3y_median=0.30, base_fund_id="ccorp::flexi")

    # Build-equivalent path (build_scored_stage1 order): dedupe → z-score.
    full = pl.DataFrame([a, a_bonus, b, c])
    deduped = _dedupe_legacy_unit_classes(full)
    assert set(deduped["scheme_code"]) == {"A", "B", "C"}
    z_pipeline = zscore_within_category(deduped)

    # Reference: z-stats computed on {A, B, C} with no duplicate ever present.
    z_reference = zscore_within_category(pl.DataFrame([a, b, c]))

    b_pipeline = z_pipeline.filter(pl.col("scheme_code") == "B")["z_ret_3y_median"][0]
    b_reference = z_reference.filter(pl.col("scheme_code") == "B")["z_ret_3y_median"][0]
    assert b_pipeline == b_reference

    # Control: had the duplicate stayed in, B's z would have shifted.
    z_contaminated = zscore_within_category(full)
    b_contaminated = z_contaminated.filter(
        pl.col("scheme_code") == "B"
    )["z_ret_3y_median"][0]
    assert b_contaminated != b_reference


def test_dedupe_keeps_growth_over_bonus():
    """Winner sort prefers the non-bonus row regardless of scheme_code or
    metric ordering (composite_score no longer exists at dedupe time)."""
    from mfs.rank.shortlist import _dedupe_legacy_unit_classes

    df = pl.DataFrame([
        _row("9001", base_fund_id="acme::flexi_bonus"),
        _row("1001", base_fund_id="acme::flexi"),
        _row("2001", base_fund_id="bcorp::flexi"),
    ])
    out = _dedupe_legacy_unit_classes(df)
    assert set(out["scheme_code"]) == {"1001", "2001"}


def test_null_base_fund_id_rows_not_collapsed():
    """Rows with null base_fund_id must pass through untouched — fill-null
    would give them all the same '' dedup key and collapse distinct funds."""
    from mfs.rank.shortlist import _dedupe_legacy_unit_classes

    df = pl.DataFrame([
        _row("A", base_fund_id=None),
        _row("B", base_fund_id=None),
        _row("C", base_fund_id="ccorp::flexi"),
        _row("C2", base_fund_id="ccorp::flexi_bonus"),
    ])
    out = _dedupe_legacy_unit_classes(df)
    assert set(out["scheme_code"]) == {"A", "B", "C"}


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


def test_stage2_style_drift_null_costs_fixed_penalty_in_live_cohort():
    """D3 (A2-4): a null style_drift in a cohort where the metric is live
    (2/3 disclosed) no longer rides free — it costs exactly the calibrated
    missing-disclosure penalty (z contribution stays 0)."""
    import math

    from mfs.config import get_pipeline_config

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
    by = {r["scheme_code"]: r for r in scored.iter_rows(named=True)}
    pen = float(get_pipeline_config().soft_penalties.missing_disclosure.penalty)
    a = by["A"]["composite_score"]
    b = by["B"]["composite_score"]
    c = by["C"]["composite_score"]
    assert a is not None and not math.isnan(a)
    # B and C carry symmetric ±z style terms, so their mean is the
    # no-style-term baseline shared by all three funds; A sits exactly the
    # missing-disclosure penalty below it.
    assert abs(a - ((b + c) / 2 - pen)) < 1e-9
    # Missing sits between the well-behaved and the worst discloser here.
    assert c > a > b


def test_stage2_aum_impact_penalty_above_threshold():
    """A scheme with high aum_impact_cost_days (>5) should rank below a peer
    with low impact cost, all else equal (A2-6 log ramp: pen(15) ≈ 0.0119,
    pen(1) = 0)."""
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


def _aum_only_composite(days):
    """Single-fund cohort: every z is 0 and every other penalty is dead, so
    the composite equals minus the AUM-impact penalty exactly."""
    df = pl.DataFrame([_row("A", aum_impact_cost_days=days)])
    z = zscore_within_category(df)
    return composite_score_stage2(z).row(0, named=True)["composite_score"]


def test_stage2_aum_impact_log_curve_pinned_values():
    """A2-6: penalty = max_pen * clip(ln(days/5)/ln(500/5), 0, 1)."""
    import math

    max_pen = 0.05
    denom = math.log(500.0 / 5.0)
    assert abs(_aum_only_composite(5.0) - 0.0) < 1e-12          # at threshold
    assert abs(_aum_only_composite(10.0) - (-max_pen * math.log(2.0) / denom)) < 1e-9
    assert abs(_aum_only_composite(500.0) - (-max_pen)) < 1e-12  # saturation
    assert abs(_aum_only_composite(924.0) - (-max_pen)) < 1e-12  # capped
    assert abs(_aum_only_composite(None) - 0.0) < 1e-12          # null → 0 here (D3 covers it)


def test_stage2_aum_impact_log_curve_monotone_over_observed_range():
    """10d / 79d (Small Cap pool median) / 163d (p95) must carry strictly
    increasing penalties — the old linear ramp flat-capped all of them."""
    p10 = -_aum_only_composite(10.0)
    p79 = -_aum_only_composite(79.0)
    p163 = -_aum_only_composite(163.0)
    assert 0 < p10 < p79 < p163 < 0.05
