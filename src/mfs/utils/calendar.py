from __future__ import annotations

from datetime import date, timedelta


def iter_windows(start: date, end: date, window_days: int):
    """Yield (from_date, to_date) tuples covering [start, end] in window_days chunks."""
    cur = start
    while cur <= end:
        nxt = min(end, cur + timedelta(days=window_days - 1))
        yield cur, nxt
        cur = nxt + timedelta(days=1)
