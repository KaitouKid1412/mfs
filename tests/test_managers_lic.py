"""Tests for the LIC Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/lic/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise PTR / AUM extraction plus URL
construction.

Notes on the unusual layout: LIC's scheme detail pages do NOT print the
scheme name in the text layer (the banner is drawn as a non-text
element). The adapter resolves scheme names from the TOC on page 3 and
maps each per-scheme detail page back to its title. The tests therefore
also exercise the TOC parser directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.lic import (
    LicAdapter,
    _build_toc_map,
    _day_with_ordinal,
    _fiscal_year_dir,
    _publish_ym,
)

PDF = Path(__file__).parent.parent / "data" / "raw" / "factsheets" / "lic" / "2026-04.pdf"


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_april_2026():
    """LIC's CMS path encodes fiscal-year + publish-month directory and
    a filename with the data month-end day + ordinal + month name."""
    a = LicAdapter()
    assert a.build_url("2026-04") == (
        "https://www.licmf.com/assets/downloads/monthly_fact_sheet/"
        "2026-2027/05/lic-mf-factsheet-30th-April-2026.pdf"
    )


def test_build_url_march_uses_previous_fy():
    """March belongs to the PREVIOUS fiscal year (2025-2026), published
    in April (04/). Day = 31 (March has 31 days), ordinal = st."""
    a = LicAdapter()
    assert a.build_url("2026-03") == (
        "https://www.licmf.com/assets/downloads/monthly_fact_sheet/"
        "2025-2026/04/lic-mf-factsheet-31st-March-2026.pdf"
    )


def test_build_url_december_rolls_publish_year():
    """December 2026 publishes in Jan 2027 — fiscal year stays
    2026-2027 (Apr 2026 - Mar 2027); publish month wraps to 01."""
    a = LicAdapter()
    assert a.build_url("2026-12") == (
        "https://www.licmf.com/assets/downloads/monthly_fact_sheet/"
        "2026-2027/01/lic-mf-factsheet-31st-December-2026.pdf"
    )


# ---------------------------------------------------------------------------
# URL helpers (pure unit tests)
# ---------------------------------------------------------------------------


def test_publish_ym_helper():
    assert _publish_ym("2026-04") == (2026, 5)
    assert _publish_ym("2026-12") == (2027, 1)


def test_fiscal_year_dir():
    """April through December stay in the same FY; Jan-Mar belong to the
    previous FY."""
    assert _fiscal_year_dir("2026-04") == "2026-2027"
    assert _fiscal_year_dir("2026-12") == "2026-2027"
    assert _fiscal_year_dir("2026-03") == "2025-2026"
    assert _fiscal_year_dir("2026-01") == "2025-2026"


def test_day_with_ordinal():
    """The ordinal suffix follows English conventions; LIC always
    publishes month-end (last day of the data month)."""
    assert _day_with_ordinal(4, 2026) == "30th"    # April 30
    assert _day_with_ordinal(3, 2026) == "31st"    # March 31
    assert _day_with_ordinal(2, 2026) == "28th"    # Feb 28 (non-leap)
    assert _day_with_ordinal(2, 2024) == "29th"    # Feb 29 (leap year)


# ---------------------------------------------------------------------------
# TOC parser (PDF-backed but cheap — opens page 3 only)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def toc_map():
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached LIC PDF not found at {PDF}")
    import pdfplumber
    import logging
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    with pdfplumber.open(PDF) as pdf:
        return _build_toc_map(pdf)


def test_toc_map_has_expected_count(toc_map):
    """The April-2026 issue lists 43 scheme detail pages (sr 6-48)."""
    assert len(toc_map) >= 40


def test_toc_map_known_entries(toc_map):
    """Spot-check a handful of canonical mappings."""
    assert toc_map.get(11) == "LIC MF Large Cap Fund"
    assert toc_map.get(13) == "LIC MF Flexi Cap Fund"
    assert toc_map.get(26) == "LIC MF ELSS Tax Saver"
    assert toc_map.get(52) == "LIC MF Gold ETF"


def test_toc_map_strips_leaked_dot_leaders(toc_map):
    """Two 2026-04 TOC entries have abbreviated dot-leader runs
    (``Aggressive Hybrid Fund .....`` / ``Balanced Advantage Fund .``)
    that leak into the name capture group. The parser must strip the
    trailing dots before yielding the clean scheme name."""
    assert toc_map.get(27) == "LIC MF Aggressive Hybrid Fund"
    assert toc_map.get(28) == "LIC MF Balanced Advantage Fund"


# ---------------------------------------------------------------------------
# PTR / AUM extraction against the cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> LicAdapter:
    return LicAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: LicAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached LIC PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


# ----- PTR --------------------------------------------------------------


def test_parse_ptr_yields_records(ptr_records):
    """LIC publishes PTR for equity / hybrid / sector / select ETF
    schemes — 23 rows on the April-2026 PDF. Brief target: >= 15."""
    assert len(ptr_records) >= 15


def test_parse_ptr_known_scheme_large_cap(ptr_records):
    """LIC MF Large Cap Fund prints ``Annual Portfolio Turnover Ratio:
    0.50 times`` on page 11 of the April-2026 PDF."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "LIC MF Large Cap Fund" in by_name
    rec = by_name["LIC MF Large Cap Fund"]
    assert rec.ptr == pytest.approx(0.50, abs=1e-6)
    assert rec.source_amc == "lic"


def test_parse_ptr_known_scheme_value(ptr_records):
    """LIC MF Value Fund prints PTR 1.92 (high turnover, page 18) —
    exercises a >1.0 fraction."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "LIC MF Value Fund" in by_name
    assert by_name["LIC MF Value Fund"].ptr == pytest.approx(1.92, abs=1e-6)


def test_parse_ptr_hybrid_equity_label(ptr_records):
    """The Aggressive Hybrid / Balanced Advantage pages use the
    ``Annual Equity Portfolio Turnover Ratio`` variant with the extra
    ``Equity`` word — the regex must accept that form."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "LIC MF Aggressive Hybrid Fund" in by_name
    assert by_name["LIC MF Aggressive Hybrid Fund"].ptr == pytest.approx(
        0.44, abs=1e-6
    )


def test_parse_ptr_drops_na_sentinel(ptr_records):
    """LIC MF Consumption Fund and Technology Fund are < 1 year old and
    print ``Annual Portfolio Turnover Ratio: NA``. They must NOT appear
    in the output (fail-fast invariant — no fabricated values)."""
    names = {r.scheme_name_printed for r in ptr_records}
    assert "LIC MF Consumption Fund" not in names
    assert "LIC MF Technology Fund" not in names


def test_parse_ptr_values_are_fraction(ptr_records):
    """LIC prints PTR as a fraction ('X times'). Storage is also
    fraction, so no scaling. Guard against an accidental ×100 percent
    encoding."""
    for r in ptr_records:
        assert r.ptr > 0, r
        assert r.ptr < 100, r


# ----- AUM --------------------------------------------------------------


