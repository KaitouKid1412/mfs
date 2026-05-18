"""Rolling 3Y Information Ratio.

IR = mean(fund_ret - bench_ret) / std(fund_ret - bench_ret), annualized by sqrt(252).
Reported as median across rolling windows.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import polars as pl

from mfs.config import get_pipeline_config


def rolling_information_ratio(
    aligned: pl.DataFrame, window_years: int = 3, step: str = "1w"
) -> float | None:
    cfg = get_pipeline_config()
    if aligned.is_empty():
        return None
    df = aligned.select(["date", "fund_log_ret", "bench_log_ret"]).drop_nulls()
    if df.is_empty():
        return None
    pdf = df.to_pandas().set_index("date").sort_index()
    n_days_year = cfg.returns.trading_days_per_year
    win_days = int(365.25 * window_years)
    approx_obs = int(252 * window_years)
    if len(pdf) < approx_obs - 30:
        return None
    step_days = 7 if step == "1w" else 1

    end = pdf.index.max()
    cur = pdf.index.min() + pd.Timedelta(days=win_days)
    irs: list[float] = []
    while cur <= end:
        sp = cur - pd.Timedelta(days=win_days)
        window = pdf.loc[sp:cur]
        if len(window) < approx_obs - 30:
            cur += pd.Timedelta(days=step_days)
            continue
        diff = window["fund_log_ret"].values - window["bench_log_ret"].values
        sd = float(np.std(diff, ddof=1))
        if sd == 0:
            cur += pd.Timedelta(days=step_days)
            continue
        mu = float(np.mean(diff))
        ir = (mu * n_days_year) / (sd * np.sqrt(n_days_year))
        irs.append(ir)
        cur += pd.Timedelta(days=step_days)

    if not irs:
        return None
    return float(np.median(irs))
