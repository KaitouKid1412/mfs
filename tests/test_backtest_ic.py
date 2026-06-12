"""D3 — retro-IC backtest pure helpers: Spearman IC (golden, ties), forward
category-relative returns, weight perturbation + the D10 p25_collapsed
variant, verdict thresholds at the boundaries, and as_of truncation. All
synthetic frames, no DB."""

from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

_TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "backtest_ic.py"
_spec = importlib.util.spec_from_file_location("backtest_ic", _TOOL_PATH)
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)


# ---------------------------------------------------------------------------
# spearman_ic
# ---------------------------------------------------------------------------


def test_spearman_ic_golden_hand_computed():
    """x ranks 1..5; y = (2,1,4,3,5) -> rank diffs d = (1,1,1,1,0),
    rho = 1 - 6*sum(d^2)/(n(n^2-1)) = 1 - 24/120 = 0.8 exactly."""
    x = np.array([10.0, 20.0, 30.0, 40.0, 50.0])
    y = np.array([2.0, 1.0, 4.0, 3.0, 5.0])
    assert abs(bt.spearman_ic(x, y) - 0.8) < 1e-12


def test_spearman_ic_perfect_and_inverted():
    x = np.array([1.0, 2.0, 3.0, 4.0])
    assert abs(bt.spearman_ic(x, x * 10) - 1.0) < 1e-12
    assert abs(bt.spearman_ic(x, -x) - (-1.0)) < 1e-12


def test_spearman_ic_ties_average_rank():
    """Tied x values get average ranks: x=(1,1,3), y=(1,2,3) -> ranks
    rx=(1.5,1.5,3), ry=(1,2,3) -> Pearson(rx,ry)=0.866025..."""
    ic = bt.spearman_ic(np.array([1.0, 1.0, 3.0]), np.array([1.0, 2.0, 3.0]))
    assert abs(ic - np.sqrt(3) / 2) < 1e-9


def test_spearman_ic_degenerate_returns_none():
    assert bt.spearman_ic(np.array([1.0, 2.0]), np.array([1.0, 2.0])) is None
    assert bt.spearman_ic(
        np.array([5.0, 5.0, 5.0]), np.array([1.0, 2.0, 3.0])
    ) is None
    # NaNs are masked out before the pair-count floor
    assert bt.spearman_ic(
        np.array([1.0, np.nan, 2.0]), np.array([1.0, 2.0, 3.0])
    ) is None


# ---------------------------------------------------------------------------
# forward returns + category-relative transform
# ---------------------------------------------------------------------------


def _nav(start: date, n: int, daily: float = 0.001) -> pl.DataFrame:
    return pl.DataFrame({
        "date": [start + timedelta(days=i) for i in range(n)],
        "nav": [100.0 * (1.0 + daily) ** i for i in range(n)],
    })


def test_forward_simple_return_exact():
    as_of = date(2020, 6, 30)
    nav = _nav(date(2019, 1, 1), 1200, daily=0.0)  # flat at 100
    nav = nav.with_columns(
        pl.when(pl.col("date") <= as_of).then(100.0).otherwise(110.0).alias("nav")
    )
    r = bt.forward_simple_return(nav, as_of, 1)
    assert abs(r - 0.10) < 1e-12


def test_forward_simple_return_missing_end_is_none():
    as_of = date(2020, 6, 30)
    nav = _nav(date(2019, 1, 1), 600)  # ends ~2020-08 — no NAV near as_of+1y
    assert bt.forward_simple_return(nav, as_of, 1) is None


def test_forward_simple_return_stale_start_is_none():
    """Last NAV 60 days before as_of (dead fund) -> None."""
    as_of = date(2020, 6, 30)
    nav = _nav(date(2018, 1, 1), 365 * 2 - 60)
    assert bt.forward_simple_return(nav, as_of, 1) is None


def test_category_relative_subtracts_category_median():
    frame = pl.DataFrame({
        "scheme_code": ["A", "B", "C", "D"],
        "canonical_category": ["X", "X", "X", "Y"],
        "forward_ret": [0.10, 0.20, 0.30, 0.05],
    })
    out = bt.category_relative(frame)
    rel = dict(zip(out["scheme_code"], out["forward_rel"]))
    assert abs(rel["A"] - (-0.10)) < 1e-12   # median of X = 0.20
    assert abs(rel["B"] - 0.0) < 1e-12
    assert abs(rel["C"] - 0.10) < 1e-12
    assert abs(rel["D"] - 0.0) < 1e-12       # singleton category


# ---------------------------------------------------------------------------
# weight perturbation / variants
# ---------------------------------------------------------------------------

WEIGHTS = {
    "ret_3y_median": 0.15, "ret_3y_p25": 0.10,
    "ret_5y_median": 0.15, "ret_5y_p25": 0.10,
    "alpha_3y": 0.25, "sortino_3y": 0.10,
    "info_ratio_3y": 0.10, "capture_efficiency": 0.05,
}


def test_perturb_weights_renormalizes_to_original_total():
    up = bt.perturb_weights(WEIGHTS, "alpha_3y", 1.5)
    assert abs(sum(up.values()) - 1.0) < 1e-12
    assert up["alpha_3y"] > WEIGHTS["alpha_3y"]
    # relative proportions of untouched keys preserved
    assert abs(up["sortino_3y"] / up["info_ratio_3y"] - 1.0) < 1e-12
    down = bt.perturb_weights(WEIGHTS, "alpha_3y", 0.5)
    assert abs(sum(down.values()) - 1.0) < 1e-12
    assert down["alpha_3y"] < WEIGHTS["alpha_3y"]


def test_p25_collapsed_variant_zeroes_p25_and_sums_to_one():
    v = bt.p25_collapsed_variant(WEIGHTS)
    assert v["ret_3y_p25"] == 0.0
    assert v["ret_5y_p25"] == 0.0
    assert abs(v["ret_3y_median"] - 0.25) < 1e-12
    assert abs(v["ret_5y_median"] - 0.25) < 1e-12
    assert abs(sum(v.values()) - 1.0) < 1e-12


def test_build_variants_registers_p25_collapsed():
    variants = bt.build_variants(WEIGHTS)
    assert "p25_collapsed" in variants            # D10 registration
    assert len(variants) == 2 * len(WEIGHTS) + 1  # ±50% each + collapse
    for v in variants.values():
        assert abs(sum(v.values()) - 1.0) < 1e-12


# ---------------------------------------------------------------------------
# verdict thresholds (pre-registered; boundary behavior)
# ---------------------------------------------------------------------------


def test_verdict_justified_at_exact_thresholds():
    assert bt.classify_verdict(0.05, 2.0, 0.001) == "JUSTIFIED"
    assert bt.classify_verdict(0.08, -2.5, 0.001) == "JUSTIFIED"  # |t|


def test_verdict_refuted_boundaries():
    assert bt.classify_verdict(0.0, 3.0, 0.01) == "REFUTED"    # mean<=0
    assert bt.classify_verdict(-0.02, 5.0, 0.01) == "REFUTED"
    assert bt.classify_verdict(0.06, 0.99, 0.01) == "REFUTED"  # |t|<1


def test_verdict_inconclusive_between():
    assert bt.classify_verdict(0.049, 2.5, 0.01) == "INCONCLUSIVE"  # IC short
    assert bt.classify_verdict(0.05, 1.99, 0.01) == "INCONCLUSIVE"  # t short
    assert bt.classify_verdict(0.05, 2.0, 0.0) == "INCONCLUSIVE"    # spread<=0
    assert bt.classify_verdict(0.05, 2.0, None) == "INCONCLUSIVE"
    assert bt.classify_verdict(None, None, None) == "INCONCLUSIVE"
    assert bt.classify_verdict(0.06, 1.5, 0.01) == "INCONCLUSIVE"   # 1<=|t|<2


# ---------------------------------------------------------------------------
# as_of truncation + history gate
# ---------------------------------------------------------------------------


def test_truncate_aligned_drops_post_as_of_rows():
    aligned = pl.DataFrame({
        "date": [date(2020, 1, 1), date(2020, 6, 30), date(2020, 7, 1)],
        "nav": [100.0, 110.0, 111.0],
        "fund_log_ret": [None, 0.01, 0.009],
    })
    out = bt.truncate_aligned(aligned, date(2020, 6, 30))
    assert out.height == 2
    assert out["date"].max() == date(2020, 6, 30)
    assert bt.truncate_aligned(pl.DataFrame(), date(2020, 6, 30)).is_empty()


def test_has_min_history_span_and_staleness():
    as_of = date(2020, 12, 31)
    ok = _nav(date(2017, 1, 1), 365 * 4)                # 4y, fresh at as_of
    assert bt.has_min_history(ok, as_of) is True
    short = _nav(date(2018, 6, 1), 365 * 2)             # 2y span
    assert bt.has_min_history(short, as_of) is False
    stale = _nav(date(2015, 1, 1), 365 * 4)             # ends ~2019 — dead
    assert bt.has_min_history(stale, as_of) is False


# ---------------------------------------------------------------------------
# small structural helpers
# ---------------------------------------------------------------------------


def test_quarter_ends_range():
    qs = bt.quarter_ends(date(2016, 1, 1), date(2017, 6, 30))
    assert qs == [
        date(2016, 3, 31), date(2016, 6, 30), date(2016, 9, 30),
        date(2016, 12, 31), date(2017, 3, 31), date(2017, 6, 30),
    ]
    assert bt.quarter_ends(date(2016, 4, 1), date(2016, 4, 2)) == []


def test_pooled_stats_t():
    mean, t, n = bt.pooled_stats([0.1, 0.2, 0.3])
    assert abs(mean - 0.2) < 1e-12
    assert n == 3
    assert abs(t - (0.2 / 0.1 * np.sqrt(3))) < 1e-9
    mean1, t1, n1 = bt.pooled_stats([0.1])
    assert (mean1, t1, n1) == (0.1, None, 1)
    assert bt.pooled_stats([]) == (None, None, 0)


def test_top_n_codes_deterministic_on_ties():
    frame = pl.DataFrame({
        "scheme_code": ["B", "A", "C"],
        "composite_score": [1.0, 1.0, 0.5],
    })
    assert bt.top_n_codes(frame, "composite_score", n=2) == ["A", "B"]
