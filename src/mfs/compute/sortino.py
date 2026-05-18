"""Rolling 3Y Sortino ratio using MAR = daily risk-free rate.

Sortino = (mean(R - MAR) * 252) / (downside_dev * sqrt(252))
where downside_dev = sqrt(mean(min(0, R - MAR)^2)) over the window.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import polars as pl

from mfs.config import get_pipeline_config


def rolling_sortino(aligned: pl.DataFrame, window_years: int = 3, step: str = "1w") -> float | None:
    cfg = get_pipeline_config()
    if aligned.is_empty():
        return None
    df = aligned.select(["date", "fund_log_ret", "rate_daily"]).drop_nulls()
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
    vals: list[float] = []
    while cur <= end:
        sp = cur - pd.Timedelta(days=win_days)
        window = pdf.loc[sp:cur]
        if len(window) < approx_obs - 30:
            cur += pd.Timedelta(days=step_days)
            continue
        excess = window["fund_log_ret"].values - window["rate_daily"].values
        downside = np.minimum(0.0, excess)
        dd = float(np.sqrt(np.mean(downside ** 2)))
        if dd == 0:
            cur += pd.Timedelta(days=step_days)
            continue
        mu_ann = float(np.mean(excess)) * n_days_year
        dd_ann = dd * np.sqrt(n_days_year)
        vals.append(mu_ann / dd_ann)
        cur += pd.Timedelta(days=step_days)

    if not vals:
        return None
    return float(np.median(vals))


def peer_percentile(values: dict[str, float | None]) -> dict[str, float | None]:
    """Convert a category's {scheme_code: sortino} into {scheme_code: percentile (0..1)}."""
    items = [(k, v) for k, v in values.items() if v is not None and not np.isnan(v)]
    if len(items) < 2:
        return {k: None for k in values}
    sorted_vals = sorted(v for _, v in items)
    out: dict[str, float | None] = {k: None for k in values}
    import bisect

    for k, v in items:
        idx = bisect.bisect_left(sorted_vals, v)
        out[k] = idx / (len(sorted_vals) - 1) if len(sorted_vals) > 1 else None
    return out
