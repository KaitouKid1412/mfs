"""Golden cross-checks for the metric math against hand-computed expectations."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import polars as pl
import pytest

from mfs.compute import alpha, capture, info_ratio, returns, rolling, sortino


def _synthetic_aligned(
    n_days: int = 252 * 4,
    fund_drift_per_day: float = 0.0001,
    beta: float = 1.0,
    seed: int = 42,
    rf_daily: float = 0.000247,  # ~6.5% annualized
    noise_scale: float = 0.002,
):
    """Construct a synthetic aligned frame with known fund/bench/rf properties.
    fund_log_ret_t = rf_log + beta * (bench_excess_t) + drift + noise

    Model is in log space, so we use rf_log = ln(1 + rf_daily) when constructing
    fund_log_ret. The simple `rate_daily` is also exposed for callers that need it.
    """
    rng = np.random.default_rng(seed)
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(n_days)]
    bench_log_ret = rng.normal(loc=0.00045, scale=0.011, size=n_days)
    noise = (
        rng.normal(loc=0.0, scale=noise_scale, size=n_days)
        if noise_scale > 0
        else np.zeros(n_days)
    )
    rf_log = float(np.log(1.0 + rf_daily))
    bench_excess = bench_log_ret - rf_log
    fund_log_ret = rf_log + beta * bench_excess + fund_drift_per_day + noise

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
            "rate_daily_log": [rf_log] * n_days,
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
        "rate_daily_log": [0.0] * n,
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


def test_rolling_alpha_negative_drift_reports_negative_alpha():
    """Signed alpha (locked D1): a fund with constant -0.0001/day drift vs its
    benchmark, beta=1, no noise — every window's intercept is exactly the
    drift, so the median annualized alpha is (1 - 0.0001)^252 - 1 ≈ -0.02489.
    Under the old t>=+1.0 censor this was unrepresentable (alpha came back
    None); negative alpha must now survive end-to-end."""
    aligned = _synthetic_aligned(
        n_days=252 * 6, fund_drift_per_day=-0.0001, beta=1.0, noise_scale=0.0
    )
    out = alpha.rolling_alpha_beta_r2(aligned, window_years=3, step="1w")
    assert out["alpha_ann"] is not None and out["alpha_ann"] < 0
    expected = (1.0 - 0.0001) ** 252 - 1.0  # ≈ -0.024886
    assert abs(out["alpha_ann"] - expected) < 1e-3
    assert out["alpha_tstat"] is not None and out["alpha_tstat"] < 0
    # Noise-free ⇒ overwhelming |t| evidence in every window: confidence 1.0
    # even though the alpha is negative — confidence is two-sided by design.
    assert out["alpha_confidence"] == 1.0


def test_rolling_alpha_pure_noise_signed_near_zero_low_confidence():
    """A fund with zero drift and only noise (weak signal). Pre-D1 the t-stat
    censor dropped every window and reported alpha_ann = None; now the median
    over ALL windows is reported: non-null, signed, near zero — with low
    alpha_confidence flagging the weak evidence. Confidence is display-only
    and must never null out or filter the alpha itself."""
    aligned = _synthetic_aligned(
        n_days=252 * 6, fund_drift_per_day=0.0, beta=1.0, noise_scale=0.005
    )
    out = alpha.rolling_alpha_beta_r2(aligned, window_years=3, step="1w")
    # Signed alpha over all windows: non-null and ~0 (seeded noise wobble only).
    assert out["alpha_ann"] is not None
    assert abs(out["alpha_ann"]) < 0.05
    assert out["alpha_tstat"] is not None
    assert abs(out["alpha_tstat"]) < 1.0
    # Few windows carry |t| >= 1 evidence for a true-zero alpha.
    assert out["alpha_confidence"] is not None
    assert out["alpha_confidence"] < 0.5
    # Beta/R² diagnostics unchanged by D1: still over all windows.
    assert out["beta"] is not None
    assert out["r2"] is not None
    assert abs(out["beta"] - 1.0) < 0.2


def test_alpha_confidence_bounds_and_strong_signal():
    """alpha_confidence is a share in [0, 1]; a persistent strong-drift fund
    (per-window t ≈ 6) has every window above the |t| threshold → exactly 1.0."""
    aligned = _synthetic_aligned(
        n_days=252 * 6, fund_drift_per_day=0.0006, beta=1.0, noise_scale=0.003
    )
    out = alpha.rolling_alpha_beta_r2(aligned, window_years=3, step="1w")
    assert out["alpha_confidence"] is not None
    assert 0.0 <= out["alpha_confidence"] <= 1.0
    assert out["alpha_confidence"] == 1.0


def test_rolling_alpha_accepts_strongly_positive_drift():
    """A fund with persistent +0.0006/day drift in noisy regime should pass the
    t-stat filter in every window and report a positive alpha_ann."""
    aligned = _synthetic_aligned(
        n_days=252 * 6, fund_drift_per_day=0.0006, beta=1.0, noise_scale=0.003
    )
    out = alpha.rolling_alpha_beta_r2(aligned, window_years=3, step="1w")
    assert out["alpha_ann"] is not None and out["alpha_ann"] > 0.05
    assert out["alpha_tstat"] is not None and out["alpha_tstat"] >= 1.0


def test_alpha_returns_none_for_short_history():
    aligned = _synthetic_aligned(n_days=30)
    out = alpha.rolling_alpha_beta_r2(aligned, window_years=3, step="1w")
    assert out["alpha_ann"] is None
    assert out["beta"] is None
    # No computable windows → no confidence share either.
    assert out["alpha_confidence"] is None


def test_rolling_alpha_emits_beta_std_and_r2_mean():
    """Phase 2: rolling_alpha_beta_r2 must expose beta_std and r2_mean for the
    style_drift derivation. A stable-beta fund should have low beta_std; an
    almost-perfect linear fund should have r2_mean very close to 1."""
    aligned = _synthetic_aligned(
        n_days=252 * 6, fund_drift_per_day=0.0001, beta=1.0, noise_scale=0.0005
    )
    out = alpha.rolling_alpha_beta_r2(aligned, window_years=3, step="1w")
    assert "beta_std" in out and "r2_mean" in out
    assert out["beta_std"] is not None
    assert out["r2_mean"] is not None
    # With tiny noise and a fixed true beta, sample std across windows is small.
    assert out["beta_std"] < 0.05
    # R² across windows hugs 1.0 since the linear fit is essentially exact.
    assert out["r2_mean"] > 0.95


def test_beta_std_picks_up_drift_in_beta():
    """A fund whose beta drifts mid-history should produce a larger beta_std
    than a fund with constant beta. Style Drift = beta_std + (1 - r2_mean)
    relies on this signal."""
    rng = np.random.default_rng(11)
    n = 252 * 6
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(n)]
    bench_log_ret = rng.normal(loc=0.0004, scale=0.011, size=n)
    rf_log = float(np.log(1.0 + 0.000247))
    bench_excess = bench_log_ret - rf_log
    # Beta = 0.7 for the first half, 1.3 for the second half. Rolling windows
    # straddling the break will see a moving slope.
    betas = np.where(np.arange(n) < n // 2, 0.7, 1.3)
    fund_log_ret = rf_log + betas * bench_excess + rng.normal(0.0, 0.001, size=n)
    aligned = pl.DataFrame(
        {
            "date": dates,
            "nav": (np.exp(np.cumsum(fund_log_ret)) * 100).tolist(),
            "bench_close": (np.exp(np.cumsum(bench_log_ret)) * 1000).tolist(),
            "fund_log_ret": fund_log_ret.tolist(),
            "bench_log_ret": bench_log_ret.tolist(),
            "rate_daily": [0.000247] * n,
            "rate_daily_log": [rf_log] * n,
        }
    ).with_columns(pl.col("date").cast(pl.Date))
    drifting = alpha.rolling_alpha_beta_r2(aligned, window_years=3, step="1w")

    stable = alpha.rolling_alpha_beta_r2(
        _synthetic_aligned(n_days=252 * 6, fund_drift_per_day=0.0001, beta=1.0,
                           noise_scale=0.001),
        window_years=3, step="1w",
    )
    assert drifting["beta_std"] is not None and stable["beta_std"] is not None
    assert drifting["beta_std"] > stable["beta_std"] * 2.0


# ---------------------------------------------------------------------------
# A1-4 — trailing-endpoint window epoch
# ---------------------------------------------------------------------------


def test_trim_to_trailing_endpoints_limits_endpoint_span():
    """A1-4: for a 10y daily frame trimmed for 3y windows, iter_windows
    endpoints span at most window_years + slack (+ one step). iter_windows
    applies the same trim internally, so feeding it the untrimmed frame walks
    the identical window set — that internal trim is what re-epochs
    alpha/sortino/IR/capture without touching their modules."""
    n = int(365.25 * 10)
    idx = pd.date_range("2014-01-01", periods=n, freq="D")
    pdf = pd.DataFrame({"x": np.zeros(n)}, index=idx)

    trimmed = rolling.trim_to_trailing_endpoints(pdf, window_years=3)
    endpoints = [w.index.max() for w in rolling.iter_windows(trimmed, 3, "1w")]
    assert endpoints
    span_days = (max(endpoints) - min(endpoints)).days
    assert span_days <= int(365.25 * (3 + rolling.TRAILING_SLACK_YEARS)) + 7
    # ~159 weekly endpoints in one trailing-3y span (spec's 2013-vintage
    # figure), not the ~365 an untrimmed 10y walk would produce.
    assert 150 <= len(endpoints) <= 170

    endpoints_untrimmed_input = [
        w.index.max() for w in rolling.iter_windows(pdf, 3, "1w")
    ]
    assert endpoints_untrimmed_input == endpoints


def test_rolling_alpha_trailing_epoch_ignores_stale_outperformance():
    """A1-4 regime-change fixture: 8y series with strong positive fund drift
    (+0.0012/day, ~35%/yr) in years 0-3 and ZERO excess drift in years 3-8,
    beta=1, no noise. Under the trailing-endpoint convention every 3y window
    ENDPOINT falls in the trailing ~3y, so the median window contains no
    drift days and the median alpha is ~0. Pre-A1-4 (windows since
    inception) the median window still overlapped the drift era and this
    fixture reported ~+5%/yr — the 0.005 bound is the regression guard."""
    rng = np.random.default_rng(17)
    n = int(365.25 * 8)
    dates = [date(2016, 1, 1) + timedelta(days=i) for i in range(n)]
    bench_log_ret = rng.normal(loc=0.00045, scale=0.011, size=n)
    rf_log = float(np.log(1.0 + 0.000247))
    drift = np.where(np.arange(n) < int(365.25 * 3), 0.0012, 0.0)
    fund_log_ret = bench_log_ret + drift  # rf + 1.0*(bench-rf) + drift
    aligned = pl.DataFrame(
        {
            "date": dates,
            "nav": (np.exp(np.cumsum(fund_log_ret)) * 100).tolist(),
            "bench_close": (np.exp(np.cumsum(bench_log_ret)) * 1000).tolist(),
            "fund_log_ret": fund_log_ret.tolist(),
            "bench_log_ret": bench_log_ret.tolist(),
            "rate_daily": [0.000247] * n,
            "rate_daily_log": [rf_log] * n,
        }
    ).with_columns(pl.col("date").cast(pl.Date))
    out = alpha.rolling_alpha_beta_r2(aligned, window_years=3, step="1w")
    assert out["alpha_ann"] is not None
    assert abs(out["alpha_ann"]) < 0.005
    assert out["beta"] is not None and abs(out["beta"] - 1.0) < 0.05


# ---------------------------------------------------------------------------
# A1-6 — deterministic golden pins
# ---------------------------------------------------------------------------


def test_rolling_returns_exact_cagr_pin():
    """Deterministic golden pin (A1-6): NAV compounds at exactly 10%/yr in
    calendar time, so every window's calendar-CAGR
    (NAV_end/NAV_start)^(1/years) - 1 with years = days/365.25 is exactly
    0.10 regardless of window placement → median == p25 == 0.10. Guards
    against future annualization-formula swaps (e.g. the formerly-documented
    252/window_days trading-day exponent would NOT return 0.10)."""
    n = int(365.25 * 5)
    dates = [date(2019, 1, 1) + timedelta(days=i) for i in range(n)]
    nav = [100.0 * (1.10 ** (i / 365.25)) for i in range(n)]
    aligned = pl.DataFrame({"date": dates, "nav": nav}).with_columns(
        pl.col("date").cast(pl.Date)
    )
    out = returns.rolling_distribution(aligned, window_years=3, step="1w")
    assert out["median"] is not None and out["p25"] is not None
    assert abs(out["median"] - 0.10) < 1e-6
    assert abs(out["p25"] - 0.10) < 1e-6


def test_sortino_hand_computed_pin():
    """[REVIEW-EXTENDED A1-6] Exact-value Sortino pin on a single-window series.

    1096 calendar-daily rows = exactly one 3y window (first endpoint at
    min + 1095d == max; the next weekly step overshoots). Excess returns over
    MAR: +0.002 on 548 days, -0.001 on the other 548. Hand computation:
        mean(excess)  = (548*0.002 - 548*0.001)/1096 = 0.0005
        mu_ann        = 0.0005 * 252 = 0.126
        downside dev  = sqrt(548 * 0.001^2 / 1096) = 0.001/sqrt(2)
        dd_ann        = (0.001/sqrt(2)) * sqrt(252) = 0.001 * sqrt(126)
        Sortino       = 0.126 / (0.001*sqrt(126)) = sqrt(126) = 11.22497216...
    rf is non-zero, so a dropped MAR subtraction shifts the pin; a missing
    sqrt(252) annualization scales it ~15.9x. Both land far outside 1e-9."""
    n = 1096
    rf_log = 1e-4
    excess = np.array([0.002] * 548 + [-0.001] * 548)
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(n)]
    aligned = pl.DataFrame(
        {
            "date": dates,
            "fund_log_ret": (excess + rf_log).tolist(),
            "rate_daily_log": [rf_log] * n,
        }
    ).with_columns(pl.col("date").cast(pl.Date))
    s = sortino.rolling_sortino(aligned, window_years=3, step="1w")
    assert s is not None
    assert abs(s - math.sqrt(126)) < 1e-9


def test_info_ratio_hand_computed_pin():
    """[REVIEW-EXTENDED A1-6] Exact-value IR pin on a single-window series.

    1096 calendar-daily rows = exactly one 3y window. fund-bench diff:
    +0.003 on 548 days, -0.001 on the other 548. Hand computation:
        mean(diff)  = 0.001 (deviations are exactly +/-0.002)
        std(ddof=1) = sqrt(1096 * 0.002^2 / 1095) = 0.002*sqrt(1096/1095)
        IR          = (0.001*252) / (std * sqrt(252))
                    = 0.001*sqrt(252) / (0.002*sqrt(1096/1095)) = 7.9336275...
    Pinned with SAMPLE std (ddof=1): population std (ddof=0) would give
    7.9372539 — 3.6e-3 away, far outside 1e-9 — and a missing sqrt(252)
    scales the value ~15.9x. bench is a non-zero constant so a dropped
    benchmark subtraction also shifts the pin."""
    n = 1096
    bench_const = 2e-4
    diff = np.array([0.003] * 548 + [-0.001] * 548)
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(n)]
    aligned = pl.DataFrame(
        {
            "date": dates,
            "fund_log_ret": (diff + bench_const).tolist(),
            "bench_log_ret": [bench_const] * n,
        }
    ).with_columns(pl.col("date").cast(pl.Date))
    ir = info_ratio.rolling_information_ratio(aligned, window_years=3, step="1w")
    assert ir is not None
    expected = 0.001 * math.sqrt(252) / (0.002 * math.sqrt(1096 / 1095))
    assert abs(ir - expected) < 1e-9
