"""Tests for the Invesco Mutual Fund (India) factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/invesco/2026-04.pdf``. Tests run entirely offline —
they read the bundled PDF and exercise PTR extraction plus URL
construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.invesco import (
    InvescoAdapter,
    _scheme_name_from_page,
)

PDF = (
    Path(__file__).parent.parent
    / "data"
    / "raw"
    / "factsheets"
    / "invesco"
    / "2026-04.pdf"
)


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_april_2026():
    """Invesco's canonical resolver is the literature API (the per-month
    PDF filenames get rewritten with a UUID suffix once they roll off
    the top spot, so a direct PDF URL is not stable). ``build_url``
    therefore returns the API endpoint, which ``fetch`` consults to
    locate the data-month ``DocumentUrl``."""
    a = InvescoAdapter()
    assert a.build_url("2026-04") == (
        "https://www.invescomutualfund.com/api/RequestForLiterature?year=2026"
    )


def test_build_url_year_only():
    """The literature API takes a year-only argument and returns every
    month's entry in one shot, so the URL doesn't depend on the
    data month within a year."""
    a = InvescoAdapter()
    assert a.build_url("2025-12") == (
        "https://www.invescomutualfund.com/api/RequestForLiterature?year=2025"
    )


# ---------------------------------------------------------------------------
# Scheme-name detection (pure unit tests — no PDF required)
# ---------------------------------------------------------------------------


def test_scheme_name_equity():
    """Most Invesco scheme pages begin ``Invesco India <Name>``."""
    text = (
        "Invesco India ELSS Tax Saver Fund\n"
        "(An open ended equity linked savings scheme...)"
    )
    assert _scheme_name_from_page(text) == "Invesco India ELSS Tax Saver Fund"


def test_scheme_name_cross_border_fof():
    """The cross-border FoF pages print ``Invesco India - Invesco ...``
    — a hyphen surrounded by spaces, not a hard break in the prefix."""
    text = (
        "Invesco India - Invesco Pan European Equity Fund of Fund\n"
        "(An open ended fund of fund scheme...)"
    )
    assert (
        _scheme_name_from_page(text)
        == "Invesco India - Invesco Pan European Equity Fund of Fund"
    )


def test_scheme_name_etf():
    text = "Invesco India NIFTY 50 Exchange Traded Fund\n..."
    assert (
        _scheme_name_from_page(text)
        == "Invesco India NIFTY 50 Exchange Traded Fund"
    )


def test_scheme_name_rejects_cover():
    """The cover and market-commentary pages (pp 1-3) don't begin with
    the Invesco India prefix."""
    text = "Strengthen your portfolio\nApril 2026\n..."
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_performance_table():
    """Lumpsum / SIP performance pages (pp 47-62) begin with the table
    title, not a scheme name."""
    text = "Lumpsum Performance - Regular Plan\n..."
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_empty():
    assert _scheme_name_from_page("") is None
    assert _scheme_name_from_page("\n\n") is None


# ---------------------------------------------------------------------------
# PTR against cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> InvescoAdapter:
    return InvescoAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: InvescoAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Invesco PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


def test_parse_ptr_yields_records(ptr_records):
    """April 2026 PDF carries PTR for ~20 schemes (equity + hybrid +
    arbitrage). Pure-debt / FOF / ETF / sectoral-launch schemes
    (Business Cycle, Consumption, Multi Asset Allocation) legitimately
    don't print Portfolio Turnover Ratio. Calibration: 21 rows."""
    assert len(ptr_records) >= 1
    assert len(ptr_records) >= 15


def test_parse_ptr_known_scheme(ptr_records):
    """Invesco India ELSS Tax Saver Fund prints
    ``Portfolio Turnover Ratio (1 Year) 0.89`` on April 30, 2026.
    Invesco stores PTR as a fraction directly (0.89 = 89%) — NO
    percent-to-fraction conversion in the adapter."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Invesco India ELSS Tax Saver Fund" in by_name
    rec = by_name["Invesco India ELSS Tax Saver Fund"]
    assert rec.ptr == pytest.approx(0.89, abs=1e-6)
    assert rec.source_amc == "invesco"


def test_parse_ptr_arbitrage_high_turnover(ptr_records):
    """Arbitrage schemes legitimately show very high turnover. Invesco
    India Arbitrage Fund prints 16.74 (i.e. 1674% / 16.74x fraction) —
    guards against accidental clipping or under-windowing in the
    word-position search."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Invesco India Arbitrage Fund" in by_name
    assert by_name["Invesco India Arbitrage Fund"].ptr == pytest.approx(
        16.74, abs=1e-6
    )


def test_parse_ptr_values_positive(ptr_records):
    """Fail-fast: zero / NaN PTR must be dropped at the adapter."""
    for r in ptr_records:
        assert r.ptr > 0, r
        # Arbitrage strategies routinely hit 10-20x; the Invesco April
        # 2026 book tops out at 16.74 so 25 is a comfortable ceiling.
        assert r.ptr < 25, r
