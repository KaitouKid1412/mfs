"""C7: invariant compute series are queried ONCE per run, not once per scheme.

The master calendar, each benchmark's close series, and the risk-free series
are identical for every scheme within a compute run, but were re-queried per
scheme (~2,000 redundant Postgres queries per Phase-1 run). ``alignment``
now lru-caches them; ``clear_caches()`` (wired into the orchestrator entry
points) forces a re-fetch so post-ingest runs never see pre-ingest frames.

No DB: the underlying ``mfs.db.queries`` functions are replaced with counting
stubs returning small fixed frames (query-count assertion via fake boundary).
"""

from __future__ import annotations

from datetime import date

import polars as pl

from mfs.compute import alignment


class _Counting:
    """Wrap a frame-returning callable, counting invocations."""

    def __init__(self, fn):
        self._fn = fn
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self._fn(*args, **kwargs)


def _frames():
    dates = [date(2026, 1, d) for d in (1, 2, 5, 6, 7, 8)]
    cal = pl.DataFrame({"date": dates})
    bench = pl.DataFrame(
        {"date": dates, "close": [100.0, 101.0, 102.0, 101.5, 103.0, 104.0]}
    )
    rf = pl.DataFrame({"date": dates, "rate_daily": [0.00025] * len(dates)})
    nav = pl.DataFrame(
        {"date": dates, "nav": [10.0, 10.1, 10.2, 10.15, 10.3, 10.4]}
    )
    return cal, bench, rf, nav


def _patch(monkeypatch):
    cal, bench, rf, nav = _frames()
    counters = {
        "cal": _Counting(lambda: cal),
        "bench": _Counting(lambda ticker: bench),
        "rf": _Counting(lambda: rf),
    }
    monkeypatch.setattr(alignment.q, "master_calendar", counters["cal"])
    monkeypatch.setattr(alignment.q, "benchmark_series", counters["bench"])
    monkeypatch.setattr(alignment.q, "risk_free_series", counters["rf"])
    monkeypatch.setattr(alignment.q, "nav_series", lambda scheme_code: nav)
    return counters


def test_two_schemes_sharing_a_ticker_query_each_series_once(monkeypatch):
    counters = _patch(monkeypatch)
    a = alignment.align_scheme("S1", "NIFTY 50 TRI")
    b = alignment.align_scheme("S2", "NIFTY 50 TRI")
    assert not a.is_empty() and not b.is_empty()
    assert counters["cal"].calls == 1
    assert counters["bench"].calls == 1
    assert counters["rf"].calls == 1


def test_distinct_tickers_are_cached_per_ticker(monkeypatch):
    counters = _patch(monkeypatch)
    alignment.align_scheme("S1", "NIFTY 50 TRI")
    alignment.align_scheme("S2", "NIFTY MIDCAP 150 TRI")
    alignment.align_scheme("S3", "NIFTY 50 TRI")  # cache hit
    assert counters["bench"].calls == 2
    assert counters["cal"].calls == 1
    assert counters["rf"].calls == 1


def test_clear_caches_forces_refetch(monkeypatch):
    counters = _patch(monkeypatch)
    alignment.align_scheme("S1", "NIFTY 50 TRI")
    alignment.clear_caches()
    alignment.align_scheme("S1", "NIFTY 50 TRI")
    assert counters["cal"].calls == 2
    assert counters["bench"].calls == 2
    assert counters["rf"].calls == 2


def test_cached_results_identical_and_frames_not_mutated(monkeypatch):
    """align_scheme over a cached series must equal the first (uncached) call
    — guards against accidental mutation of the shared cached frames."""
    _patch(monkeypatch)
    first = alignment.align_scheme("S1", "NIFTY 50 TRI")    # populates cache
    second = alignment.align_scheme("S1", "NIFTY 50 TRI")   # served from cache
    assert first.equals(second)
    # The cached calendar frame itself is untouched by the per-scheme filter.
    cal_after = alignment.master_calendar()
    assert cal_after.height == 6
