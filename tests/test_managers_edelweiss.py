"""Tests for the Edelweiss Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/edelweiss/2026-04.pdf`` (the file Edelweiss
publishes as ``Edelweiss_Factsheet_May_-_2026_<upload_ts>.pdf`` — the
publish-month is May 2026 but the portfolio data is as of April 30,
2026). Tests run entirely offline; the only network would be in
``build_url``/``fetch`` which we don't exercise here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.edelweiss import EdelweissAdapter, _scheme_name_from_page

PDF = (
    Path(__file__).parent.parent
    / "data"
    / "raw"
    / "factsheets"
    / "edelweiss"
    / "2026-04.pdf"
)


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_returns_listing_page():
    """Edelweiss's per-month PDF filename embeds an upload timestamp that
    cannot be predicted ahead of fetch, so build_url returns the stable
    CMS listing URL the user would follow to find the file. The actual
    PDF is resolved at fetch time via the encrypted CMS API."""
    a = EdelweissAdapter()
    assert (
        a.build_url("2026-04")
        == "https://www.edelweissmf.com/downloads/factsheets"
    )


def test_build_url_independent_of_month():
    a = EdelweissAdapter()
    assert a.build_url("2026-04") == a.build_url("2026-12") == a.build_url("2027-01")


def test_amc_slug_matches_scheme_master_amc_code():
    """No alias entry needed — adapter slug == scheme_master.amc_code."""
    assert EdelweissAdapter.amc_slug == "edelweiss"


# ---------------------------------------------------------------------------
# Scheme-name extraction (pure unit tests)
# ---------------------------------------------------------------------------


def test_scheme_name_simple_one_line():
    """Active equity pages put the full name on line 1."""
    text = (
        "Edelweiss Large Cap Fund\n"
        "An open ended equity scheme predominantly investing in large cap\n"
        "stocks\n"
    )
    assert _scheme_name_from_page(text) == "Edelweiss Large Cap Fund"


def test_scheme_name_wraps_to_line_two():
    """Long names wrap; line 1 lacks a Fund/ETF terminator and line 2
    supplies it. The parser must join the two."""
    text = (
        "Edelweiss Large & Mid Cap\n"
        "Fund\n"
        "An open ended equity scheme investing in both large cap and mid\n"
    )
    assert _scheme_name_from_page(text) == "Edelweiss Large & Mid Cap Fund"


def test_scheme_name_wrap_with_qualifier_word():
    """``Multi Asset`` wraps to ``Allocation Fund`` — the join must
    extend up to the first Fund token, not stop at the bare word."""
    text = (
        "Edelweiss Multi Asset\n"
        "Allocation Fund\n"
        "An open-ended scheme investing in Equity, Debt, Commodities...\n"
    )
    assert _scheme_name_from_page(text) == "Edelweiss Multi Asset Allocation Fund"


def test_scheme_name_wrap_for_fund_of_fund():
    """FoF names: ``Income Plus Arbitrage`` + ``Omni Fund of Funds``."""
    text = (
        "Edelweiss Income Plus Arbitrage\n"
        "Omni Fund of Funds\n"
        "An open-ended fund of funds scheme investing in units of...\n"
    )
    assert (
        _scheme_name_from_page(text)
        == "Edelweiss Income Plus Arbitrage Omni Fund of Funds"
    )


def test_scheme_name_rejects_cover_page():
    text = "Monthly Factsheet\nApril 2026\nEdelweiss Mutual Fund\n"
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_toc_page():
    text = "Index\nSr No Particulars Page\nEdelweiss Large Cap Fund 6\n"
    assert _scheme_name_from_page(text) is None


def test_scheme_name_rejects_empty():
    assert _scheme_name_from_page("") is None
    assert _scheme_name_from_page("\n\n") is None


# ---------------------------------------------------------------------------
# PTR extraction against the cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> EdelweissAdapter:
    return EdelweissAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: EdelweissAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Edelweiss PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


# PTR -----------------------------------------------------------------------


def test_parse_ptr_yields_records(ptr_records):
    """Edelweiss prints PTR on active equity + most passive index funds
    (it omits it on pure debt / arbitrage / ETF / FOF pages). The April
    2026 PDF carries at least a dozen such schemes; minimum bar is 1."""
    assert len(ptr_records) >= 1


def test_parse_ptr_large_cap_known_value(ptr_records):
    """Edelweiss Large Cap Fund April 2026 PTR is 0.77 (printed as
    "Portfolio Turnover Ratio3 : 0.77"). Already a fraction — no
    percent-to-fraction normalization."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Edelweiss Large Cap Fund" in by_name
    rec = by_name["Edelweiss Large Cap Fund"]
    assert rec.ptr == pytest.approx(0.77, abs=1e-6)
    assert rec.source_amc == "edelweiss"


def test_parse_ptr_high_turnover_business_cycle(ptr_records):
    """High-turnover thematic schemes legitimately exceed 1.0. Business
    Cycle Fund prints 1.86 — guards against accidental column-truncation
    or % encoding error."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Edelweiss Business Cycle Fund" in by_name
    assert by_name["Edelweiss Business Cycle Fund"].ptr == pytest.approx(
        1.86, abs=1e-6
    )


def test_parse_ptr_values_are_fraction(ptr_records):
    """Edelweiss prints PTR as a fraction; values like 1.86 (Business
    Cycle Fund) are legitimate. Guard against accidental ×100 percent
    encoding bug."""
    for r in ptr_records:
        assert r.ptr > 0, r
        assert r.ptr < 100, r


