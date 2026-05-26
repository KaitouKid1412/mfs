"""Tests for the Bandhan (ex-IDFC) factsheet adapter.

Calibrated against the cached April 2026 Bandhan combined factsheet at
``data/raw/factsheets/bandhan/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise PTR extraction plus URL
construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.bandhan import (
    BandhanAdapter,
    _publish_ym,
    _scheme_name_from_page,
)

PDF = Path(__file__).parent.parent / "data" / "raw" / "factsheets" / "bandhan" / "2026-04.pdf"


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_uses_cms_permalink_for_april_2026():
    """Bandhan's CMS permalink groups files by PUBLISH year (data + 1)
    and embeds the data month + four-digit year in the slug."""
    a = BandhanAdapter()
    assert a.build_url("2026-04") == (
        "https://cmsnew.bandhanmutual.com/monthly-factsheets-2026/"
        "bandhan-factsheet-april-2026/"
    )


def test_build_url_rolls_publish_year_at_december():
    """Data month December 2026 publishes in January 2027, so the
    permalink lives under the 2027 monthly-factsheets index."""
    a = BandhanAdapter()
    assert a.build_url("2026-12") == (
        "https://cmsnew.bandhanmutual.com/monthly-factsheets-2027/"
        "bandhan-factsheet-december-2026/"
    )


def test_publish_ym_helper():
    assert _publish_ym("2026-04") == (2026, 5)
    assert _publish_ym("2025-12") == (2026, 1)


# ---------------------------------------------------------------------------
# Scheme-name detection (pure unit tests, no PDF required)
# ---------------------------------------------------------------------------


def test_scheme_name_strips_click_here_banner():
    """Most Bandhan scheme pages have ``<Name> Click here to Know more``
    as the first line — the trailing hyperlink banner must be trimmed."""
    text = "Bandhan Large Cap Fund Click here to Know more\nLarge Cap Fund\n..."
    assert _scheme_name_from_page(text) == "Bandhan Large Cap Fund"


def test_scheme_name_strips_footnote_glyphs():
    """Several Bandhan funds carry footnote symbols after the Fund
    suffix (Bandhan Large & Mid Cap Fund££, Bandhan Focused Fund§).
    Those must not leak into the printed name passed to fuzzy match."""
    text = "Bandhan Focused Fund§\nFocused Fund\n..."
    assert _scheme_name_from_page(text) == "Bandhan Focused Fund"

    text2 = "Bandhan Large & Mid Cap Fund££\n..."
    assert _scheme_name_from_page(text2) == "Bandhan Large & Mid Cap Fund"


def test_scheme_name_strips_etf_scrip_code():
    """ETF pages append a parenthetical scrip code (e.g.
    ``Bandhan BSE Sensex ETF(BSE scrip code: 540154)``). The bare ETF
    name should be returned without the scrip-code wart."""
    text = "Bandhan BSE Sensex ETF(BSE scrip code: 540154)\n..."
    assert _scheme_name_from_page(text) == "Bandhan BSE Sensex ETF"


def test_scheme_name_accepts_legacy_idfc_prefix():
    """Pre-rename factsheets still use ``IDFC `` headers. The same
    parser must work against archived IDFC-era PDFs."""
    text = "IDFC Large Cap Fund\nLarge Cap Fund\n..."
    assert _scheme_name_from_page(text) == "IDFC Large Cap Fund"


def test_scheme_name_rejects_back_cover_page():
    """Page 129 of the April-2026 factsheet is ``Bandhan AMC Offices``
    (cid-encoded 'ces'). It starts with ``Bandhan `` but doesn't end in
    Fund/FOF/ETF, so it must NOT be matched as a scheme page."""
    text = "Bandhan AMC O(cid:431)ces\nMumbai, Delhi, ..."
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_market_commentary_pages():
    text = "Market Outlook April 2026\nEquity markets ..."
    assert _scheme_name_from_page(text) is None


# ---------------------------------------------------------------------------
# PTR extraction against the cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> BandhanAdapter:
    return BandhanAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: BandhanAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Bandhan PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


def test_parse_ptr_yields_records(ptr_records):
    """April 2026 PDF carries PTR for ~36 schemes (equity, hybrid, index,
    retirement). Debt + arbitrage schemes don't print PTR."""
    assert len(ptr_records) >= 1


def test_parse_ptr_large_cap_fund(ptr_records):
    """Bandhan Large Cap Fund prints Equity PTR = 0.67 on the April
    2026 publish."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Bandhan Large Cap Fund" in by_name
    rec = by_name["Bandhan Large Cap Fund"]
    assert rec.ptr == pytest.approx(0.67, abs=1e-6)
    assert rec.source_amc == "bandhan"


def test_parse_ptr_balanced_advantage_anchored_correctly(ptr_records):
    """Bandhan Balanced Advantage Fund's page has ``Equity Total 66.87%``
    in the holdings table appearing immediately after the ``Portfolio
    Turnover Ratio`` label (extract_text artefact). The parser must NOT
    pick that up — the correct Equity PTR is 0.65."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Bandhan Balanced Advantage Fund" in by_name
    assert by_name["Bandhan Balanced Advantage Fund"].ptr == pytest.approx(
        0.65, abs=1e-6
    )


def test_parse_ptr_values_are_fractions(ptr_records):
    """Bandhan prints PTR as a fraction (not percent). All extracted
    values must be small positive numbers — a value > 100 would indicate
    an accidental percent-encoding bug somewhere upstream."""
    for r in ptr_records:
        assert r.ptr > 0, r
        assert r.ptr < 50, r  # Bandhan max observed: 2.48 (Business Cycle)
