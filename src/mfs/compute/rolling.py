"""Shared rolling-window iterator used by every per-fund metric.

Every metric in mfs.compute (alpha, sortino, info_ratio, capture) walks a
daily-indexed pandas DataFrame in rolling N-year windows stepped weekly or
daily. This module centralizes that loop so the metric modules can focus on
the per-window math.

Window-epoch convention (A1-4): every "<W>y" metric is the median over
rolling W-year windows whose ENDPOINTS fall in the trailing ~W years of the
series. ``iter_windows`` enforces this by trimming its input to the trailing
``2*W + slack`` years before walking: the leading W years feed the first
window's lookback, so endpoints (which start at min + W years) span only the
trailing ~W (+ slack) years. ``mfs.compute.returns`` applies the identical
trim inline on its polars frame. Without the trim, a 2013-vintage fund's
"3y" alpha would be the median over every 3y window since inception (~540
windows) rather than the trailing-3y endpoint set (~159), diluting current
behavior with decade-old performance.

A window passes if it contains at least `252 * window_years - min_obs_relax`
observations. The relaxation accommodates holidays / short months.
"""

from __future__ import annotations

from typing import Iterator

import pandas as pd

# Calendar slack (in years) added to the trailing-endpoint trim so the
# earliest retained window isn't clipped by boundary effects (weekends,
# holidays, month-length wobble). Matches the inline slack in returns.py.
TRAILING_SLACK_YEARS = 0.05


def trim_to_trailing_endpoints(
    pdf: pd.DataFrame,
    window_years: int,
    slack_years: float = TRAILING_SLACK_YEARS,
) -> pd.DataFrame:
    """Trim a date-indexed frame so rolling-window ENDPOINTS span only the
    trailing ~``window_years`` (the A1-4 epoch convention; see module
    docstring).

    Keeps rows with ``index >= index.max() - (2*window_years + slack_years)``
    calendar years: the leading ``window_years`` exist solely to feed the
    earliest window's lookback, so after ``iter_windows`` starts at
    ``min + window_years``, endpoints cover only the trailing
    ``window_years + slack_years``. A no-op for frames shorter than
    ``2*window_years + slack_years``.
    """
    if len(pdf) == 0:
        return pdf
    keep = pd.Timedelta(days=int(365.25 * (2 * window_years + slack_years)))
    return pdf.loc[pdf.index >= pdf.index.max() - keep]


def iter_windows(
    pdf: pd.DataFrame,
    window_years: int,
    step: str = "1w",
    min_obs_relax: int = 30,
) -> Iterator[pd.DataFrame]:
    """Yield rolling sub-frames of `pdf` for the given window length.

    The input is first trimmed per the trailing-endpoint convention
    (``trim_to_trailing_endpoints``, A1-4): only windows whose endpoints fall
    in the trailing ~``window_years`` are walked. Yields nothing if the
    trimmed frame has insufficient total observations. Individual short
    windows are silently skipped.
    """
    pdf = trim_to_trailing_endpoints(pdf, window_years)
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
