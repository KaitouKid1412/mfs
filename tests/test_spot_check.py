"""F-11: unit tests for the cross_validate point-to-point CAGR helper.

The tool itself is a read-only DB script; only the pure math is unit-tested
(synthetic NAV series compounding exactly 10%/yr must yield 0.10 ±1e-6).
"""

from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

_TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "cross_validate.py"
_spec = importlib.util.spec_from_file_location("cross_validate", _TOOL_PATH)
cv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cv)


def _series_10pct(days: int, end: date = date(2026, 6, 12)):
    """Daily NAV series compounding exactly 10%/yr, ending at ``end``."""
    start = end - timedelta(days=days)
    return [
        (start + timedelta(days=i), 100.0 * (1.10 ** (i / cv.DAYS_PER_YEAR)))
        for i in range(days + 1)
    ]


def test_cagr_exact_10pct_3y():
    series = _series_10pct(days=4 * 366)
    p = cv.point_to_point_cagr(series, 3.0)
    assert p is not None
    assert abs(p.cagr - 0.10) < 1e-6


def test_cagr_exact_10pct_5y():
    series = _series_10pct(days=6 * 366)
    p = cv.point_to_point_cagr(series, 5.0)
    assert p is not None
    assert abs(p.cagr - 0.10) < 1e-6
    # the start anchor really is ~5y back
    assert abs(p.years_actual - 5.0) < 0.1


def test_cagr_none_when_history_too_short():
    series = _series_10pct(days=400)  # ~1.1y of history
    assert cv.point_to_point_cagr(series, 3.0) is None


def test_cagr_none_beyond_tolerance_gap():
    # 3y of history but a 60-day hole exactly around the 3y-back target date.
    end = date(2026, 6, 12)
    target = end - timedelta(days=round(3 * cv.DAYS_PER_YEAR))
    series = [
        p for p in _series_10pct(days=4 * 366, end=end)
        if abs((p[0] - target).days) > 30
    ]
    assert cv.point_to_point_cagr(series, 3.0) is None


def test_cagr_uses_actual_span_not_nominal():
    # Sparse weekly sampling still recovers the exact rate because the
    # exponent uses the actual day span between the chosen endpoints.
    daily = _series_10pct(days=4 * 366)
    weekly = daily[::7] + [daily[-1]]
    p = cv.point_to_point_cagr(weekly, 3.0)
    assert p is not None
    assert abs(p.cagr - 0.10) < 1e-6


def test_cagr_rejects_nonpositive_navs():
    series = _series_10pct(days=4 * 366)
    series[-1] = (series[-1][0], 0.0)
    assert cv.point_to_point_cagr(series, 3.0) is None
    assert cv.point_to_point_cagr([], 3.0) is None
    assert cv.point_to_point_cagr(series[-1:], 3.0) is None
