"""Tests for the NSE Index Constituents ingester (Phase 2.2.B).

The ingester reads user-provided monthly weight CSVs from
data/raw/index_constituents/manual/<ticker_slug>/<YYYY-MM>.csv and
upserts to the index_constituents_monthly table. Tests use a temporary
directory so we don't touch the live data dir.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from mfs.ingest.constituents._run import (
    _parse_ym,
    _read_manual_csv,
    _records_for_ticker,
)


def _write_csv(path: Path, rows: list[tuple[str, float, str]]):
    """Write a minimal manual constituents CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("isin,weight_pct,security_name\n")
        for isin, weight, name in rows:
            f.write(f"{isin},{weight},{name}\n")


# ---------------------------------------------------------------------------
# _parse_ym
# ---------------------------------------------------------------------------


def test_parse_ym_normal():
    assert _parse_ym("2026-04") == date(2026, 4, 1)
    assert _parse_ym("2025-12") == date(2025, 12, 1)


def test_parse_ym_rejects_bad_input():
    assert _parse_ym("not-a-date") is None
    assert _parse_ym("2026-13") is None  # invalid month
    assert _parse_ym("April 2026") is None


# ---------------------------------------------------------------------------
# _read_manual_csv
# ---------------------------------------------------------------------------


def test_read_manual_csv_happy_path(tmp_path: Path):
    p = tmp_path / "2026-04.csv"
    _write_csv(p, [
        ("INE001A01036", 9.81, "Reliance Industries Ltd."),
        ("INE040A01034", 7.84, "HDFC Bank Ltd."),
    ])
    rows = _read_manual_csv(p)
    assert len(rows) == 2
    assert rows[0]["isin"] == "INE001A01036"
    assert rows[0]["weight_pct"] == 9.81
    assert rows[0]["security_name"] == "Reliance Industries Ltd."


def test_read_manual_csv_skips_empty_isin(tmp_path: Path):
    p = tmp_path / "2026-04.csv"
    p.write_text(
        "isin,weight_pct,security_name\n"
        "INE001A01036,9.81,Reliance\n"
        ",1.00,Bad Row\n"
        "INE040A01034,7.84,HDFC Bank\n"
    )
    rows = _read_manual_csv(p)
    assert len(rows) == 2
    assert all(r["isin"] for r in rows)


def test_read_manual_csv_strips_percent_sign(tmp_path: Path):
    p = tmp_path / "2026-04.csv"
    p.write_text(
        "isin,weight_pct,security_name\n"
        "INE001A01036,9.81%,Reliance\n"
    )
    rows = _read_manual_csv(p)
    assert len(rows) == 1
    assert rows[0]["weight_pct"] == 9.81


def test_read_manual_csv_skips_non_numeric_weight(tmp_path: Path):
    p = tmp_path / "2026-04.csv"
    p.write_text(
        "isin,weight_pct,security_name\n"
        "INE001A01036,not_a_number,Reliance\n"
        "INE040A01034,7.84,HDFC Bank\n"
    )
    rows = _read_manual_csv(p)
    assert len(rows) == 1
    assert rows[0]["isin"] == "INE040A01034"


def test_read_manual_csv_requires_isin_header(tmp_path: Path):
    """A CSV without an 'isin' column is a configuration error — fail loudly."""
    from mfs.errors import IngestError
    p = tmp_path / "2026-04.csv"
    p.write_text("company,weight,name\nReliance,10,Reliance Industries\n")
    with pytest.raises(IngestError, match="isin"):
        _read_manual_csv(p)


def test_read_manual_csv_requires_weight_header(tmp_path: Path):
    from mfs.errors import IngestError
    p = tmp_path / "2026-04.csv"
    p.write_text("isin,name\nINE001A01036,Reliance\n")
    with pytest.raises(IngestError, match="weight_pct"):
        _read_manual_csv(p)


# ---------------------------------------------------------------------------
# _records_for_ticker — uses real paths.index_constituents_manual_dir
# ---------------------------------------------------------------------------


def test_records_for_ticker_returns_empty_when_no_dir(tmp_path: Path, monkeypatch):
    """If the manual directory for a ticker doesn't exist, the iterator yields
    nothing (not an error). This is the normal pre-ingest state."""
    from mfs import paths
    # Redirect data_dir to an empty temp location
    monkeypatch.setattr(paths, "raw_dir", lambda: tmp_path / "raw")
    out = list(_records_for_ticker("NIFTY 100 TRI"))
    assert out == []


def test_records_for_ticker_picks_up_multiple_months(tmp_path: Path, monkeypatch):
    from mfs import paths
    monkeypatch.setattr(paths, "raw_dir", lambda: tmp_path / "raw")
    base = tmp_path / "raw" / "index_constituents" / "manual" / "nifty_100_tri"
    _write_csv(base / "2026-03.csv", [
        ("INE001A01036", 9.50, "Reliance Industries Ltd."),
        ("INE040A01034", 7.50, "HDFC Bank Ltd."),
    ])
    _write_csv(base / "2026-04.csv", [
        ("INE001A01036", 9.81, "Reliance Industries Ltd."),
        ("INE040A01034", 7.84, "HDFC Bank Ltd."),
    ])
    records = list(_records_for_ticker("NIFTY 100 TRI"))
    # 2 months × 2 stocks = 4 records
    assert len(records) == 4
    months_seen = {r.as_of_month for r in records}
    assert months_seen == {date(2026, 3, 1), date(2026, 4, 1)}


def test_records_for_ticker_ignores_files_with_bad_filenames(
    tmp_path: Path, monkeypatch
):
    from mfs import paths
    monkeypatch.setattr(paths, "raw_dir", lambda: tmp_path / "raw")
    base = tmp_path / "raw" / "index_constituents" / "manual" / "nifty_100_tri"
    base.mkdir(parents=True)
    # Bad filenames (not YYYY-MM) should be skipped without error.
    _write_csv(base / "garbage.csv", [("INE001A01036", 9.81, "Reliance")])
    _write_csv(base / "2026-04.csv", [("INE001A01036", 9.81, "Reliance")])
    records = list(_records_for_ticker("NIFTY 100 TRI"))
    assert len(records) == 1
    assert records[0].as_of_month == date(2026, 4, 1)


def test_ticker_slug_helper():
    from mfs.paths import ticker_slug
    assert ticker_slug("NIFTY 100 TRI") == "nifty_100_tri"
    assert ticker_slug("Nifty Smallcap 250 TRI") == "nifty_smallcap_250_tri"
    assert ticker_slug("S&P BSE Sensex TRI") == "s_p_bse_sensex_tri"
