"""E2 — max drawdown + time-to-recover (display-only).

Semantics of ``compute.drawdown.drawdown_stats`` are pinned on hand-computed
synthetic series, and the threading (orchestrator phase-1 row → DB column
registries → schema.sql → stage-1 output columns) is checked exactly like
alpha_confidence's (see tests/test_alpha_confidence_threading.py). No DB
connection is touched.

Pinned conventions:
  * max_dd_pct is a POSITIVE percent (100 → 80 reports 20.0);
  * a never-declining series reports max_dd_pct=0.0 and recovery_days=None;
  * recovery_days = calendar days trough → first nav >= prior peak,
    None when not recovered by the end of the window.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

import mfs.db as mfs_db
from mfs.compute import orchestrator
from mfs.compute.drawdown import drawdown_row, drawdown_stats
from mfs.db import queries, writers

DD_COLS = (
    "max_dd_3y_pct",
    "max_dd_3y_recovery_days",
    "max_dd_5y_pct",
    "max_dd_5y_recovery_days",
)


def _nav(points: list[tuple[date, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"date": [d for d, _ in points], "nav": [v for _, v in points]},
        schema={"date": pl.Date, "nav": pl.Float64},
    )


# Hand-computed golden series: peak 120 (2024-03-01), trough 90 (2024-06-01)
# → max_dd = (120-90)/120 = 25.0%; first nav >= 120 after the trough is 126
# on 2024-12-01 → recovery_days = 183.
GOLDEN = [
    (date(2024, 1, 1), 100.0),
    (date(2024, 3, 1), 120.0),
    (date(2024, 6, 1), 90.0),
    (date(2024, 9, 1), 110.0),
    (date(2024, 12, 1), 126.0),
    (date(2025, 6, 1), 130.0),
]


def test_golden_hand_computed_drawdown():
    stats = drawdown_stats(_nav(GOLDEN), window_years=3, as_of=date(2025, 6, 30))
    assert stats is not None
    assert stats["max_dd_pct"] == pytest.approx(25.0, abs=1e-12)
    assert stats["peak_date"] == date(2024, 3, 1)
    assert stats["trough_date"] == date(2024, 6, 1)
    assert stats["recovery_days"] == (date(2024, 12, 1) - date(2024, 6, 1)).days
    assert stats["recovery_days"] == 183


def test_as_of_bounds_the_window_point_in_time():
    """as_of before the recovery leg: the decline is visible but the
    recovery is not — recovery_days must be None, not future-peeking."""
    series = [(date(2023, 5, 1), 100.0), *GOLDEN]
    stats = drawdown_stats(_nav(series), window_years=3, as_of=date(2024, 7, 1))
    assert stats is not None
    assert stats["max_dd_pct"] == pytest.approx(25.0, abs=1e-12)
    assert stats["recovery_days"] is None


def test_simple_100_80_120():
    """Spec case (1): 100 → 80 → 120 gives 20.0% and finite recovery."""
    series = [
        (date(2024, 1, 1), 100.0),
        (date(2024, 7, 1), 80.0),
        (date(2025, 2, 1), 120.0),
    ]
    stats = drawdown_stats(_nav(series), window_years=3, as_of=date(2025, 2, 1))
    assert stats is not None
    assert stats["max_dd_pct"] == pytest.approx(20.0, abs=1e-12)
    assert stats["recovery_days"] == (date(2025, 2, 1) - date(2024, 7, 1)).days


def test_monotonic_rise_zero_dd_and_no_recovery_days():
    """Spec case (2): pinned convention — 0.0 dd, recovery_days None."""
    series = [
        (date(2024, 1, 1), 100.0),
        (date(2024, 8, 1), 110.0),
        (date(2025, 2, 1), 120.0),
    ]
    stats = drawdown_stats(_nav(series), window_years=3, as_of=date(2025, 2, 1))
    assert stats is not None
    assert stats["max_dd_pct"] == 0.0
    assert stats["recovery_days"] is None
    assert stats["peak_date"] is None
    assert stats["trough_date"] is None


def test_drawdown_at_series_end_not_recovered():
    """Spec case (3): the trough is the last observation → None."""
    series = [
        (date(2024, 1, 1), 100.0),
        (date(2024, 6, 1), 120.0),
        (date(2025, 3, 1), 84.0),
    ]
    stats = drawdown_stats(_nav(series), window_years=3, as_of=date(2025, 3, 1))
    assert stats is not None
    assert stats["max_dd_pct"] == pytest.approx(30.0, abs=1e-12)
    assert stats["peak_date"] == date(2024, 6, 1)
    assert stats["trough_date"] == date(2025, 3, 1)
    assert stats["recovery_days"] is None


def test_old_crash_excluded_from_3y_included_in_5y():
    """Spec case (4): a 50% crash ~4y back is invisible to the 3y stat but
    dominates the 5y stat (both windows end at the last nav date)."""
    series = [
        (date(2021, 6, 1), 100.0),
        (date(2022, 1, 1), 50.0),    # the old crash
        (date(2022, 6, 1), 100.0),   # recovered
        (date(2023, 6, 1), 110.0),
        (date(2024, 1, 1), 99.0),    # mild in-window dip: 10%
        (date(2024, 6, 1), 115.0),
        (date(2025, 12, 1), 120.0),
    ]
    as_of = date(2026, 1, 1)
    dd3 = drawdown_stats(_nav(series), window_years=3, as_of=as_of)
    dd5 = drawdown_stats(_nav(series), window_years=5, as_of=as_of)
    assert dd3 is not None and dd5 is not None
    assert dd3["max_dd_pct"] == pytest.approx(10.0, abs=1e-9)
    assert dd5["max_dd_pct"] == pytest.approx(50.0, abs=1e-9)
    assert dd5["trough_date"] == date(2022, 1, 1)


def test_zero_nav_rows_ignored():
    """Spec case (5): a poisoned 0.0 nav row must not fabricate a 100% dd."""
    poisoned = sorted(GOLDEN + [(date(2024, 8, 1), 0.0)])
    stats = drawdown_stats(_nav(poisoned), window_years=3, as_of=date(2025, 6, 30))
    assert stats is not None
    assert stats["max_dd_pct"] == pytest.approx(25.0, abs=1e-12)
    assert stats["recovery_days"] == 183


def test_under_one_year_of_data_returns_none():
    """Spec case (6)."""
    series = [
        (date(2025, 1, 1), 100.0),
        (date(2025, 6, 1), 80.0),
    ]
    assert drawdown_stats(_nav(series), window_years=3, as_of=date(2025, 6, 1)) is None


def test_empty_and_all_invalid_inputs_return_none():
    assert drawdown_stats(pl.DataFrame(), window_years=3, as_of=date(2025, 1, 1)) is None
    junk = _nav([(date(2024, 1, 1), 0.0), (date(2025, 6, 1), -5.0)])
    assert drawdown_stats(junk, window_years=3, as_of=date(2025, 6, 1)) is None


def test_drawdown_row_flattens_both_windows():
    row = drawdown_row(_nav(GOLDEN), as_of=date(2025, 6, 30))
    assert set(row) == set(DD_COLS)
    assert row["max_dd_3y_pct"] == pytest.approx(25.0, abs=1e-12)
    assert row["max_dd_5y_pct"] == pytest.approx(25.0, abs=1e-12)
    assert row["max_dd_3y_recovery_days"] == 183


def test_drawdown_row_none_windows_stay_null():
    short = _nav([(date(2025, 1, 1), 100.0), (date(2025, 6, 1), 80.0)])
    row = drawdown_row(short, as_of=date(2025, 6, 1))
    assert all(row[c] is None for c in DD_COLS)


# ---------------------------------------------------------------------------
# Threading: alpha_confidence pattern (registries / schema.sql / phase-1 row)
# ---------------------------------------------------------------------------


def test_computed_metrics_registries_identical_and_carry_drawdown_cols():
    assert writers._COMPUTED_METRICS_COLS == queries._COMPUTED_METRICS_COLS
    for c in DD_COLS:
        assert c in writers._COMPUTED_METRICS_COLS


def test_schema_sql_adds_drawdown_columns_idempotently():
    sql = (Path(mfs_db.__file__).parent / "schema.sql").read_text()
    for c in DD_COLS:
        assert f"ADD COLUMN IF NOT EXISTS {c}" in sql


def test_stage1_output_cols_surface_drawdown():
    from mfs.rank.shortlist import STAGE1_OUTPUT_COLS

    for c in DD_COLS:
        assert c in STAGE1_OUTPUT_COLS


def test_drawdown_never_composite_weighted():
    """Display-only invariant: no scoring weight may reference drawdown."""
    from mfs.config import get_pipeline_config

    cfg = get_pipeline_config()
    for weights in (cfg.composite_weights_stage1, cfg.composite_weights_stage2):
        assert not any("dd" in k or "drawdown" in k for k in weights)


def test_phase1_row_has_drawdown_keys_on_early_return(monkeypatch):
    monkeypatch.setattr(
        orchestrator.alignment, "align_scheme", lambda *_a, **_k: pl.DataFrame()
    )
    row = orchestrator.compute_phase1_for_scheme(
        "TEST", "NIFTY 500 TRI", "Flexi Cap", date(2026, 6, 8)
    )
    assert row["data_quality_flag"] == "INSUFFICIENT_HISTORY"
    for c in DD_COLS:
        assert c in row
        assert row[c] is None


def _synthetic_aligned(n_days: int, seed: int = 42) -> pl.DataFrame:
    """Aligned-frame shape for the full phase-1 path (mirrors
    tests/test_alpha_confidence_threading.py)."""
    rng = np.random.default_rng(seed)
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(n_days)]
    bench_log_ret = rng.normal(loc=0.00045, scale=0.011, size=n_days)
    rf_log = float(np.log(1.0 + 0.000247))
    fund_log_ret = bench_log_ret + rng.normal(0.0, 0.003, size=n_days)
    return pl.DataFrame({
        "date": dates,
        "nav": (np.exp(np.cumsum(fund_log_ret)) * 100.0).tolist(),
        "bench_close": (np.exp(np.cumsum(bench_log_ret)) * 1000.0).tolist(),
        "fund_log_ret": fund_log_ret.tolist(),
        "bench_log_ret": bench_log_ret.tolist(),
        "rate_daily": [0.000247] * n_days,
        "rate_daily_log": [rf_log] * n_days,
    }).with_columns(pl.col("date").cast(pl.Date))


def test_phase1_row_populates_drawdown(monkeypatch):
    aligned = _synthetic_aligned(n_days=252 * 6)
    monkeypatch.setattr(
        orchestrator.alignment, "align_scheme", lambda *_a, **_k: aligned
    )
    monkeypatch.setattr(
        orchestrator.alignment, "master_calendar", lambda: aligned.select("date")
    )
    row = orchestrator.compute_phase1_for_scheme(
        "TEST", "NIFTY 500 TRI", "Flexi Cap", date(2026, 6, 8)
    )
    assert row["data_quality_flag"] == "GOOD"
    assert row["max_dd_3y_pct"] is not None and row["max_dd_3y_pct"] > 0.0
    assert row["max_dd_5y_pct"] is not None
    # Same window end → the 5y window is a superset of the 3y window.
    assert row["max_dd_5y_pct"] >= row["max_dd_3y_pct"]
