"""PTR month-over-month unit-flip tripwire (B14b).

A percent-vs-fraction convention flip (e.g. 9.14 leaking through where the
prior month stored 0.09) writes ~100x-wrong PTR that the per-record pydantic
bounds (B14a) cannot catch. managers/_run.py compares the incoming batch's
median PTR against the AMC's previous stored month (read-only query) and
refuses the AMC with IngestError when the median shifts BOTH >5x AND >0.5
absolute. No prior month → degrade gracefully (nothing to compare).

No network, no live DB: stub adapter (pdf_path override) + a fake connection
that scripts the prior-month median and records every executed statement.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import polars as pl
import pytest

import mfs.db.connection as dbconn
from mfs.errors import IngestError
from mfs.ingest.managers import _run as managers_run
from mfs.schemas import ParsedPtrRecord

_FIXTURE_PDF = (
    Path(__file__).parent / "fixtures" / "factsheets" / "hdfc"
    / "2026-04_extract.pdf"
)

_WORDS = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]


class _FakeConn:
    """Scripts the B14b median query + B13 distinct-count query."""

    def __init__(self, prior_median: float | None, existing_count: int = 0):
        self.prior_median = prior_median
        self.existing_count = existing_count
        self.executed: list[str] = []

    def execute(self, sql, params=None):
        self.executed.append(sql.strip())

        class _Cur:
            def __init__(self, row):
                self._row = row

            def fetchone(self):
                return self._row

        flat = " ".join(sql.split()).lower()
        if "percentile_cont" in flat:
            return _Cur((self.prior_median,))
        if flat.startswith("select count(distinct"):
            return _Cur((self.existing_count,))
        return _Cur(None)

    def deletes(self) -> list[str]:
        return [s for s in self.executed if s.upper().startswith("DELETE")]


def _fake_connect(conn):
    @contextmanager
    def _cm(autocommit=False):
        yield conn

    return _cm


class _StubAdapter:
    amc_slug = "flipamc"

    def __init__(self, ptr_values: list[float]):
        self._ptr_values = ptr_values

    def parse_holdings(self, pdf, ym):
        return []

    def parse_ptr(self, pdf, ym):
        return [
            ParsedPtrRecord(
                scheme_name_printed=f"Flip {w} Fund", ptr=v,
                source_amc=self.amc_slug,
            )
            for w, v in zip(_WORDS, self._ptr_values)
        ]


def _sm() -> pl.DataFrame:
    n = len(_WORDS)
    return pl.DataFrame({
        "scheme_code": [f"F{i:02d}" for i in range(n)],
        "scheme_name": [f"Flip {w} Fund" for w in _WORDS],
        "amc_code": ["flipamc"] * n,
        "plan_type": ["DIRECT"] * n,
        "option_type": ["GROWTH"] * n,
        "is_active": [True] * n,
        "canonical_category": ["Flexi Cap"] * n,
    })


def _setup(monkeypatch, ptr_values: list[float], prior_median: float | None):
    stub = _StubAdapter(ptr_values)
    conn = _FakeConn(prior_median)
    written: list[pl.DataFrame] = []
    monkeypatch.setattr(managers_run, "get_adapter", lambda slug: stub)
    monkeypatch.setattr(managers_run.q, "scheme_master", lambda **kw: _sm())
    monkeypatch.setattr(
        managers_run.w, "upsert_portfolio_turnover",
        lambda df, conn=None: written.append(df) or len(df),
    )
    monkeypatch.setattr(dbconn, "connect", _fake_connect(conn))
    return conn, written


def _run():
    return managers_run.run_for_amc(
        "flipamc", ym="2026-04", pdf_path=_FIXTURE_PDF,
    )


def test_percent_leak_vs_fraction_prior_refused(monkeypatch):
    """ptr=9.14 (percent leaking through) vs prior median 0.09 → ~100x AND
    9.05 absolute → refused before any DELETE; nothing written."""
    conn, written = _setup(monkeypatch, [9.14] * 5, prior_median=0.09)
    with pytest.raises(IngestError, match="unit-flip"):
        _run()
    assert conn.deletes() == []
    assert written == []


def test_fraction_vs_percent_prior_refused_both_directions(monkeypatch):
    """The flip is caught in BOTH directions (new convention 100x smaller)."""
    conn, written = _setup(monkeypatch, [0.09] * 5, prior_median=9.14)
    with pytest.raises(IngestError, match="unit-flip"):
        _run()
    assert written == []


def test_normal_drift_passes(monkeypatch):
    conn, written = _setup(monkeypatch, [0.55] * 5, prior_median=0.45)
    res = _run()
    assert res["rows_written_ptr"] == 5
    assert len(conn.deletes()) == 1
    assert len(written) == 1


def test_no_prior_month_degrades_gracefully(monkeypatch):
    """First-ever month for the AMC (median query returns NULL) → proceed."""
    conn, written = _setup(monkeypatch, [9.14] * 5, prior_median=None)
    res = _run()
    assert res["rows_written_ptr"] == 5
    assert len(written) == 1


def test_large_ratio_tiny_absolute_passes(monkeypatch):
    """6x on a tiny base (0.05 → 0.30) is ratio noise, not a unit flip —
    the >0.5 absolute leg keeps it writable."""
    _, written = _setup(monkeypatch, [0.30] * 5, prior_median=0.05)
    res = _run()
    assert res["rows_written_ptr"] == 5
    assert len(written) == 1


def test_large_absolute_small_ratio_passes(monkeypatch):
    """1.0 → 2.0 is genuine turnover drift (2x), not a convention flip."""
    _, written = _setup(monkeypatch, [2.0] * 5, prior_median=1.0)
    res = _run()
    assert res["rows_written_ptr"] == 5
    assert len(written) == 1
