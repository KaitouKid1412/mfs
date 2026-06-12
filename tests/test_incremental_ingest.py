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
