"""Tests for the Groww Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/groww/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise PTR / AUM extraction plus URL
construction.

Groww is the former Indiabulls Mutual Fund (rebranded after the 2023
acquisition); these tests use the current "Groww " branded scheme
names that appear in the 2026 factsheet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.groww import (
    GrowwAdapter,
    _fiscal_year_folder,
    _is_snapshot_page,
)

PDF = (
    Path(__file__).parent.parent
    / "data"
    / "raw"
    / "factsheets"
    / "groww"
    / "2026-04.pdf"
)


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_april_2026():
    """April 2026 is in Indian fiscal year 2026-2027 and the PDF lives
    under the spaced ``2026 - 2027`` folder on the Groww CDN. The
    filename uses the full month name and four-digit year."""
    a = GrowwAdapter()
    assert a.build_url("2026-04") == (
        "https://assets-netstorage.growwmf.in/compliance_docs/Downloads/"
        "Fact%20Sheet/2026%20-%202027/"
        "Monthly%20Factsheet%20-%20April%202026.pdf"
    )


def test_build_url_march_2026_prev_fy():
    """March falls in the *previous* Indian fiscal year (FY 2025-26).
    March 2026 factsheet lives under ``2025 - 2026``."""
    a = GrowwAdapter()
    url = a.build_url("2026-03")
    assert "2025%20-%202026" in url
    assert "March%202026.pdf" in url


def test_build_url_december_2026_same_fy():
    """December 2026 is still in FY 2026-27 (Apr 2026 - Mar 2027)."""
    a = GrowwAdapter()
    url = a.build_url("2026-12")
    assert "2026%20-%202027" in url
    assert "December%202026.pdf" in url


def test_build_url_january_2027_same_fy():
    """January 2027 still falls in FY 2026-27 (it's before April 2027)."""
    a = GrowwAdapter()
    url = a.build_url("2027-01")
    assert "2026%20-%202027" in url
    assert "January%202027.pdf" in url


# ---------------------------------------------------------------------------
# Fiscal-year folder math (pure unit test)
# ---------------------------------------------------------------------------


def test_fiscal_year_folder_april():
    assert _fiscal_year_folder(2026, 4) == "2026 - 2027"


def test_fiscal_year_folder_march():
    assert _fiscal_year_folder(2026, 3) == "2025 - 2026"


def test_fiscal_year_folder_january():
    assert _fiscal_year_folder(2026, 1) == "2025 - 2026"


def test_fiscal_year_folder_december():
    assert _fiscal_year_folder(2026, 12) == "2026 - 2027"


# ---------------------------------------------------------------------------
# Snapshot-page discriminator (pure unit test)
# ---------------------------------------------------------------------------


def test_is_snapshot_page_equity():
    assert _is_snapshot_page("Snapshot of Equity Fund\nScheme Name ...")


def test_is_snapshot_page_debt():
    assert _is_snapshot_page("Snapshot Of Debt Funds\n...")


def test_is_snapshot_page_rejects_detail():
    """Per-scheme detail pages must NOT be treated as snapshots; the
    Silver-FOF detail page (p98) contains "investing in units of
    Groww Silver ETF" in body text, which would otherwise trip the
    Groww-anchor heuristic and emit a bogus row."""
    detail_text = (
        "GROWW Silver ETF FOF\n"
        "(An open-ended fund of fund scheme investing in units of Groww Silver ETF)\n"
        "..."
    )
    assert not _is_snapshot_page(detail_text)


def test_is_snapshot_page_rejects_cover():
    assert not _is_snapshot_page("April 2026\nGroww Mutual Fund Factsheet")


def test_is_snapshot_page_rejects_empty():
    assert not _is_snapshot_page("")
    assert not _is_snapshot_page("\n\n")


# ---------------------------------------------------------------------------
# PTR / AUM against cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> GrowwAdapter:
    return GrowwAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: GrowwAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Groww PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


def test_parse_ptr_yields_records(ptr_records):
    """April 2026 PDF carries Portfolio Turnover on every snapshot
    column except the 6 debt-fund snapshot columns (pages 32-33). One
    ETF (Nifty 1D Rate Liquid) prints PTR = 0.00 and is dropped by the
    fail-fast invariant, leaving 50 emitted PTR rows."""
    assert len(ptr_records) >= 1
    assert len(ptr_records) >= 45


def test_parse_ptr_known_scheme(ptr_records):
    """Groww Largecap Fund prints Portfolio Turnover = 0.98 on the Equity
    snapshot page (p21). Groww stores PTR as a fraction directly (0.98 = 98%)
    — NO percent-to-fraction conversion in the adapter. The adapter emits the
    scheme_master-aligned spelling "Largecap" (one word) so the fuzzy matcher
    resolves it to "Groww Largecap Fund (formerly Indiabulls Blue Chip Fund)";
    the factsheet's two-word "Large Cap" wouldn't token-match that."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Groww Largecap Fund" in by_name
    rec = by_name["Groww Largecap Fund"]
    assert rec.ptr == pytest.approx(0.98, abs=1e-6)
    assert rec.source_amc == "groww"


def test_parse_ptr_high_turnover_arbitrage_or_thematic(ptr_records):
    """Groww Multi Asset Allocation Fund prints PTR = 3.07 (i.e. 307%
    fraction) on the Hybrid snapshot page — a useful guard against
    accidental clipping or under-windowing of the value-row scan."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Groww Multi Asset Allocation Fund" in by_name
    assert by_name["Groww Multi Asset Allocation Fund"].ptr == pytest.approx(
        3.07, abs=1e-6
    )


def test_parse_ptr_values_positive(ptr_records):
    """Fail-fast: zero / NaN / negative PTR must be dropped at the
    adapter. The 6 debt-fund snapshot columns and the Nifty 1D Rate
    Liquid ETF (PTR=0.00) are correctly absent from this list."""
    for r in ptr_records:
        assert r.ptr > 0, r
        # Sanity ceiling: even high-churn arbitrage / hybrid books cap
        # at single-digit fractions in Groww's 2026 universe.
        assert r.ptr < 20, r


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Negative test: stray Groww anchors on detail pages must not bleed in
# ---------------------------------------------------------------------------


