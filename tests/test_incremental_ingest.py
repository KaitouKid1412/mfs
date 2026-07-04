"""Tests for incremental-ingest start/window resolution (Phase 3).

Covers the pure decision logic that decides how far back each stage fetches:
  * benchmarks `_resolve_start` — full vs cold-start vs tail-from-watermark;
  * bhavcopy `ingest_recent` — incremental window vs full walk;
  * amfi_nav `ingest_incremental` — inclusive watermark + today's snapshot,
    and the raw-window cache bypass for a window ending today.

The correctness invariant: incremental must never fetch from *after* a gap it
should be closing. Cold/empty state and `full=True` always fall back to the
full history/window.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from mfs.errors import IngestError
from mfs.ingest import amfi_nav, benchmarks, bhavcopy


def _cfg(history_start=date(2013, 1, 1), tail=90):
    return SimpleNamespace(
        ingest=SimpleNamespace(
            benchmarks=SimpleNamespace(
                history_start=history_start, incremental_tail_days=tail
            )
        )
    )


# --- benchmarks._resolve_start --------------------------------------------

def test_full_forces_history_start():
    cfg = _cfg()
    s = benchmarks._resolve_start("NIFTY 50 TRI", cfg, full=True,
                                  latest_map={"NIFTY 50 TRI": date(2026, 6, 1)})
    assert s == date(2013, 1, 1)


def test_cold_ticker_uses_history_start():
    cfg = _cfg()
    # ticker not present in the watermark map → no rows yet → full backfill
    s = benchmarks._resolve_start("NIFTY 50 TRI", cfg, full=False, latest_map={})
    assert s == date(2013, 1, 1)


def test_incremental_tail_from_watermark():
    cfg = _cfg(tail=90)
    latest = date(2026, 6, 1)
    s = benchmarks._resolve_start("NIFTY 50 TRI", cfg, full=False,
                                  latest_map={"NIFTY 50 TRI": latest})
    assert s == latest - timedelta(days=90)


def test_tail_clamped_to_history_start():
    # If the watermark minus tail predates history_start, clamp up.
    cfg = _cfg(history_start=date(2026, 5, 1), tail=90)
    s = benchmarks._resolve_start("X", cfg, full=False,
                                  latest_map={"X": date(2026, 6, 1)})
    assert s == date(2026, 5, 1)


# --- bhavcopy.ingest_recent window ----------------------------------------

def _patch_bhavcopy(monkeypatch, latest):
    """Stub network + DB; record which day-offsets get walked."""
    walked = []
    monkeypatch.setattr(bhavcopy.q, "latest_stock_adv_date", lambda: latest)
    monkeypatch.setattr(bhavcopy, "fetch_equity_list", lambda: {})

    def fake_fetch_one(d, symbol_to_isin=None):
        walked.append(d)
        return 0

    monkeypatch.setattr(bhavcopy, "fetch_one", fake_fetch_one)
    return walked


def test_bhavcopy_full_walks_all_days(monkeypatch):
    walked = _patch_bhavcopy(monkeypatch, latest=date(2026, 6, 1))
    bhavcopy.ingest_recent(n_days=75, full=True)
    # full ignores the watermark → walks the whole 75-day window (minus weekends)
    assert len(walked) > 40  # ~53 weekdays in 75 calendar days


def test_bhavcopy_incremental_limits_window(monkeypatch):
    # latest only a few days ago → walk a small window, not 75 days.
    today = date.today()
    latest = today - timedelta(days=4)
    walked = _patch_bhavcopy(monkeypatch, latest=latest)
    bhavcopy.ingest_recent(n_days=75, full=False)
    # window = (today-latest).days + 3 = 7 calendar days → at most 7 weekday walks
    assert len(walked) <= 7


def test_bhavcopy_cold_db_walks_full_window(monkeypatch):
    walked = _patch_bhavcopy(monkeypatch, latest=None)
    bhavcopy.ingest_recent(n_days=75, full=False)
    assert len(walked) > 40  # empty table → full backfill


# --- amfi_nav.ingest_incremental (self-healing NAV stage) -------------------

def test_nav_incremental_fetches_from_watermark_inclusive(monkeypatch):
    """The bulk fetch starts AT the watermark (inclusive), so a partial last
    day is re-fetched and repaired; today's NAVAll snapshot is layered after."""
    calls = []
    monkeypatch.setattr(
        amfi_nav.q, "latest_dates", lambda: {"nav_latest": date(2026, 6, 8)}
    )

    def fake_since(since):
        calls.append(("since", since))
        return 100, [2025, 2026]

    def fake_today():
        calls.append(("today",))
        return 7, [2026]

    monkeypatch.setattr(amfi_nav, "ingest_since", fake_since)
    monkeypatch.setattr(amfi_nav, "ingest_today", fake_today)

    n, years = amfi_nav.ingest_incremental()
    assert calls == [("since", date(2026, 6, 8)), ("today",)]
    assert n == 107
    assert years == [2025, 2026]  # merged + deduped + sorted


def test_nav_incremental_empty_db_raises(monkeypatch):
    """No watermark to heal from → fail fast, point at the full backfill."""
    monkeypatch.setattr(amfi_nav.q, "latest_dates", lambda: {"nav_latest": None})
    monkeypatch.setattr(
        amfi_nav, "ingest_since",
        lambda since: pytest.fail("must not fetch from an empty DB"),
    )
    with pytest.raises(IngestError, match="--backfill"):
        amfi_nav.ingest_incremental()


# --- amfi_nav raw-window cache: bypass for a window ending today ------------

_NAV_HISTORY_SAMPLE = """Scheme Code;Scheme Name;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Net Asset Value;Repurchase Price;Sale Price;Date

Open Ended Schemes ( Equity Scheme - Large Cap Fund )

Test Mutual Fund

100001;Test Fund - Direct Plan - Growth;INF000000001;INF000000002;{nav};;;05-Jun-2026
"""


def _patch_nav_window(monkeypatch, tmp_path, fresh_text):
    """Stub network + DB + raw-cache location; record fetch_window calls."""
    fetched = []

    def fake_fetch(from_d, to_d):
        fetched.append((from_d, to_d))
        return fresh_text.encode()

    monkeypatch.setattr(
        amfi_nav.paths, "amfi_history_raw",
        lambda from_s, to_s: tmp_path / f"{from_s}_{to_s}.txt",
    )
    monkeypatch.setattr(amfi_nav, "fetch_window", fake_fetch)
    monkeypatch.setattr(amfi_nav.w, "upsert_nav_daily", lambda df, **kw: None)
    return fetched


def test_window_ending_today_bypasses_cache(monkeypatch, tmp_path):
    """A cached window ending TODAY must be re-fetched (a same-day retry after
    a sparse AMFI response must not replay stale bytes) and overwritten."""
    today = date.today()
    start = today - timedelta(days=5)
    raw = tmp_path / f"{start.isoformat()}_{today.isoformat()}.txt"
    raw.write_bytes(_NAV_HISTORY_SAMPLE.format(nav="100.00").encode())
    fetched = _patch_nav_window(
        monkeypatch, tmp_path, _NAV_HISTORY_SAMPLE.format(nav="200.00")
    )
    amfi_nav.ingest_backfill(start=start, end=today)
    assert fetched == [(start, today)]   # cache ignored: window ends today
    assert "200.00" in raw.read_text()   # fresh bytes overwrote the stale cache


def test_window_ending_before_today_uses_cache(monkeypatch, tmp_path):
    """Historical windows are immutable: a cached window ending before today
    is read from disk, never re-fetched."""
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=5)
    raw = tmp_path / f"{start.isoformat()}_{end.isoformat()}.txt"
    raw.write_bytes(_NAV_HISTORY_SAMPLE.format(nav="100.00").encode())
    fetched = _patch_nav_window(monkeypatch, tmp_path, "unused")
    amfi_nav.ingest_backfill(start=start, end=end)
    assert fetched == []                 # historical window served from cache


# ---------------------------------------------------------------------------
# B9: --full force semantics — re-download the current data month's artifacts.
#
# fetch()/fetch_excel() return cached files by mere existence, so a corrected
# or re-published artifact was unreachable without manual deletion. With
# force=True the orchestrators delete the data month's canonical cached path
# BEFORE fetch, so the (monkeypatched) downloader runs and the NEW bytes are
# parsed; force=False must never call the downloader for a cached artifact.
# ---------------------------------------------------------------------------

from contextlib import contextmanager  # noqa: E402

import openpyxl  # noqa: E402
import polars as pl  # noqa: E402

import mfs.db.connection as dbconn  # noqa: E402
import mfs.io.http as mfs_http  # noqa: E402
import mfs.paths as mfs_paths  # noqa: E402
from mfs.ingest.holdings import _generic as holdings_generic  # noqa: E402
from mfs.ingest.holdings import _run as holdings_run  # noqa: E402
from mfs.ingest.holdings._generic import GenericHoldingsAdapter  # noqa: E402
from mfs.ingest.managers import _run as managers_run  # noqa: E402
from mfs.ingest.managers._base import ManagerAdapter  # noqa: E402
from mfs.schemas import ParsedPtrRecord  # noqa: E402

_YM = "2026-05"


class _ForceFakeConn:
    """Minimal connection: scripts every scalar query as 'nothing prior'."""

    def execute(self, sql, params=None):
        class _Cur:
            def fetchone(self):
                return (0,) if "count(distinct" in " ".join(sql.split()).lower() else None

        return _Cur()


@contextmanager
def _force_fake_connect(autocommit=False):
    yield _ForceFakeConn()


def _force_sm(amc: str) -> pl.DataFrame:
    return pl.DataFrame({
        "scheme_code": ["X01"],
        "scheme_name": ["Force Alpha Fund"],
        "amc_code": [amc],
        "plan_type": ["DIRECT"],
        "option_type": ["GROWTH"],
        "is_active": [True],
        "canonical_category": ["Flexi Cap"],
    })


# --- managers (factsheet PDF) path ------------------------------------------


class _ForceMgrAdapter(ManagerAdapter):
    """Uses the INHERITED fetch() — the cached-by-existence path under test."""

    amc_slug = "forceamc"
    source_label = "Force AMC"

    def build_url(self, ym):
        return f"http://x.test/{ym}.pdf"

    def parse_ptr(self, pdf_path, ym):
        # "Parsed content reflects the bytes on disk": the artifact body IS
        # the PTR value, so the assertion sees exactly which bytes won.
        val = float(pdf_path.read_text().split("=")[1])
        return [ParsedPtrRecord(
            scheme_name_printed="Force Alpha Fund", ptr=val,
            source_amc=self.amc_slug,
        )]


def _setup_managers_force(monkeypatch, tmp_path):
    monkeypatch.setattr(
        mfs_paths, "factsheet_raw",
        lambda amc_slug, ym: tmp_path / "factsheets" / amc_slug / f"{ym}.pdf",
    )
    downloads: list[str] = []

    def fake_download(url, out, expect=None):
        downloads.append(url)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("PTR=2.0")  # the NEW (re-published) bytes
        return out

    monkeypatch.setattr(mfs_http, "download_to", fake_download)
    monkeypatch.setattr(managers_run, "get_adapter", lambda slug: _ForceMgrAdapter())
    monkeypatch.setattr(managers_run.q, "scheme_master", lambda **kw: _force_sm("forceamc"))
    monkeypatch.setattr(managers_run.q, "has_factsheet_rows", lambda *a, **k: False)
    written: list[pl.DataFrame] = []
    monkeypatch.setattr(
        managers_run.w, "upsert_portfolio_turnover",
        lambda df, conn=None: written.append(df) or len(df),
    )
    monkeypatch.setattr(dbconn, "connect", _force_fake_connect)
    # Pre-seed the month's cached artifact with the OLD bytes.
    cached = tmp_path / "factsheets" / "forceamc" / f"{_YM}.pdf"
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text("PTR=0.5")
    return downloads, written, cached


def test_managers_force_true_redownloads_and_parses_new_bytes(monkeypatch, tmp_path):
    downloads, written, cached = _setup_managers_force(monkeypatch, tmp_path)
    res = managers_run.run_for_amc("forceamc", ym=_YM, force=True)
    assert downloads == [f"http://x.test/{_YM}.pdf"]   # downloader was called
    assert res["rows_written_ptr"] == 1
    assert written[0]["ptr"].to_list() == [2.0]        # NEW bytes parsed in
    assert cached.read_text() == "PTR=2.0"             # cache replaced


def test_managers_force_false_serves_cache_without_download(monkeypatch, tmp_path):
    downloads, written, cached = _setup_managers_force(monkeypatch, tmp_path)
    res = managers_run.run_for_amc("forceamc", ym=_YM, force=False)
    assert downloads == []                             # downloader never called
    assert written[0]["ptr"].to_list() == [0.5]        # OLD cached bytes parsed
    assert cached.read_text() == "PTR=0.5"


# --- holdings (per-scheme Excel) path ----------------------------------------


class _ForceHoldAdapter(GenericHoldingsAdapter):
    """Uses the INHERITED fetch_excel() — the cached-by-existence path."""

    amc_slug = "forceh"
    source_label = "Force Holdings"

    def discover_scheme_urls(self, ym):
        return {"Force Alpha Fund": "http://x.test/a.xlsx"}


def _write_portfolio_xlsx(path, prefix: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["ISIN", "Name of the Instrument", "Industry", "% to NAV"])
    for i in range(6):
        ws.append([f"INE{i:03d}B0104{i % 10}", f"{prefix} {i}", "X", 16.5])
    wb.save(path)


def _setup_holdings_force(monkeypatch, tmp_path):
    def fake_excel_raw(amc_slug, ym, scheme_filename):
        return tmp_path / "holdings" / amc_slug / ym / scheme_filename.replace("/", "_")

    monkeypatch.setattr(mfs_paths, "holdings_excel_raw", fake_excel_raw)
    downloads: list[str] = []

    def fake_download(url, out, expect=None):
        downloads.append(url)
        _write_portfolio_xlsx(out, "NewCo")           # the NEW bytes
        return out

    monkeypatch.setattr(holdings_generic, "download_to", fake_download)
    monkeypatch.setattr(holdings_run, "get_adapter", lambda slug: _ForceHoldAdapter())
    monkeypatch.setattr(holdings_run.q, "scheme_master", lambda **kw: _force_sm("forceh"))
    written: list[pl.DataFrame] = []
    monkeypatch.setattr(
        holdings_run.w, "upsert_holdings",
        lambda df, conn=None: written.append(df) or len(df),
    )
    monkeypatch.setattr(dbconn, "connect", _force_fake_connect)
    # Pre-seed the month's cached Excel with the OLD bytes.
    cached = fake_excel_raw("forceh", _YM, "Force Alpha Fund.xlsx")
    _write_portfolio_xlsx(cached, "OldCo")
    return downloads, written


def test_holdings_force_true_redownloads_and_parses_new_bytes(monkeypatch, tmp_path):
    downloads, written = _setup_holdings_force(monkeypatch, tmp_path)
    res = holdings_run.run_for_amc("forceh", ym=_YM, force=True)
    assert downloads == ["http://x.test/a.xlsx"]       # downloader was called
    assert res["rows_written"] == 6
    names = written[0]["security_name"].to_list()
    assert all(n.startswith("NewCo") for n in names)   # NEW bytes parsed in


@pytest.mark.db
def test_holdings_force_false_serves_cache_without_download(monkeypatch, tmp_path):
    downloads, written = _setup_holdings_force(monkeypatch, tmp_path)
    res = holdings_run.run_for_amc("forceh", ym=_YM, force=False)
    assert downloads == []                             # downloader never called
    assert res["rows_written"] == 6
    names = written[0]["security_name"].to_list()
    assert all(n.startswith("OldCo") for n in names)   # cached bytes parsed


# ---------------------------------------------------------------------------
# C3: ingest_all_known parallelizes over tickers (ThreadPool, 3 workers) while
# preserving per-ticker failure aggregation and the equity-failure IngestError.
# ---------------------------------------------------------------------------

import threading  # noqa: E402

_C3_START = date(2026, 5, 1)  # explicit start → no DB watermark query


def test_ingest_all_known_covers_every_ticker(monkeypatch):
    seen: list[str] = []
    lock = threading.Lock()

    def fake_ingest(ticker, start=None, end=None, **kw):
        with lock:
            seen.append(ticker)
        return 7

    monkeypatch.setattr(benchmarks, "ingest_ticker", fake_ingest)
    out = benchmarks.ingest_all_known(start=_C3_START)
    assert set(out) == set(benchmarks.NSE_INDEX_NAME_MAP)
    assert set(seen) == set(benchmarks.NSE_INDEX_NAME_MAP)
    assert all(n == 7 for n in out.values())


def test_ingest_all_known_aggregates_every_equity_failure(monkeypatch):
    tickers = list(benchmarks.NSE_INDEX_NAME_MAP)
    bad = {tickers[0], tickers[3]}

    def fake_ingest(ticker, start=None, end=None, **kw):
        if ticker in bad:
            raise RuntimeError(f"NSE choked on {ticker}")
        return 5

    monkeypatch.setattr(benchmarks, "ingest_ticker", fake_ingest)
    with pytest.raises(IngestError) as ei:
        benchmarks.ingest_all_known(start=_C3_START)
    msg = str(ei.value)
    assert "2 ticker(s) failed" in msg
    for t in bad:
        assert t in msg  # BOTH failures listed — raise waits for the pool


def test_ingest_all_known_is_concurrent(monkeypatch):
    """Passes only if ≥2 tickers are in flight at once (3-worker pool)."""
    barrier = threading.Barrier(2, timeout=5)
    released = {"n": 0}
    lock = threading.Lock()

    def fake_ingest(ticker, start=None, end=None, **kw):
        # Only the first two participants need to rendezvous; later tickers
        # pass straight through so the run completes quickly.
        with lock:
            released["n"] += 1
            n = released["n"]
        if n <= 2:
            barrier.wait()
        return 1

    monkeypatch.setattr(benchmarks, "ingest_ticker", fake_ingest)
    out = benchmarks.ingest_all_known(start=_C3_START)
    assert all(n == 1 for n in out.values())


def test_non_nse_mapped_zero_rows_is_not_an_equity_failure(monkeypatch):
    """A hybrid/manual-CSV ticker (empty NSE mapping) returning 0 rows stays
    exempt from the equity-failure aggregation (existing exemption)."""
    monkeypatch.setattr(
        benchmarks, "NSE_TRI_MAP",
        {"EQ TRI": ("EQ", "Eq Index"), "HYBRID TRI": ("", "")},
    )
    monkeypatch.setattr(
        benchmarks, "NSE_INDEX_NAME_MAP", {"EQ TRI": "Eq Index", "HYBRID TRI": ""},
    )

    def fake_ingest(ticker, start=None, end=None, **kw):
        return 0 if ticker == "HYBRID TRI" else 9

    monkeypatch.setattr(benchmarks, "ingest_ticker", fake_ingest)
    out = benchmarks.ingest_all_known(start=_C3_START)  # must NOT raise
    assert out == {"EQ TRI": 9, "HYBRID TRI": 0}
