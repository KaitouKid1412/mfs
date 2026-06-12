"""B8: required-stage ingesters translate raw httpx 4xx into IngestError.

cli/pipeline's ``_stage`` only renders (PipelineError, IngestError) cleanly;
anything else used to escape as a raw traceback. These tests pin the
per-ingester translation at the top-level entry points: amfi_aum.ingest_quarter,
benchmarks.fetch_tri_window (whose IngestError feeds ingest_all_known's
per-ticker aggregation), and amfi_nav.fetch_today (the network boundary shared
by ingest_today and scheme_master.build). Each must raise IngestError carrying
source context (URL / source, status code) — never httpx.HTTPStatusError.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from mfs.errors import IngestError
from mfs.ingest import amfi_aum, amfi_nav, benchmarks


def _status_error(url: str, code: int = 403) -> httpx.HTTPStatusError:
    """Build the exact error httpx's raise_for_status raises for `code`."""
    req = httpx.Request("GET", url)
    resp = httpx.Response(code, request=req)
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        resp.raise_for_status()
    return excinfo.value


# --- amfi_aum.ingest_quarter --------------------------------------------------

def test_amfi_aum_4xx_becomes_ingest_error(monkeypatch):
    def _fail(url, params=None):
        raise _status_error(url)

    monkeypatch.setattr(amfi_aum.http, "fetch_bytes", _fail)

    with pytest.raises(IngestError, match="AMFI AAUM fetch failed for Q4-2026") as excinfo:
        amfi_aum.ingest_quarter("Q4-2026")

    # Source context: endpoint URL + status code, chained from the raw error.
    assert amfi_aum.SCHEMEWISE_URL in str(excinfo.value)
    assert "403" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, httpx.HTTPStatusError)


# --- benchmarks.fetch_tri_window ------------------------------------------------

class _Client403:
    """Stand-in for httpx.Client whose POST returns a 403."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, content=None):
        return httpx.Response(403, request=httpx.Request("POST", url))


def test_benchmarks_tri_4xx_becomes_ingest_error(monkeypatch):
    monkeypatch.setattr(benchmarks.httpx, "Client", _Client403)

    with pytest.raises(IngestError, match="NSE Indices 403") as excinfo:
        benchmarks.fetch_tri_window(
            "NIFTY 50", "Nifty 50", date(2026, 1, 1), date(2026, 3, 1)
        )

    assert benchmarks.NIFTY_TRI_URL in str(excinfo.value)
    assert "Nifty 50" in str(excinfo.value)


# --- amfi_nav.fetch_today (shared by ingest_today + scheme_master.build) -------

def test_amfi_nav_fetch_today_4xx_becomes_ingest_error(monkeypatch):
    def _fail(url, params=None):
        raise _status_error(url)

    monkeypatch.setattr(amfi_nav.http, "fetch_bytes", _fail)

    with pytest.raises(IngestError, match="AMFI NAVAll fetch failed") as excinfo:
        amfi_nav.fetch_today()

    assert amfi_nav.NAV_TODAY_URL in str(excinfo.value)
    assert "403" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, httpx.HTTPStatusError)
