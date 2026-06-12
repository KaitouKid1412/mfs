"""Rolling 3Y / 5Y returns at weekly step.

For each rolling window, compute the annualized calendar-CAGR simple return:
    R = (NAV_end / NAV_start)^(1 / years) - 1
    where years = (d_end - d_start).days / 365.25

Then aggregate per scheme: median and 25th-percentile across windows.

Window-epoch convention (A1-4): only windows whose ENDPOINTS fall in the
trailing ~window_years contribute — the same trailing-endpoint trim as
``mfs.compute.rolling.trim_to_trailing_endpoints``, implemented inline on
the polars frame below.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import cast

import polars as pl


def rolling_distribution(aligned: pl.DataFrame, window_years: int, step: str = "1w") -> dict:
    """Return {"median": float|None, "p25": float|None} of annualized returns across
    rolling windows. Skips windows where NAV at either endpoint is null."""
    if aligned.is_empty():
        return {"median": None, "p25": None}

    df = aligned.select(["date", "nav"]).drop_nulls()
    if df.is_empty():
        return {"median": None, "p25": None}

    # Trailing-endpoint trim (A1-4 convention; see rolling.trim_to_trailing_
    # endpoints): keep 2*window_years + 0.05y so the leading window_years feed
    # the earliest window's lookback and endpoints span only the trailing
    # ~window_years. Split into two int() terms to preserve the historical
    # integer-day arithmetic exactly.
    end = cast(date, df["date"].max())
    start_required = end - timedelta(days=int(365.25 * (window_years + 0.05)))
    df = df.filter(pl.col("date") >= start_required - timedelta(days=int(365.25 * window_years)))
    if df.height < 30:
        return {"median": None, "p25": None}

    if step == "1w":
        step_days = 7
    elif step == "1d":
        step_days = 1
    else:
        raise ValueError(f"unsupported step: {step}")

    # Build window endpoint dates
    cur = cast(date, df["date"].min()) + timedelta(days=int(365.25 * window_years))
    end_date = cast(date, df["date"].max())
    endpoints: list[date] = []
    while cur <= end_date:
        endpoints.append(cur)
        cur += timedelta(days=step_days)
    if not endpoints:
        return {"median": None, "p25": None}

    # Pre-sort and convert to numpy for speed
    by_date: dict[date, float] = {r["date"]: r["nav"] for r in df.iter_rows(named=True)}
    sorted_dates: list[date] = sorted(by_date)

    def _nearest_le(d: date) -> date | None:
        # binary search
        import bisect

        idx = bisect.bisect_right(sorted_dates, d) - 1
        if idx < 0:
            return None
        return sorted_dates[idx]

    rs: list[float] = []
    win_calendar_days = int(365.25 * window_years)
    for ep in endpoints:
        sp = ep - timedelta(days=win_calendar_days)
        d_start = _nearest_le(sp)
        d_end = _nearest_le(ep)
        if d_start is None or d_end is None or d_start == d_end:
            continue
        n_start = by_date.get(d_start)
        n_end = by_date.get(d_end)
        if not n_start or not n_end or n_start <= 0:
            continue
        # Calendar-CAGR annualization: exponent 1/years on actual elapsed days.
        years = (d_end - d_start).days / 365.25
        if years <= 0:
            continue
        r = (n_end / n_start) ** (1.0 / years) - 1.0
        rs.append(r)

    if not rs:
        return {"median": None, "p25": None}
    s = pl.Series(rs)
    # rs is non-empty here, so median/quantile cannot return None.
    return {
        "median": float(cast(float, s.median())),
        "p25": float(cast(float, s.quantile(0.25))),
    }
