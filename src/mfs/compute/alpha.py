"""Rolling 3Y Jensen's alpha via OLS: r_f - r_rf = alpha + beta*(r_b - r_rf) + eps.

Returns the median annualized alpha (and median beta, R²) across rolling windows.

Per-window t-stat filter: only windows with `tstat >= ALPHA_TSTAT_FILTER_MIN`
contribute to the reported alpha and alpha_tstat. This restricts the reported
alpha to periods where the regression provided meaningful evidence of a
positive intercept. Beta and R² are diagnostics of the fund/benchmark
relationship and are reported over *all* windows so downstream filters (R²
band, beta band) keep their full sample.

If no windows pass the t-stat threshold, alpha and alpha_tstat are None — the
scheme then drops out of ranking via the existing not-null filter, even if
its beta/R² are otherwise reasonable.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import polars as pl
import statsmodels.api as sm

from mfs.compute.rolling import iter_windows
from mfs.config import get_pipeline_config

# Minimum per-window t-stat for that window's alpha to be counted (one-sided,
# positive). At ~750 daily obs and 1 regressor, t ≥ 1.0 corresponds to ~84%
# confidence that alpha is positive.
ALPHA_TSTAT_FILTER_MIN = 1.0


def rolling_alpha_beta_r2(aligned: pl.DataFrame, window_years: int = 3, step: str = "1w") -> dict:
    """Return {"alpha_ann", "alpha_tstat", "beta", "r2", "beta_std", "r2_mean"}.

    Uses log returns. Alpha is the daily intercept; annualized by trading_days_per_year.
    Aggregation: median across the t-stat-filtered window set for alpha/t-stat;
    median across *all* windows for beta/R²; sample-std of beta and mean of R²
    across all windows feed the Phase 2 Style Drift metric.
    """
    cfg = get_pipeline_config()
    empty = {
        "alpha_ann": None, "alpha_tstat": None,
        "beta": None, "r2": None,
        "beta_std": None, "r2_mean": None,
    }
    if aligned.is_empty():
        return dict(empty)
    df = aligned.select(
        ["date", "fund_log_ret", "bench_log_ret", "rate_daily_log"]
    ).drop_nulls()
    if df.is_empty():
        return dict(empty)

    pdf = df.to_pandas().set_index("date").sort_index()
    n_days_year = cfg.returns.trading_days_per_year

    alphas: list[float] = []
    tstats: list[float] = []
    betas: list[float] = []
    r2s: list[float] = []
    for window in iter_windows(pdf, window_years, step):
        excess_fund = window["fund_log_ret"] - window["rate_daily_log"]
        excess_bench = window["bench_log_ret"] - window["rate_daily_log"]
        X = sm.add_constant(excess_bench.values)
        y = excess_fund.values
        try:
            res = sm.OLS(y, X).fit()
            a_daily = float(res.params[0])
            b = float(res.params[1])
            tstat = float(res.tvalues[0])
            r2 = float(res.rsquared)
            alpha_ann = (1.0 + a_daily) ** n_days_year - 1.0
            betas.append(b)
            r2s.append(r2)
            if tstat >= ALPHA_TSTAT_FILTER_MIN:
                alphas.append(alpha_ann)
                tstats.append(tstat)
        except Exception:  # noqa: BLE001
            pass

    median_beta = float(np.median(betas)) if betas else None
    median_r2 = float(np.median(r2s)) if r2s else None
    # ddof=1 sample std. Needs ≥2 windows; single-window scheme has no drift signal.
    beta_std = float(np.std(betas, ddof=1)) if len(betas) >= 2 else None
    r2_mean = float(np.mean(r2s)) if r2s else None
    out = {
        "alpha_ann": float(np.median(alphas)) if alphas else None,
        "alpha_tstat": float(np.median(tstats)) if tstats else None,
        "beta": median_beta,
        "r2": median_r2,
        "beta_std": beta_std,
        "r2_mean": r2_mean,
    }
    return out


def single_window_alpha_beta_r2(
    aligned: pl.DataFrame, window_years: int = 3
) -> dict:
    """Compute one Jensen's alpha regression over the trailing window_years using all data
    present in aligned. Useful for tests and audit. Returns daily alpha, beta, R², t-stat."""
    cfg = get_pipeline_config()
    df = aligned.select(
        ["date", "fund_log_ret", "bench_log_ret", "rate_daily_log"]
    ).drop_nulls()
    if df.is_empty():
        return {"alpha_ann": None, "alpha_tstat": None, "beta": None, "r2": None}
    pdf = df.to_pandas().set_index("date").sort_index()
    end = pdf.index.max()
    start = end - pd.Timedelta(days=int(365.25 * window_years))
    window = pdf.loc[start:end]
    if window.empty:
        return {"alpha_ann": None, "alpha_tstat": None, "beta": None, "r2": None}
    excess_fund = window["fund_log_ret"] - window["rate_daily_log"]
    excess_bench = window["bench_log_ret"] - window["rate_daily_log"]
    X = sm.add_constant(excess_bench.values)
    y = excess_fund.values
    res = sm.OLS(y, X).fit()
    a_daily = float(res.params[0])
    return {
        "alpha_ann": (1.0 + a_daily) ** cfg.returns.trading_days_per_year - 1.0,
        "alpha_tstat": float(res.tvalues[0]),
        "beta": float(res.params[1]),
        "r2": float(res.rsquared),
    }
