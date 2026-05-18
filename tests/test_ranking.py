from datetime import date, datetime

import polars as pl

from mfs.rank.filters import apply_hard_filters
from mfs.rank.score import composite_score
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
        "sortino_3y_peer_pct": 0.8,
        "info_ratio_3y": 0.7,
        "capture_up": 1.05,
        "capture_down": 0.85,
        "capture_efficiency": 1.235,
        "r_squared_3y": 0.82,
        "beta_3y": 0.95,
        "data_quality_flag": "GOOD",
        "computed_at": datetime.utcnow(),
        "pipeline_version": "v1.0.0",
    }
    base.update(overrides)
    return base


def test_filters_drop_low_capture_efficiency():
    df = pl.DataFrame([
        _row("A"),
        _row("B", capture_efficiency=0.95),  # below threshold
    ])
    out = apply_hard_filters(df)
    assert set(out["scheme_code"]) == {"A"}


def test_filters_drop_out_of_band_r2():
    df = pl.DataFrame([
        _row("A"),
        _row("B", r_squared_3y=0.95),  # above band
        _row("C", r_squared_3y=0.50),  # below band
    ])
    out = apply_hard_filters(df)
    assert set(out["scheme_code"]) == {"A"}


def test_zscore_and_composite_pick_best_in_category():
    df = pl.DataFrame([
        _row("A", alpha_3y_annualized=0.06, sortino_3y=1.8, info_ratio_3y=1.2),
        _row("B", alpha_3y_annualized=0.02, sortino_3y=0.9, info_ratio_3y=0.6),
        _row("C", alpha_3y_annualized=0.04, sortino_3y=1.4, info_ratio_3y=0.9),
    ])
    z = zscore_within_category(df)
    scored = composite_score(z)
    top = scored.sort("composite_score", descending=True).head(1)
    assert top["scheme_code"][0] == "A"
