"""Tests for the SBI monthly portfolio Excel holdings adapter (Phase 3.C).

SBI's portfolios page is a Sitefinity SPA that loads its scheme list via an
AJAX POST (`POST /ajaxcall/CMS/GetSchemePortfolioSheets`). The Excels live
on the same host. Column layout differs from HDFC — ISIN sits in col D
(not col B), section banners sit in col C (not col B).

Fixture: tests/fixtures/holdings/sbi/sbi_large_cap_april_2026.xlsx
(SBI Large Cap Fund, April 2026 data — 51 holdings, 98% total weight).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mfs.ingest.holdings.sbi import (
    SbiHoldingsAdapter,
    _ANCHOR_RE,
    _FILENAME_RE,
    _classify_section,
    _data_month_name,
    _decode_url_path,
    _is_isin,
    _strip_url_query,
)

FIXTURE_XLSX = Path(__file__).parent / "fixtures/holdings/sbi/sbi_large_cap_april_2026.xlsx"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_data_month_name_april_2026():
    assert _data_month_name("2026-04") == ("April", "2026")


def test_data_month_name_december_does_not_roll():
    """Unlike HDFC's publish-month convention, SBI keys the API by the
    DATA month — December data goes to December listing, no year roll."""
    assert _data_month_name("2025-12") == ("December", "2025")


def test_strip_url_query_drops_sfvrsn_cache_buster():
    raw = (
        "https://www.sbimf.com/docs/default-source/scheme-portfolios/"
        "sbi-large-cap-fund-monthly-portfolio---april-2026.xlsx?sfvrsn=abc_2"
    )
    bare = _strip_url_query(raw)
    assert bare.endswith(".xlsx")
    assert "?" not in bare


def test_is_isin_accepts_valid():
    assert _is_isin("INE040A01034") is True


def test_is_isin_rejects_banner_strings():
    assert _is_isin("EQUITY & EQUITY RELATED") is False
    assert _is_isin("Equity Shares") is False
    assert _is_isin("") is False


@pytest.mark.parametrize("label,expected", [
    ("EQUITY & EQUITY RELATED", "Equity"),
    ("Equity Shares", "Equity"),
    # Sub-banner — doesn't carry "equity" keyword, classifier returns None
    # and the parser keeps the previously-set section (already Equity).
    ("a) Listed/awaiting listing on Stock Exchanges", None),
    ("DEBT INSTRUMENTS", "Debt"),
    ("Money Market Instruments", "Debt"),
    ("TREPS", "Debt"),
    ("Government Securities", "Debt"),
    ("Commercial Paper", "Debt"),
    ("REITs & InvITs", "REIT/InvIT"),
    ("Net Receivables/(Payables)", "Cash"),
    ("Cash & Cash Equivalents", "Cash"),
    ("Foo Bar", None),
    ("", None),
])
def test_classify_section(label, expected):
    assert _classify_section(label) == expected


def test_decode_url_path_unescapes_pct_encoding():
    raw = "sbi-large-%26-midcap-fund-monthly-portfolio---april-2026.xlsx"
    assert _decode_url_path(raw) == "sbi-large-&-midcap-fund-monthly-portfolio---april-2026.xlsx"


# ---------------------------------------------------------------------------
# URL + filename regex contract
# ---------------------------------------------------------------------------


def test_anchor_re_matches_real_listing_anchor():
    snippet = (
        '<td><a href="https://www.sbimf.com/docs/default-source/'
        'scheme-portfolios/sbi-large-cap-fund-monthly-portfolio---april-2026'
        '.xlsx?sfvrsn=abc_2" target="_blank">SBI Large Cap Fund MONTHLY '
        'PORTFOLIO - APRIL 2026</a><a href="..." class="primary-button" '
        'download="true">Download</a></td>'
    )
    m = _ANCHOR_RE.search(snippet)
    assert m is not None
    assert ".xlsx" in m.group("url")
    assert "Large Cap" in m.group("label")


def test_filename_re_accepts_per_scheme_file():
    m = _FILENAME_RE.match("sbi-large-cap-fund-monthly-portfolio---april-2026.xlsx")
    assert m is not None
    assert m.group("slug") == "sbi-large-cap-fund"
    assert m.group("month").lower() == "april"
    assert m.group("year") == "2026"


def test_filename_re_accepts_magnum_prefix():
    """SBI has a legacy 'Magnum' brand for some schemes — the slug
    prefix can be 'magnum-' as well as 'sbi-'."""
    m = _FILENAME_RE.match("magnum-children-benefit-fund-monthly-portfolio---april-2026.xlsx")
    assert m is not None
    assert m.group("slug") == "magnum-children-benefit-fund"


def test_filename_re_rejects_aggregate_file():
    """The 'all-schemes-monthly-portfolio---as-on-30th-april-2026.xlsx'
    aggregate file must NOT match — discover_scheme_urls would otherwise
    pick it up and the parser would choke on its multi-scheme layout."""
    aggregate = "all-schemes-monthly-portfolio---as-on-30th-april-2026.xlsx"
    assert _FILENAME_RE.match(aggregate) is None


# ---------------------------------------------------------------------------
# Integration: parse the saved Large Cap fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def large_cap_records():
    if not FIXTURE_XLSX.exists():
        pytest.skip(f"Fixture missing: {FIXTURE_XLSX}")
    adapter = SbiHoldingsAdapter()
    return list(adapter.parse_excel(FIXTURE_XLSX, "SBI Large Cap Fund", "2026-04"))


def test_large_cap_yields_many_rows(large_cap_records):
    assert len(large_cap_records) >= 50


def test_large_cap_every_record_has_valid_isin(large_cap_records):
    """The whole point of moving off factsheet PDFs."""
    for r in large_cap_records:
        assert r.isin, f"row missing ISIN: {r}"
        assert _is_isin(r.isin), f"malformed ISIN {r.isin!r}"


def test_large_cap_total_equity_weight_realistic(large_cap_records):
    eq = sum(r.weight_pct for r in large_cap_records if r.instrument_type == "Equity")
    assert eq >= 90.0, f"equity weight={eq:.2f} suspiciously low"


def test_large_cap_marquee_holdings_present(large_cap_records):
    """Anchor stocks for the parser — layout drift on these would fail loud."""
    by_isin = {r.isin: r for r in large_cap_records}
    must_have = {
        "INE040A01034": "HDFC Bank",
        "INE090A01021": "ICICI Bank",
        "INE002A01018": "Reliance Industries",
        "INE062A01020": "State Bank of India",
    }
    for isin, label in must_have.items():
        assert isin in by_isin, f"missing anchor {label} (ISIN {isin})"
        assert by_isin[isin].weight_pct > 0.5


def test_large_cap_no_duplicate_isins(large_cap_records):
    isins = [r.isin for r in large_cap_records]
    assert len(set(isins)) == len(isins)


def test_large_cap_source_amc_is_sbi(large_cap_records):
    assert all(r.source_amc == "sbi" for r in large_cap_records)


def test_large_cap_scheme_name_propagated(large_cap_records):
    assert all(r.scheme_name_printed == "SBI Large Cap Fund" for r in large_cap_records)


def test_large_cap_instrument_types_in_known_set(large_cap_records):
    types = {r.instrument_type for r in large_cap_records}
    assert "Equity" in types
    assert types.issubset({"Equity", "Debt", "REIT/InvIT", "Cash"})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_sbi_holdings_adapter_is_registered():
    from mfs.ingest.holdings._registry import registered_adapters
    import mfs.ingest.holdings  # noqa: F401
    assert "sbi" in registered_adapters()
