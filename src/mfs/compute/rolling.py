"""Shared rolling-window iterator used by every per-fund metric.

Every metric in mfs.compute (alpha, sortino, info_ratio, capture) walks a
daily-indexed pandas DataFrame in rolling N-year windows stepped weekly or
daily. This module centralizes that loop so the metric modules can focus on
the per-window math.

A window passes if it contains at least `252 * window_years - min_obs_relax`
observations. The relaxation accommodates holidays / short months.
"""

from __future__ import annotations

from typing import Iterator

import pandas as pd


def iter_windows(
    pdf: pd.DataFrame,
    window_years: int,
    step: str = "1w",
    min_obs_relax: int = 30,
) -> Iterator[pd.DataFrame]:
    """Yield rolling sub-frames of `pdf` for the given window length.

    Yields nothing if `pdf` has insufficient total observations. Individual
    short windows are silently skipped.
    """
    win_days = int(365.25 * window_years)
    approx_obs = int(252 * window_years)
    if len(pdf) < approx_obs - min_obs_relax:
        return

    if step == "1w":
        step_days = 7
    elif step == "1d":
        step_days = 1
    else:
        raise ValueError(f"unsupported step: {step}")

    end = pdf.index.max()
    cur = pdf.index.min() + pd.Timedelta(days=win_days)
    step_delta = pd.Timedelta(days=step_days)
    while cur <= end:
        sp = cur - pd.Timedelta(days=win_days)
        window = pdf.loc[sp:cur]
        if len(window) >= approx_obs - min_obs_relax:
            yield window
        cur += step_delta
