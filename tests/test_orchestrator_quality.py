"""Orchestrator quality gates (A1-3, A1-5) and failure escalation (A1-9).

A1-3: per-scheme as_of staleness gate — a scheme whose last NAV lags the
run's as_of beyond quality.max_scheme_nav_lag_bdays trading days flags STALE.
A1-5: quality.min_history_years_for_ranking is wired from config into
_data_quality_flag (the YAML value, not a hardcoded default, decides).
A1-9: per-scheme compute failures are counted skip-and-report events; the
run halts with PipelineError when the failure fraction exceeds
quality.max_scheme_compute_failure_rate — BEFORE any partition write.

No DB: alignment.master_calendar / align_scheme, db queries, and db writers
are monkeypatched throughout.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from mfs.compute import orchestrator
from mfs.config import get_pipeline_config
from mfs.errors import PipelineError

AS_OF = date(2026, 6, 8)  # a Monday — last synthetic trading day <= as_of


def _business_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


CALENDAR = _business_days(date(2018, 1, 1), date(2026, 6, 12))
CAL_DF = pl.DataFrame({"date": CALENDAR}).with_columns(pl.col("date").cast(pl.Date))
CAL_LE_AS_OF = [d for d in CALENDAR if d <= AS_OF]


def _patch_calendar(monkeypatch):
    monkeypatch.setattr(orchestrator.alignment, "master_calendar", lambda: CAL_DF)


def _nav_frame(end_lag_bdays: int, span_years: float = 4.0) -> pl.DataFrame:
    """(date, nav) frame on the synthetic calendar ending at the
    ``end_lag_bdays``-th-from-last trading day <= AS_OF (1 = AS_OF itself),
    spanning ~span_years with no gaps."""
    end = CAL_LE_AS_OF[-end_lag_bdays]
    start = end - timedelta(days=int(365.25 * span_years))
    dates = [d for d in CALENDAR if start <= d <= end]
    return pl.DataFrame(
        {"date": dates, "nav": [100.0 + 0.01 * i for i in range(len(dates))]}
    ).with_columns(pl.col("date").cast(pl.Date))


def _full_aligned(span_years: float, end_lag_bdays: int = 1) -> pl.DataFrame:
    """Aligned frame with all metric columns, ending end_lag_bdays from AS_OF."""
    end = CAL_LE_AS_OF[-end_lag_bdays]
    start = end - timedelta(days=int(365.25 * span_years))
    dates = [d for d in CALENDAR if start <= d <= end]
    n = len(dates)
    rng = np.random.default_rng(7)
    bench_log_ret = rng.normal(0.0004, 0.01, size=n)
    fund_log_ret = bench_log_ret + 0.0001
    rf_log = float(np.log(1.000247))
    return pl.DataFrame({
        "date": dates,
        "nav": (np.exp(np.cumsum(fund_log_ret)) * 100.0).tolist(),
        "bench_close": (np.exp(np.cumsum(bench_log_ret)) * 1000.0).tolist(),
        "fund_log_ret": fund_log_ret.tolist(),
        "bench_log_ret": bench_log_ret.tolist(),
        "rate_daily": [0.000247] * n,
        "rate_daily_log": [rf_log] * n,
    }).with_columns(pl.col("date").cast(pl.Date))


def _cfg_copy(**quality_overrides):
    cfg = get_pipeline_config().model_copy(deep=True)
    for k, v in quality_overrides.items():
        setattr(cfg.quality, k, v)
    return cfg


# ---------------------------------------------------------------------------
# A1-3 — staleness gate in _data_quality_flag
# ---------------------------------------------------------------------------


def test_flag_stale_when_nav_ends_30_trading_days_before_as_of(monkeypatch):
    _patch_calendar(monkeypatch)
    aligned = _nav_frame(end_lag_bdays=31, span_years=4.0)
    flag = orchestrator._data_quality_flag(
        aligned, min_years=3.0, as_of=AS_OF, max_nav_lag_bdays=10,
    )
    assert flag == "STALE"


def test_flag_good_when_nav_ends_3_trading_days_before_as_of(monkeypatch):
    _patch_calendar(monkeypatch)
    aligned = _nav_frame(end_lag_bdays=4, span_years=4.0)
    flag = orchestrator._data_quality_flag(
        aligned, min_years=3.0, as_of=AS_OF, max_nav_lag_bdays=10,
    )
    assert flag == "GOOD"


def test_flag_staleness_exact_boundary(monkeypatch):
    """Last NAV exactly on the N-th-from-last trading date is NOT stale;
    one trading day earlier is."""
    _patch_calendar(monkeypatch)
    on_boundary = _nav_frame(end_lag_bdays=10, span_years=4.0)
    past_boundary = _nav_frame(end_lag_bdays=11, span_years=4.0)
    kw = dict(min_years=3.0, as_of=AS_OF, max_nav_lag_bdays=10)
    assert orchestrator._data_quality_flag(on_boundary, **kw) == "GOOD"
    assert orchestrator._data_quality_flag(past_boundary, **kw) == "STALE"


def test_insufficient_history_takes_precedence_over_stale(monkeypatch):
    """A short-history scheme that is ALSO stale reports the first failing
    check (INSUFFICIENT_HISTORY), matching filter application order."""
    _patch_calendar(monkeypatch)
    aligned = _nav_frame(end_lag_bdays=31, span_years=2.0)
    flag = orchestrator._data_quality_flag(
        aligned, min_years=3.0, as_of=AS_OF, max_nav_lag_bdays=10,
    )
    assert flag == "INSUFFICIENT_HISTORY"


def test_staleness_gate_off_without_as_of(monkeypatch):
    """Back-compat: when as_of / max_nav_lag_bdays aren't provided the gate
    is skipped (old behavior)."""
    _patch_calendar(monkeypatch)
    aligned = _nav_frame(end_lag_bdays=31, span_years=4.0)
    assert orchestrator._data_quality_flag(aligned, min_years=3.0) == "GOOD"


# ---------------------------------------------------------------------------
# A1-5 — config wiring of min_history_years_for_ranking
# ---------------------------------------------------------------------------


def test_yaml_min_history_gate_is_3_years():
    """The live deliberate gate is 3y; wiring the config must not silently
    shrink the GOOD universe to 5y."""
    assert get_pipeline_config().quality.min_history_years_for_ranking == 3


def _phase1_flag_with_min_years(monkeypatch, min_years: int) -> str:
    _patch_calendar(monkeypatch)
    aligned = _full_aligned(span_years=3.5)
    monkeypatch.setattr(
        orchestrator.alignment, "align_scheme", lambda *_a, **_k: aligned,
    )
    monkeypatch.setattr(
        orchestrator, "get_pipeline_config",
        lambda: _cfg_copy(min_history_years_for_ranking=min_years),
    )
    row = orchestrator.compute_phase1_for_scheme(
        "TEST", "NIFTY 500 TRI", "Flexi Cap", AS_OF,
    )
    return row["data_quality_flag"]


def test_config_min_history_is_honored_4y_rejects_3p5y_span(monkeypatch):
    assert _phase1_flag_with_min_years(monkeypatch, 4) == "INSUFFICIENT_HISTORY"


def test_config_min_history_is_honored_3y_accepts_3p5y_span(monkeypatch):
    assert _phase1_flag_with_min_years(monkeypatch, 3) == "GOOD"


# ---------------------------------------------------------------------------
# A1-9 — counted skips + halt threshold
# ---------------------------------------------------------------------------


def _eligible_frame(n: int) -> pl.DataFrame:
    return pl.DataFrame({
        "scheme_code": [f"S{i:03d}" for i in range(n)],
        "benchmark_ticker": ["NIFTY 500 TRI"] * n,
        "canonical_category": ["Flexi Cap"] * n,
    })


def _patch_phase1_run(monkeypatch, n: int, failing: set[str], max_rate: float):
    monkeypatch.setattr(
        orchestrator, "get_pipeline_config",
        lambda: _cfg_copy(max_scheme_compute_failure_rate=max_rate),
    )
    monkeypatch.setattr(
        orchestrator.q, "scheme_master", lambda **_kw: _eligible_frame(n),
    )

    def fake_compute(code, bench, cat, as_of, step="1w"):
        if code in failing:
            raise ValueError(f"boom {code}")
        return {"scheme_code": code, "data_quality_flag": "GOOD"}

    monkeypatch.setattr(orchestrator, "compute_phase1_for_scheme", fake_compute)
    upserts: list[pl.DataFrame] = []
    monkeypatch.setattr(
        orchestrator.w, "upsert_computed_metrics_phase1",
        lambda df, as_of: upserts.append(df) or df.height,
    )
    return upserts


def test_run_phase1_halts_above_failure_threshold_without_writing(monkeypatch):
    failing = {"S000", "S001"}  # 2/10 = 20% > 10%
    upserts = _patch_phase1_run(monkeypatch, n=10, failing=failing, max_rate=0.10)
    with pytest.raises(PipelineError) as ei:
        orchestrator.run_phase1(as_of=AS_OF, skip_freshness=True)
    msg = str(ei.value)
    assert "S000" in msg and "S001" in msg
    assert "2/10" in msg
    assert upserts == []  # halt happens BEFORE any partition write


def test_run_phase1_below_threshold_skips_and_reports(monkeypatch):
    failing = {"S003"}  # 1/10 = 10% < 50%
    upserts = _patch_phase1_run(monkeypatch, n=10, failing=failing, max_rate=0.50)
    df = orchestrator.run_phase1(as_of=AS_OF, skip_freshness=True)
    assert df.height == 9  # n - k rows survive
    assert "S003" not in df["scheme_code"].to_list()
    assert len(upserts) == 1 and upserts[0].height == 9


def test_run_phase1_all_failures_halts(monkeypatch):
    """Total breakage (every scheme raises) must halt, never write an empty
    partition."""
    failing = {f"S{i:03d}" for i in range(5)}
    upserts = _patch_phase1_run(monkeypatch, n=5, failing=failing, max_rate=0.01)
    with pytest.raises(PipelineError):
        orchestrator.run_phase1(as_of=AS_OF, skip_freshness=True)
    assert upserts == []


def test_run_phase2_halts_above_failure_threshold(monkeypatch):
    monkeypatch.setattr(
        orchestrator, "get_pipeline_config",
        lambda: _cfg_copy(max_scheme_compute_failure_rate=0.10),
    )
    existing = pl.DataFrame({
        "scheme_code": [f"S{i}" for i in range(10)],
        "beta_3y_std": [0.1] * 10,
        "r_squared_3y_mean": [0.9] * 10,
        "benchmark_ticker": ["NIFTY 500 TRI"] * 10,
    })
    monkeypatch.setattr(orchestrator.q, "computed_metrics_at", lambda as_of: existing)

    def fake_phase2(code, **_kw):
        if code in ("S0", "S1"):
            raise RuntimeError("db type change")
        return {
            "scheme_code": code,
            "style_drift_3y": None,
            "active_share_median_1y": None,
            "ptr_latest": None,
            "aum_impact_cost_days": None,
        }

    monkeypatch.setattr(orchestrator, "compute_phase2_for_scheme", fake_phase2)
    updates: list[pl.DataFrame] = []
    monkeypatch.setattr(
        orchestrator.w, "update_computed_metrics_phase2",
        lambda df, as_of: updates.append(df) or df.height,
    )
    with pytest.raises(PipelineError) as ei:
        orchestrator.run_phase2(as_of=AS_OF)
    assert "S0" in str(ei.value) and "S1" in str(ei.value)
    assert updates == []
