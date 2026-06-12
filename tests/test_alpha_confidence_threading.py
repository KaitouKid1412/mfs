"""A1-2 plumbing checks (locked D1, signed alpha): `alpha_confidence` must
flow alpha.py → orchestrator phase-1 row → DB column registries → schema.sql,
strictly as a display-only column. No DB connection is touched."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

import mfs.db as mfs_db
from mfs.compute import orchestrator
from mfs.db import queries, writers


def _synthetic_aligned(n_days: int, drift: float, noise: float, seed: int = 42):
    """Minimal aligned frame: fund = rf + 1.0*bench_excess + drift + noise."""
    rng = np.random.default_rng(seed)
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(n_days)]
    bench_log_ret = rng.normal(loc=0.00045, scale=0.011, size=n_days)
    rf_log = float(np.log(1.0 + 0.000247))
    fund_log_ret = (
        rf_log + (bench_log_ret - rf_log) + drift
        + rng.normal(0.0, noise, size=n_days)
    )
    return pl.DataFrame({
        "date": dates,
        "nav": (np.exp(np.cumsum(fund_log_ret)) * 100.0).tolist(),
        "bench_close": (np.exp(np.cumsum(bench_log_ret)) * 1000.0).tolist(),
        "fund_log_ret": fund_log_ret.tolist(),
        "bench_log_ret": bench_log_ret.tolist(),
        "rate_daily": [0.000247] * n_days,
        "rate_daily_log": [rf_log] * n_days,
    }).with_columns(pl.col("date").cast(pl.Date))


def test_computed_metrics_registries_identical_and_carry_alpha_confidence():
    """writers/queries column registries are a write/read pair over the same
    table — they must stay identical, and both must carry alpha_confidence."""
    assert writers._COMPUTED_METRICS_COLS == queries._COMPUTED_METRICS_COLS
    assert "alpha_confidence" in writers._COMPUTED_METRICS_COLS


def test_schema_sql_adds_alpha_confidence_idempotently():
    sql = (Path(mfs_db.__file__).parent / "schema.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS alpha_confidence" in sql


def test_phase1_row_has_alpha_confidence_key_on_early_return(monkeypatch):
    """Even the INSUFFICIENT_HISTORY early-return row must carry the key so
    the writer's column registry always finds it."""
    monkeypatch.setattr(
        orchestrator.alignment, "align_scheme", lambda *_a, **_k: pl.DataFrame()
    )
    row = orchestrator.compute_phase1_for_scheme(
        "TEST", "NIFTY 500 TRI", "Flexi Cap", date(2026, 6, 8)
    )
    assert row["data_quality_flag"] == "INSUFFICIENT_HISTORY"
    assert "alpha_confidence" in row
    assert row["alpha_confidence"] is None


def test_phase1_row_populates_signed_alpha_and_confidence(monkeypatch):
    """Full phase-1 path on a persistent UNDERPERFORMER: the orchestrator row
    must surface a negative alpha (locked D1) alongside alpha_confidence in
    [0, 1] — high here, because the negative drift is strong (|t| ≈ 5-6 per
    window). Confidence reflects evidence strength, not sign."""
    aligned = _synthetic_aligned(n_days=252 * 6, drift=-0.0005, noise=0.003)
    monkeypatch.setattr(
        orchestrator.alignment, "align_scheme", lambda *_a, **_k: aligned
    )
    monkeypatch.setattr(
        orchestrator.alignment, "master_calendar",
        lambda: aligned.select("date"),
    )
    row = orchestrator.compute_phase1_for_scheme(
        "TEST", "NIFTY 500 TRI", "Flexi Cap", date(2026, 6, 8)
    )
    assert row["data_quality_flag"] == "GOOD"
    assert row["alpha_3y_annualized"] is not None
    assert row["alpha_3y_annualized"] < 0  # impossible before D1
    assert row["alpha_confidence"] is not None
    assert 0.0 <= row["alpha_confidence"] <= 1.0
    assert row["alpha_confidence"] > 0.9
