"""Tests for the HSBC Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/hsbc/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise PTR / AUM extraction plus URL
construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.hsbc import (
    HsbcAdapter,
    _scheme_name_from_page,
)

PDF = Path(__file__).parent.parent / "data" / "raw" / "factsheets" / "hsbc" / "2026-04.pdf"


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_april_2026():
    """HSBC's actual PDF URL embeds a per-upload UUID, so build_url
    returns a stable template with a ``LISTING`` placeholder — the real
    download URL is resolved at fetch time from the listing page."""
    a = HsbcAdapter()
    assert a.build_url("2026-04") == (
        "https://www.assetmanagement.hsbc.co.in/assets/documents/"
        "mutual-funds/en/LISTING/the-asset-april-2026.pdf"
    )


def test_build_url_december():
    a = HsbcAdapter()
    assert a.build_url("2026-12") == (
        "https://www.assetmanagement.hsbc.co.in/assets/documents/"
        "mutual-funds/en/LISTING/the-asset-december-2026.pdf"
    )


def test_build_url_january():
    a = HsbcAdapter()
    assert a.build_url("2027-01") == (
        "https://www.assetmanagement.hsbc.co.in/assets/documents/"
        "mutual-funds/en/LISTING/the-asset-january-2027.pdf"
    )


# ---------------------------------------------------------------------------
# Scheme-name detection (pure unit tests — no PDF required)
# ---------------------------------------------------------------------------


def test_scheme_name_simple():
    """Most HSBC scheme pages have a clean first line ``HSBC <name> Fund``."""
    text = "HSBC Large Cap Fund\nLarge Cap Fund - An open ended equity scheme...\n..."
    assert _scheme_name_from_page(text) == "HSBC Large Cap Fund"


def test_scheme_name_strips_footnote_glyph():
    """Page 32 prints ``HSBC Global Emerging Markets Fund*`` — strip the
    trailing footnote glyph so canonical name lines up with scheme_master.
    """
    text = "HSBC Global Emerging Markets Fund*\nAn open ended fund of fund scheme...\n..."
    assert (
        _scheme_name_from_page(text) == "HSBC Global Emerging Markets Fund"
    )


def test_scheme_name_accepts_fof_suffix():
    """FoF schemes end in ``FOF`` rather than ``Fund``."""
    text = "HSBC Aggressive Hybrid Active FOF\nHybrid FoF - An open-ended...\n..."
    assert (
        _scheme_name_from_page(text) == "HSBC Aggressive Hybrid Active FOF"
    )


def test_scheme_name_accepts_etf_suffix():
    text = "HSBC Gold ETF\nETF...\n..."
    assert _scheme_name_from_page(text) == "HSBC Gold ETF"


def test_scheme_name_rejects_cover():
    """Page 1 prints just the page number, no HSBC prefix."""
    text = "6"
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_ceo_speak():
    text = "CEO speak\nDiscipline in investing - the only way forward.\n..."
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_index_page():
    """The TOC line ``Index | How to read Factsheet 03`` doesn't start
    with ``HSBC ``."""
    text = "Index | How to read Factsheet 03 HSBC Aggressive Hybrid Active FOF 34..."
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_empty():
    assert _scheme_name_from_page("") is None
    assert _scheme_name_from_page("\n\n") is None


def test_scheme_name_rejects_back_cover_disclosure():
    """Disclosure / performance / IDCW pages don't start with ``HSBC ``
    and must be rejected."""
    assert _scheme_name_from_page("Statutory Details & Disclaimers\n...") is None
    assert _scheme_name_from_page("Product Labelling\n...") is None
    assert _scheme_name_from_page("SIP Performance Equity Schemes - Direct Plan\n...") is None
    # And an ``HSBC `` line that lacks any Fund/FOF/ETF suffix on line 1.
    assert _scheme_name_from_page("HSBC Asset Management Office Addresses\n...") is None


# ---------------------------------------------------------------------------
# PTR / AUM against cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> HsbcAdapter:
    return HsbcAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: HsbcAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached HSBC PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


def test_parse_ptr_yields_records(ptr_records):
    """April 2026 PDF carries PTR for 24 schemes (active equity + index +
    ETF + hybrid + equity FOFs). Pure debt / liquid / overnight / debt
    FOF / target-maturity-index pages legitimately omit PTR."""
    assert len(ptr_records) >= 1
    # Calibration: 24 in the captured PDF — guard against regressions.
    assert len(ptr_records) >= 20


def test_parse_ptr_known_scheme(ptr_records):
    """HSBC Large Cap Fund prints ``Portfolio Turnover (1 year) 0.42`` on
    April 30, 2026. HSBC stores PTR as a fraction directly — no
    percent→fraction conversion."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "HSBC Large Cap Fund" in by_name
    rec = by_name["HSBC Large Cap Fund"]
    assert rec.ptr == pytest.approx(0.42, abs=1e-6)
    assert rec.source_amc == "hsbc"


def test_parse_ptr_handles_column_bleed(ptr_records):
    """HSBC Large and Mid Cap Fund's PTR row column-bleeds with the
    right-column ``Portfolio Classification`` table on the page, so the
    text-extract regex fails to anchor. The word-position extractor
    must recover the value ``1.36``."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "HSBC Large and Mid Cap Fund" in by_name
    assert by_name["HSBC Large and Mid Cap Fund"].ptr == pytest.approx(
        1.36, abs=1e-6
    )


def test_parse_ptr_hybrid_uses_equity_turnover(ptr_records):
    """Hybrid / arbitrage pages print three stacked rows: ``Portfolio
    Turnover (1 year)`` (no value), ``Equity Turnover 2.38``, ``Total
    Turnover 5.70``. The adapter must always pick ``Equity Turnover``
    (stock-picking activity), never ``Total Turnover`` (which adds
    debt + derivative rotation and can be 2-3× higher).

    HSBC Arbitrage Fund equity turnover = 2.38 (total = 5.70). The
    test asserts we picked 2.38."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "HSBC Arbitrage Fund" in by_name
    assert by_name["HSBC Arbitrage Fund"].ptr == pytest.approx(
        2.38, abs=1e-6
    )


def test_parse_ptr_values_positive_and_fractional(ptr_records):
    """Fail-fast: zero / NaN PTR must be dropped. PTR is a fraction —
    arbitrage strategies can reach 5× but nothing in the HSBC book
    exceeds 10× in practice."""
    for r in ptr_records:
        assert r.ptr > 0, r
        assert r.ptr < 10, r


# ---------------------------------------------------------------------------


