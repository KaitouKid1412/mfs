"""Tests for the PGIM India Mutual Fund factsheet adapter.

Calibrated against the cached April 2026 combined factsheet at
``data/raw/factsheets/pgim_india/2026-04.pdf``.  Tests run entirely
offline — they read the bundled PDF and exercise PTR extraction
plus URL construction.

Notes on the layout: PGIM's scheme detail pages print the title as an
ALL-CAPS multi-line logo in the top-left band (no usable text-flow
extraction); the adapter resolves scheme names via word-position
extraction in that band.  PTR appears mid-page as a clean label
(``Portfolio Turnover: X.XX``).  Fund-of-Fund pages (p18-p20) and
pure-debt pages (p28-p35) omit PTR by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.managers.pgim_india import (
    PgimIndiaAdapter,
    _scheme_title_from_words,
)

PDF = (
    Path(__file__).parent.parent
    / "data"
    / "raw"
    / "factsheets"
    / "pgim_india"
    / "2026-04.pdf"
)


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_build_url_april_2026():
    """PGIM's CMS path embeds the data-month name + year, with spaces
    URL-encoded as ``%20``."""
    a = PgimIndiaAdapter()
    assert a.build_url("2026-04") == (
        "https://www.pgimindia.com/api/v1/brochure/about-us/image/"
        "Factsheet%20-%20April%202026.pdf"
    )


def test_build_url_march_2026():
    """March uses the same pattern, just with the prior month's name."""
    a = PgimIndiaAdapter()
    assert a.build_url("2026-03") == (
        "https://www.pgimindia.com/api/v1/brochure/about-us/image/"
        "Factsheet%20-%20March%202026.pdf"
    )


def test_build_url_january_uses_correct_month_name():
    """Single-digit month → ``January`` (Title Case)."""
    a = PgimIndiaAdapter()
    assert a.build_url("2026-01") == (
        "https://www.pgimindia.com/api/v1/brochure/about-us/image/"
        "Factsheet%20-%20January%202026.pdf"
    )


def test_build_url_december_unchanged_year():
    """December stays in the same year — no fiscal-year shenanigans
    here (unlike LIC, PGIM uses a flat per-month folder)."""
    a = PgimIndiaAdapter()
    assert a.build_url("2026-12") == (
        "https://www.pgimindia.com/api/v1/brochure/about-us/image/"
        "Factsheet%20-%20December%202026.pdf"
    )


# ---------------------------------------------------------------------------
# Scheme-title extraction (PDF-backed, but cheap)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _pdf():
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached PGIM India PDF not found at {PDF}")
    import logging

    import pdfplumber

    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    with pdfplumber.open(PDF) as pdf:
        yield pdf


def test_scheme_title_large_cap(_pdf):
    """p9: Large Cap Fund — single-row title."""
    title = _scheme_title_from_words(_pdf.pages[8])
    assert title == "PGIM INDIA LARGE CAP FUND"


def test_scheme_title_flexi_cap(_pdf):
    """p10: Flexi Cap Fund — flagship equity scheme."""
    title = _scheme_title_from_words(_pdf.pages[9])
    assert title == "PGIM INDIA FLEXI CAP FUND"


def test_scheme_title_large_and_mid_cap(_pdf):
    """p11: Large and Mid Cap Fund — title includes the word ``AND``."""
    title = _scheme_title_from_words(_pdf.pages[10])
    assert title == "PGIM INDIA LARGE AND MID CAP FUND"


def test_scheme_title_fof_with_continuation(_pdf):
    """p18: Emerging Markets Equity Fund of Fund — the title wraps into
    a second visual row carrying ``FUND OF FUND``; the parser must
    accept the continuation after the first FUND."""
    title = _scheme_title_from_words(_pdf.pages[17])
    assert title == "PGIM INDIA EMERGING MARKETS EQUITY FUND OF FUND"


def test_scheme_title_target_maturity_index(_pdf):
    """p35: CRISIL IBX Gilt Index - Apr 2028 Fund — title includes a
    digit (2028) which must be preserved."""
    title = _scheme_title_from_words(_pdf.pages[34])
    assert title == "PGIM INDIA CRISIL IBX GILT INDEX - APR 2028 FUND"


def test_scheme_title_strips_description_fragment(_pdf):
    """p31: Money Market Fund — without the strict FUND-stop logic, a
    leaked single-letter ``A`` from the ``A relatively low...``
    description bled into the title.  The strict gate must drop it."""
    title = _scheme_title_from_words(_pdf.pages[30])
    assert title == "PGIM INDIA MONEY MARKET FUND"


def test_scheme_title_returns_none_on_non_scheme_page(_pdf):
    """Cover (p1), TOC (p2), market review (p5) and back matter pages
    don't carry a PGIM logo + ALL CAPS title — the parser must return
    None so we don't yield spurious rows."""
    for idx in (0, 1, 4, 6, 7, 36, 37, 39):  # cover/TOC/review/SIP/glossary
        title = _scheme_title_from_words(_pdf.pages[idx])
        assert title is None, f"page {idx + 1} unexpectedly yielded {title!r}"


# ---------------------------------------------------------------------------
# PTR extraction
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> PgimIndiaAdapter:
    return PgimIndiaAdapter()


@pytest.fixture(scope="module")
def ptr_records(adapter: PgimIndiaAdapter):
    pytest.importorskip("pdfplumber")
    if not PDF.exists():
        pytest.skip(f"Cached PGIM India PDF not found at {PDF}")
    return list(adapter.parse_ptr(PDF, "2026-04"))


# ----- PTR --------------------------------------------------------------


def test_parse_ptr_yields_records(ptr_records):
    """PGIM publishes PTR for every equity / hybrid scheme — 14 rows on
    the April-2026 PDF.  Brief target: >= 9 (the 11 ranked equity
    schemes minus any that might legitimately omit the label)."""
    assert len(ptr_records) >= 9


def test_parse_ptr_known_scheme_large_cap(ptr_records):
    """PGIM India Large Cap Fund prints ``Portfolio Turnover: 0.30`` on
    page 9 of the April-2026 PDF.  Stored as fraction."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "PGIM INDIA LARGE CAP FUND" in by_name
    rec = by_name["PGIM INDIA LARGE CAP FUND"]
    assert rec.ptr == pytest.approx(0.30, abs=1e-6)
    assert rec.source_amc == "pgim_india"


def test_parse_ptr_known_scheme_flexi_cap(ptr_records):
    """Flexi Cap Fund — largest equity scheme by AUM (5,746.80 Cr).
    PTR = 0.43 on page 10."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "PGIM INDIA FLEXI CAP FUND" in by_name
    assert by_name["PGIM INDIA FLEXI CAP FUND"].ptr == pytest.approx(
        0.43, abs=1e-6
    )


def test_parse_ptr_arbitrage_high_turnover(ptr_records):
    """Arbitrage funds rotate derivatives near-daily; PGIM India
    Arbitrage Fund prints PTR = 5.85 (= 585%, real value).  This
    exercises the > 1.0 fraction path and confirms we don't truncate."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    assert "PGIM INDIA ARBITRAGE FUND" in by_name
    assert by_name["PGIM INDIA ARBITRAGE FUND"].ptr == pytest.approx(
        5.85, abs=1e-6
    )


def test_parse_ptr_hybrid_for_equity_scope(ptr_records):
    """Hybrid pages tag the PTR with a ``(For Equity)`` scope marker
    after the numeric value.  The regex must ignore the trailing
    annotation and capture only the number."""
    by_name = {r.scheme_name_printed: r for r in ptr_records}
    # Aggressive Hybrid Equity Fund (p21): "Portfolio Turnover: 0.23 (For Equity)"
    assert "PGIM INDIA AGGRESSIVE HYBRID EQUITY FUND" in by_name
    assert by_name["PGIM INDIA AGGRESSIVE HYBRID EQUITY FUND"].ptr == pytest.approx(
        0.23, abs=1e-6
    )


def test_parse_ptr_no_ptr_for_fof(ptr_records):
    """The three Fund-of-Fund schemes (Emerging Markets / Global Equity
    Opportunities / Global Select Real Estate Securities) do NOT print
    a PTR — they must be absent from the output (fail-fast: no
    fabricated zero rows)."""
    names = {r.scheme_name_printed for r in ptr_records}
    assert "PGIM INDIA EMERGING MARKETS EQUITY FUND OF FUND" not in names
    assert "PGIM INDIA GLOBAL EQUITY OPPORTUNITIES FUND OF FUND" not in names
    assert (
        "PGIM INDIA GLOBAL SELECT REAL ESTATE SECURITIES FUND OF FUND" not in names
    )


def test_parse_ptr_no_ptr_for_debt(ptr_records):
    """Pure debt schemes (Liquid / Overnight / Ultra Short / Money
    Market / Dynamic Bond / Corporate Bond / Gilt) and the target-
    maturity index fund don't print PTR — they must NOT appear."""
    names = {r.scheme_name_printed for r in ptr_records}
    for debt in (
        "PGIM INDIA LIQUID FUND",
        "PGIM INDIA OVERNIGHT FUND",
        "PGIM INDIA ULTRA SHORT DURATION FUND",
        "PGIM INDIA MONEY MARKET FUND",
        "PGIM INDIA DYNAMIC BOND FUND",
        "PGIM INDIA CORPORATE BOND FUND",
        "PGIM INDIA GILT FUND",
        "PGIM INDIA CRISIL IBX GILT INDEX - APR 2028 FUND",
    ):
        assert debt not in names, f"debt scheme {debt} unexpectedly produced a PTR"


def test_parse_ptr_values_are_positive(ptr_records):
    """Fail-fast invariant: PTR <= 0 / NaN must never be yielded."""
    for r in ptr_records:
        assert r.ptr > 0, r
        assert r.ptr == r.ptr  # NaN guard
