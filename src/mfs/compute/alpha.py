"""Rolling 3Y Jensen's alpha via OLS: r_f - r_rf = alpha + beta*(r_b - r_rf) + eps.

Returns the median annualized alpha (and median beta, R²) across rolling windows.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import polars as pl
import statsmodels.api as sm

from mfs.config import get_pipeline_config


def rolling_alpha_beta_r2(aligned: pl.DataFrame, window_years: int = 3, step: str = "1w") -> dict:
    """Return {"alpha_ann": float|None, "alpha_tstat": float|None, "beta": float|None, "r2": float|None}.

    Uses log returns. Alpha is the daily intercept; annualized by trading_days_per_year.
    Aggregation: median across windows for alpha/beta/R²; mean of |t| for tstat (use median of t).
    """
    cfg = get_pipeline_config()
    if aligned.is_empty():
        return {"alpha_ann": None, "alpha_tstat": None, "beta": None, "r2": None}
    df = aligned.select(["date", "fund_log_ret", "bench_log_ret", "rate_daily"]).drop_nulls()
    if df.is_empty():
        return {"alpha_ann": None, "alpha_tstat": None, "beta": None, "r2": None}

    # Convert to pandas for statsmodels
    pdf = df.to_pandas().set_index("date").sort_index()
    win_days = int(365.25 * window_years)
    n_days_year = cfg.returns.trading_days_per_year

    # Approximate trading days in the window
    approx_obs = int(252 * window_years)
    if len(pdf) < approx_obs - 30:
        return {"alpha_ann": None, "alpha_tstat": None, "beta": None, "r2": None}

    if step == "1w":
        step_days = 7
    elif step == "1d":
        step_days = 1
    else:
        raise ValueError(f"unsupported step: {step}")

    end = pdf.index.max()
    cur = pdf.index.min() + pd.Timedelta(days=win_days)
    alphas: list[float] = []
    tstats: list[float] = []
    betas: list[float] = []
    r2s: list[float] = []

    while cur <= end:
        sp = cur - pd.Timedelta(days=win_days)
        window = pdf.loc[sp:cur]
        if len(window) < approx_obs - 30:
            cur += pd.Timedelta(days=step_days)
            continue
        excess_fund = window["fund_log_ret"] - window["rate_daily"]
        excess_bench = window["bench_log_ret"] - window["rate_daily"]
        X = sm.add_constant(excess_bench.values)
        y = excess_fund.values
        try:
            res = sm.OLS(y, X).fit()
            a_daily = float(res.params[0])
            b = float(res.params[1])
            tstat = float(res.tvalues[0])
            r2 = float(res.rsquared)
            alpha_ann = (1.0 + a_daily) ** n_days_year - 1.0
            alphas.append(alpha_ann)
            tstats.append(tstat)
            betas.append(b)
            r2s.append(r2)
        except Exception:  # noqa: BLE001
            pass
        cur += pd.Timedelta(days=step_days)

    if not alphas:
        return {"alpha_ann": None, "alpha_tstat": None, "beta": None, "r2": None}
    return {
        "alpha_ann": float(np.median(alphas)),
        "alpha_tstat": float(np.median(tstats)),
        "beta": float(np.median(betas)),
        "r2": float(np.median(r2s)),
    }


def single_window_alpha_beta_r2(
    aligned: pl.DataFrame, window_years: int = 3
) -> dict:
    """Compute one Jensen's alpha regression over the trailing window_years using all data
    present in aligned. Useful for tests and audit. Returns daily alpha, beta, R², t-stat."""
    cfg = get_pipeline_config()
    df = aligned.select(["date", "fund_log_ret", "bench_log_ret", "rate_daily"]).drop_nulls()
    if df.is_empty():
        return {"alpha_ann": None, "alpha_tstat": None, "beta": None, "r2": None}
    pdf = df.to_pandas().set_index("date").sort_index()
    end = pdf.index.max()
    start = end - pd.Timedelta(days=int(365.25 * window_years))
    window = pdf.loc[start:end]
    if window.empty:
        return {"alpha_ann": None, "alpha_tstat": None, "beta": None, "r2": None}
    excess_fund = window["fund_log_ret"] - window["rate_daily"]
    excess_bench = window["bench_log_ret"] - window["rate_daily"]
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
