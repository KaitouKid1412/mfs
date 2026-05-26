"""Tests for the Motilal Oswal Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 "Most Factsheet April 2026
Active" combined factsheet at
``data/raw/factsheets/motilal_oswal/2026-04.pdf``. Tests run entirely
offline — they read the bundled PDF and exercise PTR extraction
plus URL construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.motilal_oswal import (
    MotilalOswalAdapter,
    _publish_ym,
    _scheme_name_from_page,
)

PDF = (
    Path(__file__).parent.parent
    / "data"
    / "raw"
    / "factsheets"
    / "motilal_oswal"
    / "2026-04.pdf"
)


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_april_2026():
    """Motilal Oswal's canonical resolver is the AEM search-documents API
    (the per-month PDF filenames are human-readable strings like
    ``Most Factsheet April 2026 Active.pdf`` that occasionally carry a
    "Most " prefix). ``build_url`` therefore returns the API endpoint
    with the publish month (data_ym + 1 = 2026-05), and ``fetch``
    resolves the data-month PDF path from the API response."""
    a = MotilalOswalAdapter()
    assert a.build_url("2026-04") == (
        "https://www.motilaloswalmf.com/content/"
        "aem-cloud-dept-backend-motilal-oswal/api/search-documents.json"
        "?year=2026&category=factsheet&month=may&type=mf"
    )


def test_build_url_year_rollover():
    """December 2025 data is published in January 2026 — the API
    endpoint reflects the publish month and rolls the year forward."""
    a = MotilalOswalAdapter()
    assert a.build_url("2025-12") == (
        "https://www.motilaloswalmf.com/content/"
        "aem-cloud-dept-backend-motilal-oswal/api/search-documents.json"
        "?year=2026&category=factsheet&month=jan&type=mf"
    )


def test_publish_ym_rollover():
    """``_publish_ym`` shifts a YYYY-MM string forward by one calendar
    month with December → January year-rollover."""
    assert _publish_ym("2026-04") == "2026-05"
    assert _publish_ym("2025-12") == "2026-01"
    assert _publish_ym("2026-01") == "2026-02"


# ---------------------------------------------------------------------------
# Scheme-name detection (pure unit tests — no PDF required)
# ---------------------------------------------------------------------------


def test_scheme_name_equity():
    """Every Motilal Oswal scheme page begins ``Motilal Oswal <Name> Fund``."""
    text = (
        "Motilal Oswal Large Cap Fund\n"
        "(An open ended equity scheme predominantly investing in large cap stocks)"
    )
    assert _scheme_name_from_page(text) == "Motilal Oswal Large Cap Fund"


def test_scheme_name_thematic():
    text = (
        "Motilal Oswal Innovation Opportunities Fund\n"
        "(An open-ended equity scheme following innovation theme)"
    )
    assert (
        _scheme_name_from_page(text)
        == "Motilal Oswal Innovation Opportunities Fund"
    )


def test_scheme_name_elss():
    text = "Motilal Oswal ELSS Tax Saver Fund\n..."
    assert _scheme_name_from_page(text) == "Motilal Oswal ELSS Tax Saver Fund"


def test_scheme_name_debt():
    text = (
        "Motilal Oswal Ultra Short Term Fund\n"
        "(An open ended ultra-short term debt scheme...)"
    )
    assert (
        _scheme_name_from_page(text)
        == "Motilal Oswal Ultra Short Term Fund"
    )


def test_scheme_name_rejects_cover():
    """The cover (p1) prints an NFO announcement / banner copy, not a
    scheme title."""
    text = "As on 30 April 2026\nIntroducing\nMotilal Oswal Contra Fund\n..."
    # First non-empty line is "As on 30 April 2026" — must be rejected.
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_index():
    """The TOC / INDEX page starts with ``INDEX`` followed by entries."""
    text = (
        "INDEX\n"
        "1 Market Outlook\n"
        "2 Equity, Debt & Hybrid Funds\n"
        "3 Motilal Oswal Large Cap Fund 1\n"
    )
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_aum_disclosure():
    text = "Assets Under Management\nAUM REPORT FOR THE QUARTER ENDED ...\n"
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_empty():
    assert _scheme_name_from_page("") is None
    assert _scheme_name_from_page("\n\n") is None


# ---------------------------------------------------------------------------
# PTR against cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> MotilalOswalAdapter:
    return MotilalOswalAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: MotilalOswalAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Motilal Oswal PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


def test_parse_ptr_yields_records(ptr_records):
    """April 2026 Active factsheet has 23 scheme pages; 22 carry the
    ``Portfolio Turnover Ratio`` line (Financial Services Fund's p22
    has no Performance section in this issue, so no PTR line is
    printed). Calibration: 22 rows."""
    assert len(ptr_records) >= 1
    assert len(ptr_records) >= 18


def test_parse_ptr_known_scheme(ptr_records):
    """Motilal Oswal Large Cap Fund prints
    ``Portfolio Turnover Ratio 0.61`` on April 30, 2026. Motilal stores
    PTR as a fraction directly (0.61 = 61%) — NO percent-to-fraction
    conversion in the adapter."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Motilal Oswal Large Cap Fund" in by_name
    rec = by_name["Motilal Oswal Large Cap Fund"]
    assert rec.ptr == pytest.approx(0.61, abs=1e-6)
    assert rec.source_amc == "motilal_oswal"


def test_parse_ptr_arbitrage_high_turnover(ptr_records):
    """Arbitrage schemes legitimately show very high turnover. Motilal
    Oswal Arbitrage Fund prints 11.06 (i.e. 1106% / 11.06x fraction) —
    guards against accidental clipping when value goes double-digit."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Motilal Oswal Arbitrage Fund" in by_name
    assert by_name["Motilal Oswal Arbitrage Fund"].ptr == pytest.approx(
        11.06, abs=1e-6
    )


def test_parse_ptr_values_positive(ptr_records):
    """Fail-fast: zero / NaN PTR must be dropped at the adapter."""
    for r in ptr_records:
        assert r.ptr > 0, r
        # Arbitrage strategies routinely hit 10-15x; Motilal's April
        # 2026 book tops out at 11.06 so 25 is a comfortable ceiling.
        assert r.ptr < 25, r
