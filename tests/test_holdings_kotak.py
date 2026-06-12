"""B11: Kotak absent-month probe + TTL'd negative cache; B1: download magic.

All HTTP is mocked at the module boundary (kotak's ``httpx`` reference is
replaced with a stub namespace) and ``time.sleep`` is recorded instead of
slept, so an "absent month" run is simulated wall-clock, not real.
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import httpx
import pytest

from mfs.errors import IngestError
from mfs.ingest.holdings import kotak

YM = "2026-05"
_FUNDLIST = {
    "status": "Success",
    "fundList": [
        {"FundName": "Kotak Flexicap Fund", "FundCode": "SEF"},
        {"FundName": "Kotak Bluechip Fund", "FundCode": "KBC"},
    ],
}
HTML_SHELL = b'<!doctype html><html><body><div id="root"></div></body></html>'
XLSX_BYTES = b"PK\x03\x04" + b"\x00" * 64


class FakeResponse:
    def __init__(self, status_code=200, body=None, content=b""):
        self.status_code = status_code
        if body is not None:
            self.text = json.dumps(body)
            self.content = self.text.encode()
        else:
            self.content = content
            self.text = content.decode("utf-8", errors="replace")


class FakeCookies:
    def clear(self):
        pass


class FakeClient:
    """Stands in for httpx.Client; routes every GET through ``handler``."""

    def __init__(self, handler, calls):
        self._handler = handler
        self.calls = calls
        self.cookies = FakeCookies()

    def get(self, url):
        self.calls.append(url)
        return self._handler(url)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Sandbox the data dir, stub kotak's httpx + time, expose call/sleep logs."""
    import mfs.paths as paths

    monkeypatch.setattr(paths, "raw_dir", lambda: tmp_path / "raw")

    state = SimpleNamespace(calls=[], sleeps=[], handler=None)

    def client_factory(*args, **kwargs):
        assert state.handler is not None, "test must set env.handler"
        return FakeClient(state.handler, state.calls)

    monkeypatch.setattr(
        kotak, "httpx",
        SimpleNamespace(
            Client=client_factory,
            HTTPError=httpx.HTTPError,
            Timeout=httpx.Timeout,
        ),
    )
    monkeypatch.setattr(
        kotak, "time", SimpleNamespace(sleep=lambda s: state.sleeps.append(s)),
    )
    state.marker = kotak._absent_marker_path(YM)
    return state


def _folderlist_calls(calls):
    return [c for c in calls if "folderlist" in c]


def _handler(folderlist_response):
    def handle(url):
        if "schemesearch" in url:
            return FakeResponse(200, body=_FUNDLIST)
        if "folderlist" in url:
            return folderlist_response(url)
        raise AssertionError(f"unexpected URL {url}")
    return handle


# ---------------------------------------------------------------------------
# (a) absent month: deterministic folderlist failure -> marker + IngestError
# ---------------------------------------------------------------------------

def test_absent_month_probes_two_codes_writes_marker(env):
    env.handler = _handler(
        lambda url: FakeResponse(200, body={"status": "Failure", "statusCode": "400"})
    )
    adapter = kotak.KotakHoldingsAdapter()
    with pytest.raises(IngestError, match=f"month {YM} not yet published"):
        adapter.discover_scheme_urls(YM)

    fl = _folderlist_calls(env.calls)
    # Exactly two probe budgets, never the per-scheme 20x budget.
    assert len(fl) == 2 * kotak._PROBE_RETRIES
    assert len(fl) <= 10
    # Both probed codes are distinct catalog codes.
    assert {("SEF" in c, "KBC" in c) for c in fl} == {(True, False), (False, True)}
    # Marker written with a parseable ISO timestamp.
    assert env.marker.exists()
    dt.datetime.fromisoformat(env.marker.read_text().strip())
    # Simulated wall-clock: probe sleeps only — far under the <60s budget
    # (vs ~9,400s measured for the 2026-06-08 unpublished-month run).
    assert sum(env.sleeps) < 60


# ---------------------------------------------------------------------------
# (b) fresh marker: raise immediately with ZERO HTTP calls
# ---------------------------------------------------------------------------

def test_fresh_marker_short_circuits_with_zero_http(env):
    env.marker.parent.mkdir(parents=True, exist_ok=True)
    env.marker.write_text(dt.datetime.now(dt.timezone.utc).isoformat())

    def explode(url):  # any HTTP call is a failure of the negative cache
        raise AssertionError(f"HTTP call issued despite fresh marker: {url}")

    env.handler = explode
    with pytest.raises(IngestError, match=f"month {YM} not yet published"):
        kotak.KotakHoldingsAdapter().discover_scheme_urls(YM)
    assert env.calls == []
    assert env.marker.exists()  # still in force for the rest of its TTL


# ---------------------------------------------------------------------------
# (c) expired marker + month now present: re-probe passes, marker removed
# ---------------------------------------------------------------------------

def test_expired_marker_reprobes_and_clears(env):
    env.marker.parent.mkdir(parents=True, exist_ok=True)
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)
    env.marker.write_text(stale.isoformat())

    env.handler = _handler(
        lambda url: FakeResponse(200, body={"status": "Success", "dataList": ["SEF.xlsx"]})
    )
    urls = kotak.KotakHoldingsAdapter().discover_scheme_urls(YM)
    assert not env.marker.exists()
    assert set(urls) == {"Kotak Flexicap Fund", "Kotak Bluechip Fund"}
    # Month confirmed on the FIRST probe leaf — no extra probe traffic.
    assert len(_folderlist_calls(env.calls)) == 1


# ---------------------------------------------------------------------------
# (d) month present, no marker: behavior unchanged vs today
# ---------------------------------------------------------------------------

def test_month_present_discovery_unchanged(env):
    env.handler = _handler(
        lambda url: FakeResponse(200, body={"status": "Success", "dataList": ["SEF.xlsx"]})
    )
    urls = kotak.KotakHoldingsAdapter().discover_scheme_urls(YM)
    assert urls["Kotak Flexicap Fund"] == (
        f"{kotak._API}/downloadfile?path=SEF/2026/May/Consolidated"
        "&filename=SEF.xlsx"
    )
    assert urls["Kotak Bluechip Fund"] == (
        f"{kotak._API}/downloadfile?path=KBC/2026/May/Consolidated"
        "&filename=KBC.xlsx"
    )
    assert not env.marker.exists()
    assert env.sleeps == []  # one schemesearch + one successful probe leaf


# ---------------------------------------------------------------------------
# B1(d): fetch_excel with an HTML payload retries and never caches
# ---------------------------------------------------------------------------

def test_fetch_excel_html_payload_retries_never_cached(env, monkeypatch):
    monkeypatch.setattr(kotak, "_MAX_RETRIES", 3)
    downloads = []

    def handle(url):
        if "folderlist" in url:
            return FakeResponse(200, body={"status": "Success", "dataList": ["SEF.xlsx"]})
        if "downloadfile" in url:
            downloads.append(url)
            return FakeResponse(200, content=HTML_SHELL)  # WAF/challenge page as 200
        raise AssertionError(url)

    env.handler = handle
    adapter = kotak.KotakHoldingsAdapter()
    url = adapter._download_url("SEF", "2026", "May")
    with pytest.raises(RuntimeError, match="leaf-prime\\+download failed"):
        adapter.fetch_excel(url, "SEF.xlsx", YM)
    assert len(downloads) == 3  # every attempt retried, none accepted
    out = kotak.paths.holdings_excel_raw("kotak", YM, "SEF.xlsx")
    assert not out.exists()
    assert not out.with_suffix(out.suffix + ".tmp").exists()


def test_fetch_excel_real_xlsx_is_cached(env):
    def handle(url):
        if "folderlist" in url:
            return FakeResponse(200, body={"status": "Success", "dataList": ["SEF.xlsx"]})
        if "downloadfile" in url:
            return FakeResponse(200, content=XLSX_BYTES)
        raise AssertionError(url)

    env.handler = handle
    adapter = kotak.KotakHoldingsAdapter()
    url = adapter._download_url("SEF", "2026", "May")
    out = adapter.fetch_excel(url, "SEF.xlsx", YM)
    assert out.read_bytes() == XLSX_BYTES
