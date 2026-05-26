"""Tests for the Sundaram Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 consolidated factsheet at
``data/raw/factsheets/sundaram/2026-04.pdf``. Tests run entirely
offline — they read the bundled PDF and exercise PTR extraction plus
URL construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.sundaram import (
    SundaramAdapter,
    _scheme_name_from_page,
)

PDF = (
    Path(__file__).parent.parent
    / "data"
    / "raw"
    / "factsheets"
    / "sundaram"
    / "2026-04.pdf"
)


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_april_2026():
    """Sundaram's canonical resolver is the consolidated-factsheet archive
    ASHX endpoint. The PDF filename embeds an upload timestamp
    (``Consolidated_Factsheet_<M>_<YYYY>_<DDMMYY>_<HHMMSS>.pdf``), so a
    direct PDF URL is non-deterministic month over month. ``build_url``
    returns the stable archive endpoint; ``fetch`` does the POST-and-
    resolve dance against it."""
    a = SundaramAdapter()
    assert a.build_url("2026-04") == (
        "https://www.sundarammutual.com/ajax/"
        "Modules_Forms_Downloads_Fundwise_Factsheet,App_Web_jmhjkqpp.ashx"
        "?_method=DownloadArchive&_session=no"
    )


def test_build_url_year_independent():
    """The endpoint is the same for every data month — the month value
    is passed in the POST body, not the URL — so the URL doesn't vary
    across calendar months or years."""
    a = SundaramAdapter()
    assert a.build_url("2025-12") == a.build_url("2026-04")
    assert a.build_url("2026-01") == a.build_url("2027-07")


# ---------------------------------------------------------------------------
# Scheme-name detection (pure unit tests — no PDF required)
# ---------------------------------------------------------------------------


def test_scheme_name_equity():
    """Every active Sundaram scheme page starts with ``Sundaram <Name>
    Fund``."""
    text = (
        "Sundaram Large Cap Fund\n"
        "An open-ended equity scheme predominantly investing in large cap stocks\n"
        "FUND FEATURES PORTFOLIO\n"
    )
    assert _scheme_name_from_page(text) == "Sundaram Large Cap Fund"


def test_scheme_name_with_special_chars():
    """A handful of Sundaram funds carry punctuation (hyphen, ampersand)
    in the canonical name."""
    text = "Sundaram Multi-Factor Fund\n..."
    assert _scheme_name_from_page(text) == "Sundaram Multi-Factor Fund"
    text2 = "Sundaram Banking & PSU Fund\n..."
    assert _scheme_name_from_page(text2) == "Sundaram Banking & PSU Fund"


def test_scheme_name_fof():
    """FoF schemes carry a trailing ``FoF`` token rather than ``Fund``."""
    text = "Sundaram Global Brand Theme - Equity Active FoF\n..."
    assert (
        _scheme_name_from_page(text)
        == "Sundaram Global Brand Theme - Equity Active FoF"
    )


def test_scheme_name_rejects_cover_pages():
    """The cover (page 1), index (page 2), and outlook (pages 3-5) of
    the consolidated factsheet do NOT start with ``Sundaram ``."""
    assert _scheme_name_from_page("Fact Sheet for April 2026\n...") is None
    assert _scheme_name_from_page("Index\n...") is None
    assert _scheme_name_from_page("Outlook\n...") is None


def test_scheme_name_rejects_back_matter():
    """IDCW history / disclosures / track record / riskometer / fund
    managers / annexure pages don't carry the Sundaram prefix on line 1."""
    for first_line in (
        "Annexure",
        "IDCW History - Equity & Balanced Funds (Latest Three)",
        "Disclosures",
        "Performance Track Record Equity Funds",
        "Riskometer",
        "Fund Managers",
    ):
        assert _scheme_name_from_page(f"{first_line}\n...") is None


def test_scheme_name_rejects_empty():
    assert _scheme_name_from_page("") is None
    assert _scheme_name_from_page("\n\n") is None


# ---------------------------------------------------------------------------
# PTR against cached PDF
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> SundaramAdapter:
    return SundaramAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: SundaramAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached Sundaram PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


def test_parse_ptr_yields_records(ptr_records):
    """April 2026 PDF carries PTR (``Turnover Ratio``) on every equity /
    hybrid / arbitrage / index scheme page. Pure-debt / FoF / Multi
    Asset Allocation pages legitimately omit the block. Calibration:
    21 rows."""
    assert len(ptr_records) >= 1
    assert len(ptr_records) >= 15


def test_parse_ptr_known_scheme(ptr_records):
    """Sundaram Large Cap Fund prints ``Turnover Ratio 31.4`` on
    April 30, 2026. Sundaram stores PTR as a **percent**, so the
    adapter divides by 100 to keep the fraction storage convention
    (31.4% -> 0.314)."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Sundaram Large Cap Fund" in by_name
    rec = by_name["Sundaram Large Cap Fund"]
    assert rec.ptr == pytest.approx(0.314, abs=1e-6)
    assert rec.source_amc == "sundaram"


def test_parse_ptr_arbitrage_high_turnover(ptr_records):
    """Arbitrage schemes legitimately churn close to 100%. Sundaram
    Arbitrage Fund prints ``Turnover Ratio 99.1`` (i.e. 0.991 fraction)
    — guards against accidental clipping or under-windowing."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Sundaram Arbitrage Fund" in by_name
    assert by_name["Sundaram Arbitrage Fund"].ptr == pytest.approx(
        0.991, abs=1e-6
    )


def test_parse_ptr_handles_column_bleed(ptr_records):
    """On a number of equity scheme pages the right-column portfolio
    table fuses onto the same visual line as ``Turnover Ratio`` (e.g.
    ``Turnover Ratio 37.2 Berger Paints Ltd 0.4 Telecom - Services 2.1``
    on the Sundaram Mid Cap Fund page). The regex anchors on the
    numeric token immediately after the label so the trailing portfolio
    fragments don't interfere — Mid Cap Fund must extract 37.2 -> 0.372."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "Sundaram Mid Cap Fund" in by_name
    assert by_name["Sundaram Mid Cap Fund"].ptr == pytest.approx(0.372, abs=1e-6)


def test_parse_ptr_values_positive(ptr_records):
    """Fail-fast: zero / NaN PTR must be dropped at the adapter."""
    for r in ptr_records:
        assert r.ptr > 0, r
        # All Sundaram schemes (including arbitrage at 99.1%) print PTR
        # below 100% in the April 2026 book; 10x is a comfortable ceiling.
        assert r.ptr < 10, r
