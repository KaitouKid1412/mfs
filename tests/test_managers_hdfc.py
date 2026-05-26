"""Tests for the HDFC Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/hdfc/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise scheme-name + PTR / AUM
extraction plus URL construction. Coverage targets follow the Phase 3
refresh: ≥22 PTR records and ≥40 AUM records on this snapshot, with
the word-position fallback recovering the ~5 PTR / ~6 AUM rows that
``extract_text`` mangles due to column-bleed between the QUANTITATIVE
DATA panel and the right-hand portfolio table.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.hdfc import HdfcAdapter

PDF = (
    Path(__file__).parent.parent
    / "data"
    / "raw"
    / "factsheets"
    / "hdfc"
    / "2026-04.pdf"
)


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_april_2026_matches_downloaded_pdf():
    """build_url for the calibrated 2026-04 data month must equal the
    URL we actually downloaded from. HDFC's filename embeds the DATA
    month name + DATA year, but the path segment is the PUBLISH month
    (data + 1)."""
    a = HdfcAdapter()
    assert a.build_url("2026-04") == (
        "https://files.hdfcfund.com/s3fs-public/2026-05/"
        "HDFC%20MF%20Factsheet%20-%20April%202026.pdf"
    )


def test_build_url_rolls_year_at_december():
    """Publish month rolls to the next January when data month is
    December — confirm both the path segment and the data-year inside
    the filename track correctly."""
    a = HdfcAdapter()
    assert a.build_url("2025-12") == (
        "https://files.hdfcfund.com/s3fs-public/2026-01/"
        "HDFC%20MF%20Factsheet%20-%20December%202025.pdf"
    )


def test_amc_slug_matches_scheme_master_amc_code():
    """No alias entry needed — adapter slug == scheme_master.amc_code."""
    assert HdfcAdapter.amc_slug == "hdfc"


# ---------------------------------------------------------------------------
# PTR / AUM extraction against the cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> HdfcAdapter:
    return HdfcAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: HdfcAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached HDFC PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


# PTR ------------------------------------------------------------------------


def test_parse_ptr_yields_at_least_22_records(ptr_records):
    """Refresh acceptance bar: at least 22 PTR records on the 2026-04
    factsheet. The pre-refresh text-only parser yielded 24 raw records;
    the word-position fallback adds ~5 more on column-bleed pages (Large
    Cap, Transportation, Pharma, Housing, Infrastructure)."""
    assert len(ptr_records) >= 22, (
        f"PTR coverage dropped to {len(ptr_records)} — refresh target is 22"
    )


def test_parse_ptr_flexi_cap_known_value(ptr_records):
    """HDFC Flexi Cap Fund April 2026: 'Equity Turnover 9.14%' → 0.0914.
    Pins the percent→fraction conversion against accidental ×100 bugs."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "HDFC Flexi Cap Fund" in by_name
    rec = by_name["HDFC Flexi Cap Fund"]
    assert rec.ptr == pytest.approx(0.0914, abs=1e-6)
    assert rec.source_amc == "hdfc"


def test_parse_ptr_recovers_column_bleed_schemes(ptr_records):
    """These five schemes hit the column-bleed pathway: extract_text
    mangles the PTR line, so only the word-position fallback can
    recover them. If any disappear from coverage, the fallback has
    regressed."""
    names = {r.scheme_name_printed for r in ptr_records}
    for n in (
        "HDFC Large Cap Fund",
        "HDFC Transportation and Logistics Fund",
        "HDFC Pharma and Healthcare Fund",
        "HDFC Housing Opportunities Fund",
        "HDFC Infrastructure Fund",
    ):
        assert n in names, f"word-position PTR fallback regressed: missing {n!r}"


def test_parse_ptr_values_are_positive_fraction(ptr_records):
    """Fail-fast invariant: all PTR values are positive fractions.
    Arbitrage funds legitimately report >1 (e.g. HDFC Arbitrage = 2.07),
    so we cap at 10 rather than 1 to avoid false alarms."""
    for r in ptr_records:
        assert r.ptr > 0, r
        assert r.ptr < 10, r


# AUM ------------------------------------------------------------------------


