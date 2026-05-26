"""Tests for the Nippon India Mutual Fund factsheet adapter (Phase 3.D).

Calibrated against the cached April 2026 PDF at
``data/raw/factsheets/nippon/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise PTR extraction plus URL
construction. No network access is performed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.nippon import (
    NipponAdapter,
    _scheme_name_from_page,
)

PDF = Path(__file__).parent.parent / "data" / "raw" / "factsheets" / "nippon" / "2026-04.pdf"


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_uses_upper_case_month_abbreviation():
    """Nippon's filename embeds the DATA month as an UPPER-case 3-letter
    abbreviation (e.g. APR for April).
    """
    a = NipponAdapter()
    assert a.build_url("2026-04") == (
        "https://mf.nipponindiaim.com/InvestorServices/FactSheetsDocuments/"
        "Nippon-FS-APR-2026.pdf"
    )


def test_build_url_december_does_not_roll_year():
    """Unlike Kotak, Nippon's URL embeds the DATA month, not the publish
    month, so a December data month uses ``DEC`` and the same year — no
    rollover.
    """
    a = NipponAdapter()
    assert a.build_url("2026-12") == (
        "https://mf.nipponindiaim.com/InvestorServices/FactSheetsDocuments/"
        "Nippon-FS-DEC-2026.pdf"
    )


# ---------------------------------------------------------------------------
# _scheme_name_from_page
# ---------------------------------------------------------------------------


def test_scheme_name_accepts_standard_nippon_india_prefix():
    text = "Nippon India Large Cap Fund\nSome other line\n"
    assert _scheme_name_from_page(text) == "Nippon India Large Cap Fund"


def test_scheme_name_rejects_non_nippon_first_line():
    text = "Disclaimer\nNippon India Foo Fund\n"
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_ter_aggregation_pages():
    """TER aggregation pages (110-114) concatenate two scheme names on the
    first line. We must drop those — they don't carry per-scheme PTR/AUM.
    """
    text = (
        "Nippon India Growth Mid Cap Fund Nippon India Dynamic Bond Fund\n"
        "TER table follows..."
    )
    assert _scheme_name_from_page(text) is None


def test_scheme_name_collapses_whitespace():
    text = "Nippon India  Large   Cap  Fund\n"
    assert _scheme_name_from_page(text) == "Nippon India Large Cap Fund"


def test_scheme_name_returns_none_for_empty_text():
    assert _scheme_name_from_page("") is None
    assert _scheme_name_from_page(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PTR extraction against the cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> NipponAdapter:
    return NipponAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: NipponAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Nippon PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


# ---------- PTR ----------


def test_parse_ptr_yields_at_least_thirty_records(ptr_records):
    """Floor: the rewrite targets >=30 PTR rows; below that the rewrite
    failed to improve over the legacy 14-row state."""
    assert len(ptr_records) >= 30, (
        f"only {len(ptr_records)} PTR records; expected >=30"
    )


def test_parse_ptr_known_scheme_large_cap(ptr_records):
    """Nippon India Large Cap Fund prints PTR on page 5 in the cached PDF."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Nippon India Large Cap Fund" in by_name
    rec = by_name["Nippon India Large Cap Fund"]
    # Nippon stores PTR as fraction directly — should be in a sane range.
    assert 0 < rec.ptr < 5.0
    assert rec.source_amc == "nippon"


def test_parse_ptr_values_are_fraction(ptr_records):
    """Nippon prints PTR as a fraction directly (no percent → fraction
    conversion). Guard against an accidental misread of percent values.
    """
    for r in ptr_records:
        assert r.ptr > 0, r
        # The Nippon Quant Fund tends to show the highest PTR in the
        # universe (often >2.0 = 200%); a fraction value above 50
        # would imply we accidentally read a percent (e.g. 27% read
        # as 27.0 fraction).
        assert r.ptr < 50, r


def test_parse_ptr_drops_sentinel_pages(ptr_records):
    """Page 20 (US Equity Opportunities Fund) prints
    ``Portfolio Turnover (Times) --`` (a literal dash). That scheme must
    NOT appear in ptr_records.
    """
    names = {r.scheme_name_printed for r in ptr_records}
    assert "Nippon India US Equity Opportunities Fund" not in names
