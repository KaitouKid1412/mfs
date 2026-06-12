"""Max drawdown + time-to-recover over trailing NAV windows (E2).

STRICTLY display-only: these statistics are surfaced as behavioral-risk
context in the stage-2/3 outputs and the investor report. They are never
composite-weighted — no edits to ``rank/score.py`` or the pipeline weight
vectors may reference them.

``drawdown_stats`` is a pure function over a ``(date, nav)`` frame (the
shape of ``mfs.db.queries.nav_series`` and of the orchestrator's aligned
frame). The orchestrator threads the 3y/5y stats into ``computed_metrics``
(``max_dd_3y_pct``, ``max_dd_3y_recovery_days``, ``max_dd_5y_pct``,
``max_dd_5y_recovery_days``) following the ``alpha_confidence`` pattern.

Conventions (pinned by tests/test_drawdown.py):
  * ``max_dd_pct`` is the max peak-to-trough decline as a POSITIVE percent
    (e.g. ``20.0`` for 100 → 80).
  * A series with no decline reports ``max_dd_pct = 0.0`` and
    ``recovery_days = None`` (no drawdown → no recovery concept).
  * ``recovery_days`` = calendar days from the trough until the nav first
    regains the prior peak; ``None`` when not recovered by series end.
  * Ties on the max drawdown resolve to the EARLIEST trough.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import polars as pl

#: A window with less than this many days of NAV span reports None — a
#: sub-1y "3y max drawdown" would be noise dressed up as history.
MIN_WINDOW_SPAN_DAYS = 365

#: Calendar days per year for the trailing-window cut.
_DAYS_PER_YEAR = 365.25


def drawdown_stats(
    nav: pl.DataFrame, window_years: int, as_of: date,
) -> dict | None:
    """Max drawdown + recovery over the trailing ``window_years`` calendar
    window ending at ``min(last nav date, as_of)``.

    Returns ``{"max_dd_pct", "peak_date", "trough_date", "recovery_days"}``
    or ``None`` when the window holds less than ``MIN_WINDOW_SPAN_DAYS`` of
    data. Zero/negative and non-finite navs are dropped first (defense
    against legacy zero-NAV poisoning).
    """
    if nav.is_empty() or "date" not in nav.columns or "nav" not in nav.columns:
        return None
    df = (
        nav.select(["date", "nav"])
        .drop_nulls()
        .filter(pl.col("nav").is_finite() & (pl.col("nav") > 0))
        .sort("date")
    )
    if df.is_empty():
        return None

    end = min(df["date"].max(), as_of)
    start = end - timedelta(days=round(_DAYS_PER_YEAR * window_years))
    df = df.filter((pl.col("date") >= start) & (pl.col("date") <= end))
    if df.is_empty():
        return None
    span_days = (df["date"].max() - df["date"].min()).days
    if span_days < MIN_WINDOW_SPAN_DAYS:
        return None

    dates: list[date] = df["date"].to_list()
    navs: list[float] = df["nav"].to_list()

    # Running peak + max decline in one pass; earliest trough wins ties.
    peak_nav = navs[0]
    peak_idx = 0
    max_dd = 0.0
    best_peak_idx: int | None = None
    best_trough_idx: int | None = None
    for i, v in enumerate(navs):
        if v > peak_nav:
            peak_nav = v
            peak_idx = i
        dd = (peak_nav - v) / peak_nav
        if dd > max_dd:
            max_dd = dd
            best_peak_idx = peak_idx
            best_trough_idx = i

    if best_trough_idx is None or math.isclose(max_dd, 0.0):
        return {
            "max_dd_pct": 0.0,
            "peak_date": None,
            "trough_date": None,
            "recovery_days": None,
        }

    prior_peak_nav = navs[best_peak_idx]
    trough_date = dates[best_trough_idx]
    recovery_days: float | None = None
    for j in range(best_trough_idx + 1, len(navs)):
        if navs[j] >= prior_peak_nav:
            recovery_days = float((dates[j] - trough_date).days)
            break

    return {
        "max_dd_pct": max_dd * 100.0,
        "peak_date": dates[best_peak_idx],
        "trough_date": trough_date,
        "recovery_days": recovery_days,
    }


def drawdown_row(nav: pl.DataFrame, as_of: date) -> dict:
    """The four ``computed_metrics`` drawdown columns for one scheme:
    3y/5y ``drawdown_stats`` flattened to the column names, ``None`` where a
    window has insufficient data."""
    out: dict = {
        "max_dd_3y_pct": None,
        "max_dd_3y_recovery_days": None,
        "max_dd_5y_pct": None,
        "max_dd_5y_recovery_days": None,
    }
    for years in (3, 5):
        stats = drawdown_stats(nav, window_years=years, as_of=as_of)
        if stats is not None:
            out[f"max_dd_{years}y_pct"] = stats["max_dd_pct"]
            out[f"max_dd_{years}y_recovery_days"] = stats["recovery_days"]
    return out
