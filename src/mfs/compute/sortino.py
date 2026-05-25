"""Rolling 3Y Sortino ratio using MAR = daily risk-free rate.

Sortino = (mean(R - MAR) * 252) / (downside_dev * sqrt(252))
where downside_dev = sqrt(mean(min(0, R - MAR)^2)) over the window.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from mfs.compute.rolling import iter_windows
from mfs.config import get_pipeline_config


def rolling_sortino(aligned: pl.DataFrame, window_years: int = 3, step: str = "1w") -> float | None:
    cfg = get_pipeline_config()
    if aligned.is_empty():
        return None
    df = aligned.select(["date", "fund_log_ret", "rate_daily_log"]).drop_nulls()
    if df.is_empty():
        return None
    pdf = df.to_pandas().set_index("date").sort_index()
    n_days_year = cfg.returns.trading_days_per_year

    vals: list[float] = []
    for window in iter_windows(pdf, window_years, step):
        excess = window["fund_log_ret"].values - window["rate_daily_log"].values
        downside = np.minimum(0.0, excess)
        dd = float(np.sqrt(np.mean(downside ** 2)))
        if dd == 0:
            continue
        mu_ann = float(np.mean(excess)) * n_days_year
        dd_ann = dd * np.sqrt(n_days_year)
        vals.append(mu_ann / dd_ann)

    if not vals:
        return None
    return float(np.median(vals))
