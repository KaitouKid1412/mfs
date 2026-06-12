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

    def fake_run(slug, ym=None, force=False):
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

    def fake_run(slug, ym=None, force=False):
        raise ValueError("boom")

    monkeypatch.setattr(holdings_run, "run_for_amc", fake_run)
    with pytest.raises(IngestError):
        holdings_run.run_all()


# ---------------------------------------------------------------------------
# F-5: the extracted shared core (mfs.ingest._common.run_all_isolated) drives
# both families — pin the isolation semantics directly on it, parametrized
# over both families' configurations.
# ---------------------------------------------------------------------------

from mfs.ingest import _common  # noqa: E402

_FAMILIES = [
    pytest.param(
        dict(family="managers",
             error_zero_fields=("rows_written_holdings", "rows_written_ptr"),
             adapter_kind="factsheet"),
        id="managers",
    ),
    pytest.param(
        dict(family="holdings", error_zero_fields=("rows_written",)),
        id="holdings",
    ),
]


@pytest.mark.parametrize("kw", _FAMILIES)
def test_run_all_isolated_one_fails_continues(kw):
    def run_one(slug):
        if slug == "b":
            raise _http_404()
        return {"amc_slug": slug, "ok": True}

    res = _common.run_all_isolated(
        kw["family"], ["a", "b", "c"], run_one,
        ym="2026-04",
        error_zero_fields=kw["error_zero_fields"],
        adapter_kind=kw.get("adapter_kind"),
    )
    assert set(res) == {"a", "b", "c"}
    assert res["a"]["ok"] and res["c"]["ok"]
    assert res["b"]["error_type"] == "HTTPStatusError"
    assert res["b"]["ym"] == "2026-04"
    for f in kw["error_zero_fields"]:
        assert res["b"][f] == 0


@pytest.mark.parametrize("kw", _FAMILIES)
def test_run_all_isolated_all_fail_raises(kw):
    def run_one(slug):
        raise ValueError(f"boom-{slug}")

    with pytest.raises(IngestError, match="systemic") as ei:
        _common.run_all_isolated(
            kw["family"], ["a", "b"], run_one,
            error_zero_fields=kw["error_zero_fields"],
            adapter_kind=kw.get("adapter_kind"),
        )
    # deterministic 'first error' = first slug's error; kind label included
    assert "boom-a" in str(ei.value)
    assert (kw.get("adapter_kind") or kw["family"]) in str(ei.value)


# ---------------------------------------------------------------------------
# C2: run_all parallelizes over AMCs (ThreadPool in run_all_isolated) while
# preserving the isolation semantics pinned above.
# ---------------------------------------------------------------------------

import threading  # noqa: E402


def test_managers_run_all_is_concurrent(monkeypatch):
    """Passes only if ≥3 AMCs are in flight simultaneously: each fake blocks
    on a 3-party barrier, which a serial loop can never satisfy."""
    barrier = threading.Barrier(3, timeout=5)
    monkeypatch.setattr(managers_run, "registered_adapters", lambda: ["a", "b", "c"])

    def fake_run(slug, ym=None, match_threshold=0, force=False):
        barrier.wait()
        return {"amc_slug": slug, "rows_written_holdings": 1, "rows_written_ptr": 1}

    monkeypatch.setattr(managers_run, "run_for_amc", fake_run)
    res = managers_run.run_all()
    assert set(res) == {"a", "b", "c"}
    assert all(r["rows_written_ptr"] == 1 for r in res.values())


def test_holdings_run_all_is_concurrent(monkeypatch):
    barrier = threading.Barrier(3, timeout=5)
    monkeypatch.setattr(holdings_run, "registered_adapters", lambda: ["x", "y", "z"])

    def fake_run(slug, ym=None, force=False):
        barrier.wait()
        return {"amc_slug": slug, "rows_written": 2}

    monkeypatch.setattr(holdings_run, "run_for_amc", fake_run)
    res = holdings_run.run_all()
    assert set(res) == {"x", "y", "z"}
    assert all(r["rows_written"] == 2 for r in res.values())


def test_workers_1_restores_serial_with_identical_results(monkeypatch):
    """MFS_INGEST_WORKERS=1 (monkeypatched settings) must produce the exact
    same results dict — including error rows — as the pooled run."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        managers_run, "registered_adapters", lambda: ["a", "b", "c"]
    )

    def fake_run(slug, ym=None, match_threshold=0, force=False):
        if slug == "b":
            raise ValueError("boom-b")
        return {"amc_slug": slug, "ym": ym,
                "rows_written_holdings": 3, "rows_written_ptr": 1}

    monkeypatch.setattr(managers_run, "run_for_amc", fake_run)

    pooled = managers_run.run_all(ym="2026-04")

    in_flight = {"n": 0, "max": 0}
    lock = threading.Lock()

    def tracking_run(slug, ym=None, match_threshold=0, force=False):
        with lock:
            in_flight["n"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["n"])
        try:
            return fake_run(slug, ym=ym, match_threshold=match_threshold, force=force)
        finally:
            with lock:
                in_flight["n"] -= 1

    monkeypatch.setattr(managers_run, "run_for_amc", tracking_run)
    monkeypatch.setattr(
        _common, "get_settings", lambda: SimpleNamespace(ingest_workers=1)
    )
    serial = managers_run.run_all(ym="2026-04")
    assert serial == pooled
    assert in_flight["max"] == 1  # truly serial


# --- _default_data_month (single shared implementation, F-5) ----------------

from datetime import date  # noqa: E402


def test_default_data_month_january_rolls_to_prior_december():
    assert _common._default_data_month(date(2026, 1, 15)) == "2025-12"


def test_default_data_month_mid_year():
    assert _common._default_data_month(date(2026, 6, 10)) == "2026-05"


def test_default_data_month_is_the_single_shared_copy():
    """Both orchestrators must use the one _common implementation."""
    assert managers_run._default_data_month is _common._default_data_month
    assert holdings_run._default_data_month is _common._default_data_month
