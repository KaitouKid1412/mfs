"""Tests for the bhavcopy NSE EQUITY_L (symbol→ISIN) cache refresh.

A stale symbol→ISIN map silently drops rows for symbols renamed by M&A /
corporate actions, so the cache must self-heal on a weekly cadence. These tests
pin that behaviour: fresh cache is reused, stale cache triggers a re-pull, and a
failed re-pull falls back to the stale cache rather than aborting the run.

Also pins B8: an NSE 403 (the archives endpoint's proven failure mode) surfaces
from ingest_recent as IngestError — never a raw httpx.HTTPStatusError — so the
pipeline halts via the clean failure path.
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

from mfs.errors import IngestError
from mfs.ingest import bhavcopy
from mfs.io.http import TransientHttpError

_CSV_A = b"SYMBOL,NAME OF COMPANY,SERIES,ISIN NUMBER\nAAA,A Co,EQ,INE000A01001\n"
_CSV_B = (
    b"SYMBOL,NAME OF COMPANY,SERIES,ISIN NUMBER\n"
    b"AAA,A Co,EQ,INE000A01001\nBBB,B Co,EQ,INE000B01002\n"
)


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    p = tmp_path / "equity_list.csv"
    monkeypatch.setattr(bhavcopy.paths, "nse_equity_list_raw", lambda: p)
    return p


def _set_age_days(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_fresh_cache_reused_without_fetch(cache_path, monkeypatch):
    cache_path.write_bytes(_CSV_A)
    _set_age_days(cache_path, 1)  # within the 7-day window

    def _boom(url):
        raise AssertionError("should not fetch when cache is fresh")

    monkeypatch.setattr(bhavcopy, "_fetch_with_retry", _boom)
    out = bhavcopy.fetch_equity_list()
    assert out == {"AAA": "INE000A01001"}


def test_stale_cache_triggers_refresh(cache_path, monkeypatch):
    cache_path.write_bytes(_CSV_A)
    _set_age_days(cache_path, 30)  # well past the 7-day window
    monkeypatch.setattr(bhavcopy, "_fetch_with_retry", lambda url: _CSV_B)

    out = bhavcopy.fetch_equity_list()
    assert out == {"AAA": "INE000A01001", "BBB": "INE000B01002"}
    assert cache_path.read_bytes() == _CSV_B  # refreshed on disk


def test_missing_cache_fetches(cache_path, monkeypatch):
    monkeypatch.setattr(bhavcopy, "_fetch_with_retry", lambda url: _CSV_A)
    out = bhavcopy.fetch_equity_list()
    assert out == {"AAA": "INE000A01001"}


def test_refresh_failure_falls_back_to_stale_cache(cache_path, monkeypatch):
    cache_path.write_bytes(_CSV_A)
    _set_age_days(cache_path, 30)

    def _fail(url):
        raise TransientHttpError("NSE down")

    monkeypatch.setattr(bhavcopy, "_fetch_with_retry", _fail)
    out = bhavcopy.fetch_equity_list()  # must not raise
    assert out == {"AAA": "INE000A01001"}


def test_missing_cache_and_failed_fetch_is_fatal(cache_path, monkeypatch):
    monkeypatch.setattr(bhavcopy, "_fetch_with_retry", lambda url: None)  # 404
    with pytest.raises(IngestError, match="EQUITY_L"):
        bhavcopy.fetch_equity_list()


# --- B8: raw 4xx never escapes ingest_recent ---------------------------------

def _raise_403(url):
    """Simulate what _fetch_with_retry's raise_for_status does on an NSE 403."""
    req = httpx.Request("GET", url)
    httpx.Response(403, request=req).raise_for_status()


def test_ingest_recent_403_becomes_ingest_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bhavcopy, "fetch_equity_list", lambda *a, **kw: {"AAA": "INE000A01001"}
    )
    # No cached bhavcopy files → every weekday goes to _fetch_with_retry.
    monkeypatch.setattr(
        bhavcopy.paths, "bhavcopy_raw", lambda d: tmp_path / f"{d.isoformat()}.csv"
    )
    monkeypatch.setattr(bhavcopy, "_fetch_with_retry", _raise_403)

    # full=True skips the stock_adv_daily watermark query (no DB in tests).
    with pytest.raises(IngestError, match="bhavcopy: NSE fetch failed") as excinfo:
        bhavcopy.ingest_recent(n_days=7, full=True)

    # Source context preserved: status code + URL, chained from the raw error.
    assert "403" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, httpx.HTTPStatusError)
