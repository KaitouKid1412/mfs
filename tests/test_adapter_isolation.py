"""Tests for per-AMC fault isolation in the factsheet/holdings run_all loops.

A single advisory-source AMC failing (e.g. a 404 raising httpx.HTTPStatusError,
which is neither TransientHttpError nor PipelineError) must NOT crash the whole
pipeline — it is recorded as that AMC's gap and the run continues. Only a
SYSTEMIC failure (every AMC failing) raises IngestError.
"""

from __future__ import annotations

import httpx
import pytest

from mfs.errors import IngestError
from mfs.ingest.holdings import _run as holdings_run
from mfs.ingest.managers import _run as managers_run


def _http_404():
    return httpx.HTTPStatusError(
        "404 Not Found",
        request=httpx.Request("GET", "http://example.test/404"),
        response=httpx.Response(404),
    )


def test_managers_run_all_isolates_one_amc(monkeypatch):
    monkeypatch.setattr(managers_run, "registered_adapters", lambda: ["a", "b", "c"])

    def fake_run(slug, ym=None, match_threshold=0, force=False):
        if slug == "b":
            raise _http_404()  # the exact uncaught-4xx crash class
        return {"amc_slug": slug, "ym": ym,
                "rows_written_holdings": 5, "rows_written_ptr": 1}

    monkeypatch.setattr(managers_run, "run_for_amc", fake_run)

    res = managers_run.run_all()
    assert set(res) == {"a", "b", "c"}
    assert res["b"]["error_type"] == "HTTPStatusError"
    assert res["b"]["rows_written_holdings"] == 0
    assert res["a"]["rows_written_holdings"] == 5
    assert res["c"]["rows_written_ptr"] == 1


def test_managers_run_all_raises_when_all_fail(monkeypatch):
    monkeypatch.setattr(managers_run, "registered_adapters", lambda: ["a", "b"])

    def fake_run(slug, ym=None, match_threshold=0, force=False):
        raise ValueError("boom")

    monkeypatch.setattr(managers_run, "run_for_amc", fake_run)
    with pytest.raises(IngestError):
        managers_run.run_all()


def test_holdings_run_all_isolates_one_amc(monkeypatch):
    monkeypatch.setattr(holdings_run, "registered_adapters", lambda: ["x", "y"])

    def fake_run(slug, ym=None):
        if slug == "y":
            raise _http_404()
        return {"amc_slug": slug, "ym": ym, "rows_written": 10}

    monkeypatch.setattr(holdings_run, "run_for_amc", fake_run)

    res = holdings_run.run_all()
    assert res["x"]["rows_written"] == 10
    assert res["y"]["error_type"] == "HTTPStatusError"
    assert res["y"]["rows_written"] == 0


def test_holdings_run_all_raises_when_all_fail(monkeypatch):
    monkeypatch.setattr(holdings_run, "registered_adapters", lambda: ["x"])

    def fake_run(slug, ym=None):
        raise ValueError("boom")

    monkeypatch.setattr(holdings_run, "run_for_amc", fake_run)
    with pytest.raises(IngestError):
        holdings_run.run_all()
