"""Tests for RBI prid discovery (discover_latest_prid).

Regression coverage for the stale-RSS undershoot bug: the RSS feed served a max
prid *below* prids we had already cached, which capped the walk and stranded
newer weekly auctions. Discovery must anchor on max(RSS, state, cache HWM) and
probe forward to the true tip, while never treating a transient error as the
end of the feed.
"""

from __future__ import annotations

import contextlib

from mfs.ingest import fbil_tbill as fbil


@contextlib.contextmanager
def _fake_client():
    yield object()


def _make_fake_fetch(real_max: int, transient_at: int | None = None):
    """Fake _fetch_prid_html: real page for prid<=real_max, absent above,
    optional transient failure at `transient_at`."""
    def _fake(client, prid):
        if transient_at is not None and prid == transient_at:
            return None, "rate limited"          # transient failure
        if prid <= real_max:
            return f"<html>prid {prid}</html>", None  # real page
        return None, None                        # permanent absence (404/empty)
    return _fake


def _patch(monkeypatch, *, rss, state, cache_hwm, real_max, transient_at=None):
    monkeypatch.setattr(fbil, "fetch_latest_prid", lambda *a, **k: rss)
    monkeypatch.setattr(fbil, "_read_state", lambda: state)
    monkeypatch.setattr(fbil, "_cache_high_water_mark", lambda: cache_hwm)
    monkeypatch.setattr(fbil, "_rbi_client", _fake_client)
    monkeypatch.setattr(fbil.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(fbil, "_fetch_prid_html", _make_fake_fetch(real_max, transient_at))


def test_reaches_true_tip_despite_stale_rss(monkeypatch):
    # The bug: RSS (62669) is below our own cache HWM (62754); real auctions
    # exist up to 62870. Discovery must anchor on the cache HWM and probe to 62870.
    _patch(monkeypatch, rss=62669, state={"latest_prid": 62669},
           cache_hwm=62754, real_max=62870)
    assert fbil.discover_latest_prid() == 62870


def test_probe_stops_after_consecutive_absent(monkeypatch):
    # No real prids above the anchor — probe must stop (no runaway) and return
    # the anchor as the tip.
    _patch(monkeypatch, rss=1000, state={}, cache_hwm=1000, real_max=1000)
    assert fbil.discover_latest_prid() == 1000


def test_transient_mid_probe_not_treated_as_tip(monkeypatch):
    # Real prids exist well past the anchor, but a transient error hits at 1003.
    # Discovery must STOP (not count the transient toward the absent-gap, which
    # would falsely look like end-of-feed) and return the highest real seen.
    _patch(monkeypatch, rss=1000, state={"latest_prid": 1000},
           cache_hwm=1000, real_max=2000, transient_at=1003)
    # probes 1001 (real), 1002 (real), 1003 (transient -> break)
    assert fbil.discover_latest_prid() == 1002


def test_cold_start_nothing_known_returns_none(monkeypatch):
    _patch(monkeypatch, rss=None, state={}, cache_hwm=0, real_max=0)
    assert fbil.discover_latest_prid() is None
