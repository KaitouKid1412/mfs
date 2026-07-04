"""Partition-shrinkage guard (B13): the DELETE+reinsert that makes a holdings
(or PTR) ingest authoritative per (source_amc, month) must REFUSE to lose
previously-good schemes. A scheme that parsed last run but failed this run
would otherwise have its prior rows silently deleted. Shrinkage raises
IngestError BEFORE the DELETE (transaction never mutates); ``force`` (--full)
replaces the partition anyway with a loud warning.

No network, no live DB: stub adapters + a fake connection that scripts the
existing distinct-scheme count and records every executed statement.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import openpyxl
import polars as pl
import pytest
from structlog.testing import capture_logs

import mfs.db.connection as dbconn
from mfs.errors import IngestError
from mfs.ingest.holdings import _run as holdings_run
from mfs.ingest.holdings._generic import GenericHoldingsAdapter
from mfs.ingest.managers import _run as managers_run
from mfs.schemas import ParsedPtrRecord

_WORDS = ["Alpha", "Bravo", "Charlie", "Delta", "Echo",
          "Foxtrot", "Golf", "Hotel", "India", "Juliett"]


class _FakeConn:
    """Scripts SELECT COUNT(DISTINCT scheme_code) and records every SQL."""

    def __init__(self, existing_count: int):
        self.existing_count = existing_count
        self.executed: list[str] = []

    def execute(self, sql, params=None):
        self.executed.append(sql.strip())

        class _Cur:
            def __init__(self, row):
                self._row = row

            def fetchone(self):
                return self._row

        if sql.lstrip().upper().startswith("SELECT COUNT(DISTINCT"):
            return _Cur((self.existing_count,))
        return _Cur(None)

    def deletes(self) -> list[str]:
        return [s for s in self.executed if s.upper().startswith("DELETE")]


def _fake_connect(conn):
    @contextmanager
    def _cm(autocommit=False):
        yield conn

    return _cm


# --- holdings path -----------------------------------------------------------


class _StubAdapter(GenericHoldingsAdapter):
    amc_slug = "shrinkamc"
    source_label = "Shrink AMC"

    def __init__(self, cache_dir: Path, scheme_names: list[str]):
        self._cache_dir = cache_dir
        self._names = scheme_names

    def discover_scheme_urls(self, ym):
        return {n: f"http://x.test/{n}.xlsx" for n in self._names}

    def fetch_excel(self, url, scheme_filename, ym):
        out = self._cache_dir / self.amc_slug / ym / scheme_filename
        out.parent.mkdir(parents=True, exist_ok=True)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["ISIN", "Name of the Instrument", "Industry", "% to NAV"])
        for i in range(6):
            ws.append([f"INE{i:03d}B0104{i % 10}", f"Security {i}", "X", 16.5])
        wb.save(out)
        return out


def _sm(names: list[str]) -> pl.DataFrame:
    n = len(names)
    return pl.DataFrame({
        "scheme_code": [f"S{i:02d}" for i in range(n)],
        "scheme_name": names,
        "amc_code": ["shrinkamc"] * n,
        "plan_type": ["DIRECT"] * n,
        "option_type": ["GROWTH"] * n,
        "is_active": [True] * n,
        "canonical_category": ["Flexi Cap"] * n,
    })


def _setup_holdings(monkeypatch, tmp_path, n_incoming: int, existing: int):
    names = [f"Shrink {w} Fund" for w in _WORDS[:n_incoming]]
    stub = _StubAdapter(tmp_path, names)
    conn = _FakeConn(existing)
    written: list[pl.DataFrame] = []
    monkeypatch.setattr(holdings_run, "get_adapter", lambda slug: stub)
    # scheme_master knows all 10 schemes; the adapter only "publishes"
    # n_incoming of them this run.
    monkeypatch.setattr(
        holdings_run.q, "scheme_master",
        lambda **kw: _sm([f"Shrink {w} Fund" for w in _WORDS]),
    )
    monkeypatch.setattr(
        holdings_run.w, "upsert_holdings",
        lambda df, conn=None: written.append(df) or len(df),
    )
    monkeypatch.setattr(dbconn, "connect", _fake_connect(conn))
    return conn, written


@pytest.mark.db
def test_holdings_10_to_7_refused_db_untouched(tmp_path, monkeypatch):
    conn, written = _setup_holdings(monkeypatch, tmp_path, n_incoming=7, existing=10)
    with pytest.raises(IngestError, match="partition shrinkage"):
        holdings_run.run_for_amc("shrinkamc", ym="2026-05")
    assert conn.deletes() == []  # refusal happens BEFORE the DELETE
    assert written == []


@pytest.mark.db
def test_holdings_10_to_10_proceeds(tmp_path, monkeypatch):
    conn, written = _setup_holdings(monkeypatch, tmp_path, n_incoming=10, existing=10)
    res = holdings_run.run_for_amc("shrinkamc", ym="2026-05")
    assert len(conn.deletes()) == 1
    assert res["rows_written"] == len(written[0])


@pytest.mark.db
def test_holdings_7_to_10_growth_proceeds(tmp_path, monkeypatch):
    conn, written = _setup_holdings(monkeypatch, tmp_path, n_incoming=10, existing=7)
    holdings_run.run_for_amc("shrinkamc", ym="2026-05")
    assert len(conn.deletes()) == 1 and len(written) == 1


def test_holdings_force_overrides_with_loud_log(tmp_path, monkeypatch):
    conn, written = _setup_holdings(monkeypatch, tmp_path, n_incoming=7, existing=10)
    with capture_logs() as logs:
        holdings_run.run_for_amc("shrinkamc", ym="2026-05", force=True)
    assert len(conn.deletes()) == 1 and len(written) == 1
    ev = [e for e in logs if e["event"] == "holdings.partition_shrinkage_forced"]
    assert len(ev) == 1 and ev[0]["log_level"] == "warning"
    assert ev[0]["incoming"] == 7 and ev[0]["existing"] == 10


def test_run_all_forwards_force(monkeypatch):
    seen = {}

    def fake_run(slug, ym=None, force=False):
        seen[slug] = force
        return {"amc_slug": slug, "rows_written": 1}

    monkeypatch.setattr(holdings_run, "registered_adapters", lambda: ["a"])
    monkeypatch.setattr(holdings_run, "run_for_amc", fake_run)
    holdings_run.run_all(ym="2026-05", force=True)
    assert seen == {"a": True}


# --- managers PTR path -------------------------------------------------------

_FIXTURE_PDF = (
    Path(__file__).parent / "fixtures" / "factsheets" / "hdfc"
    / "2026-04_extract.pdf"
)


class _StubManagerAdapter:
    amc_slug = "shrinkmgr"

    def fetch(self, ym):  # pragma: no cover — pdf_path override used
        raise AssertionError("fetch must not be called with pdf_path")

    def parse_holdings(self, pdf, ym):
        return []

    def __init__(self, ptr_names: list[str]):
        self._ptr_names = ptr_names

    def parse_ptr(self, pdf, ym):
        return [
            ParsedPtrRecord(scheme_name_printed=n, ptr=0.5, source_amc=self.amc_slug)
            for n in self._ptr_names
        ]


def _setup_managers(monkeypatch, n_incoming: int, existing: int):
    names = [f"Shrink {w} Fund" for w in _WORDS[:n_incoming]]
    stub = _StubManagerAdapter(names)
    conn = _FakeConn(existing)
    written: list[pl.DataFrame] = []
    monkeypatch.setattr(managers_run, "get_adapter", lambda slug: stub)
    sm = _sm([f"Shrink {w} Fund" for w in _WORDS]).with_columns(
        pl.lit("shrinkmgr").alias("amc_code")
    )
    monkeypatch.setattr(managers_run.q, "scheme_master", lambda **kw: sm)
    monkeypatch.setattr(
        managers_run.w, "upsert_portfolio_turnover",
        lambda df, conn=None: written.append(df) or len(df),
    )
    monkeypatch.setattr(dbconn, "connect", _fake_connect(conn))
    return conn, written


def test_ptr_5_to_3_refused_before_any_delete(monkeypatch):
    conn, written = _setup_managers(monkeypatch, n_incoming=3, existing=5)
    with pytest.raises(IngestError, match="PTR partition shrinkage"):
        managers_run.run_for_amc(
            "shrinkmgr", ym="2026-04", pdf_path=_FIXTURE_PDF,
        )
    assert conn.deletes() == []
    assert written == []


def test_ptr_same_count_proceeds(monkeypatch):
    conn, written = _setup_managers(monkeypatch, n_incoming=5, existing=5)
    res = managers_run.run_for_amc(
        "shrinkmgr", ym="2026-04", pdf_path=_FIXTURE_PDF,
    )
    assert len(conn.deletes()) == 1
    assert res["rows_written_ptr"] == 5


def test_ptr_force_overrides_with_loud_log(monkeypatch):
    conn, written = _setup_managers(monkeypatch, n_incoming=3, existing=5)
    with capture_logs() as logs:
        managers_run.run_for_amc(
            "shrinkmgr", ym="2026-04", pdf_path=_FIXTURE_PDF, force=True,
        )
    assert len(conn.deletes()) == 1 and len(written) == 1
    ev = [e for e in logs
          if e["event"] == "managers.ptr_partition_shrinkage_forced"]
    assert len(ev) == 1 and ev[0]["log_level"] == "warning"
