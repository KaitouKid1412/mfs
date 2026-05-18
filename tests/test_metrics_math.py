"""Golden cross-checks for the metric math against hand-computed expectations."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from mfs.compute import alpha, capture, info_ratio, returns, sortino


def _synthetic_aligned(
    n_days: int = 252 * 4,
    fund_drift_per_day: float = 0.0001,
    beta: float = 1.0,
    seed: int = 42,
    rf_daily: float = 0.000247,  # ~6.5% annualized
    noise_scale: float = 0.002,
):
    """Construct a synthetic aligned frame with known fund/bench/rf properties.
    fund_log_ret_t = rf + beta * (bench_excess_t) + drift + noise
    """
    rng = np.random.default_rng(seed)
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(n_days)]
    bench_log_ret = rng.normal(loc=0.00045, scale=0.011, size=n_days)
    noise = (
        rng.normal(loc=0.0, scale=noise_scale, size=n_days)
        if noise_scale > 0
        else np.zeros(n_days)
    )
    bench_excess = bench_log_ret - rf_daily
    fund_log_ret = rf_daily + beta * bench_excess + fund_drift_per_day + noise

    # Build NAV from log returns (start at 100)
    nav = np.exp(np.cumsum(fund_log_ret)) * 100.0
    bench_close = np.exp(np.cumsum(bench_log_ret)) * 1000.0

    return pl.DataFrame(
        {
            "date": dates,
            "nav": nav.tolist(),
            "bench_close": bench_close.tolist(),
            "fund_log_ret": fund_log_ret.tolist(),
            "bench_log_ret": bench_log_ret.tolist(),
            "rate_daily": [rf_daily] * n_days,
        }
    ).with_columns(pl.col("date").cast(pl.Date))


def test_single_window_alpha_recovers_drift_noise_free():
    """Deterministic recovery: with drift 0.0001 daily and beta=1, no noise → alpha_ann ≈ 0.0255."""
    aligned = _synthetic_aligned(fund_drift_per_day=0.0001, beta=1.0, noise_scale=0.0)
    out = alpha.single_window_alpha_beta_r2(aligned, window_years=3)
    assert out["alpha_ann"] is not None
    # Exact: (1.0001)^252 - 1 = 0.02551..
    assert abs(out["alpha_ann"] - 0.02551) < 1e-4
    assert abs(out["beta"] - 1.0) < 1e-6
    assert out["r2"] > 0.99


def test_single_window_beta_recovers_noise_free():
    aligned = _synthetic_aligned(fund_drift_per_day=0.0, beta=1.3, noise_scale=0.0)
    out = alpha.single_window_alpha_beta_r2(aligned, window_years=3)
    assert abs(out["beta"] - 1.3) < 1e-6
    assert abs(out["alpha_ann"]) < 1e-6
    assert out["r2"] > 0.99


def test_rolling_returns_distribution_positive_drift():
    aligned = _synthetic_aligned(n_days=252 * 6, fund_drift_per_day=0.0002, beta=1.0)
    out = returns.rolling_distribution(aligned, window_years=3, step="1w")
    assert out["median"] is not None
    assert out["p25"] is not None
    # 0.0002 daily drift + bench 0.00045 → fund ~ 0.00065/day ≈ 17% annualized
    assert 0.07 < out["median"] < 0.35


def test_capture_efficiency_asymmetric_fund():
    """Build a fund that captures 100% up, 50% down → efficiency = 2.0."""
    rng = np.random.default_rng(7)
    n = 252 * 6
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(n)]
    bench = rng.normal(loc=0.0, scale=0.01, size=n)
    fund = np.where(bench > 0, bench * 1.0, bench * 0.5)
    aligned = pl.DataFrame({
        "date": dates,
        "nav": (np.exp(np.cumsum(fund)) * 100).tolist(),
        "bench_close": (np.exp(np.cumsum(bench)) * 1000).tolist(),
        "fund_log_ret": fund.tolist(),
        "bench_log_ret": bench.tolist(),
        "rate_daily": [0.0] * n,
    }).with_columns(pl.col("date").cast(pl.Date))
    out = capture.rolling_capture(aligned, window_years=3, step="1w")
    assert out["capture_up"] is not None
    assert out["capture_down"] is not None
    assert 0.85 < out["capture_up"] < 1.15
    assert 0.35 < out["capture_down"] < 0.65
    assert 1.6 < out["capture_efficiency"] < 2.5


def test_sortino_positive_for_alpha_fund():
    aligned = _synthetic_aligned(n_days=252 * 6, fund_drift_per_day=0.0003, beta=1.0)
    s = sortino.rolling_sortino(aligned, window_years=3, step="1w")
    assert s is not None and s > 0


def test_info_ratio_positive_for_alpha_fund():
    aligned = _synthetic_aligned(n_days=252 * 6, fund_drift_per_day=0.0003, beta=1.0)
    ir = info_ratio.rolling_information_ratio(aligned, window_years=3, step="1w")
    assert ir is not None and ir > 0


def test_alpha_returns_none_for_short_history():
    aligned = _synthetic_aligned(n_days=30)
    out = alpha.rolling_alpha_beta_r2(aligned, window_years=3, step="1w")
    assert out["alpha_ann"] is None
    assert out["beta"] is None
