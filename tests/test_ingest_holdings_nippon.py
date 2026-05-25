"""Tests for the Nippon India monthly portfolio Excel adapter (Phase 3.C).

Unlike HDFC where each scheme has its own per-month Excel, Nippon publishes
ONE consolidated workbook per month with ~110 sheets (one per scheme plus
an Index sheet). The adapter discovers the master URL, downloads it once,
and parses one sheet per scheme by name lookup.

All ISIN-bearing rows must round-trip:
- weight is unit-converted from fraction (Nippon's storage) to percent
  (the canonical schema unit);
- segregated-portfolio mini-tables after the first GRAND TOTAL are
  excluded;
- non-traded suffix '**' is stripped from security names.
"""

from __future__ import annotations

import re
from pathlib import Path

import openpyxl
import pytest

from mfs.ingest.holdings.nippon import (
    NipponHoldingsAdapter,
    _classify_section,
    _extract_scheme_name_from_index_row,
    _is_isin,
    _month_name_to_int,
    _parse_filename_ym,
    _publish_ym,
    _URL_RE,
)

FIXTURE_XLSX = (
    Path(__file__).parent
    / "fixtures/holdings/nippon/monthly_portfolio_april_2026.xlsx"
)


# ---------------------------------------------------------------------------
# Unit tests on helpers
# ---------------------------------------------------------------------------


def test_publish_ym_rolls_year_at_december():
    assert _publish_ym("2025-12") == "2026-01"


def test_publish_ym_normal_month():
    assert _publish_ym("2026-04") == "2026-05"


def test_is_isin_accepts_valid_indian_equity_isin():
    assert _is_isin("INE090A01021") is True


def test_is_isin_rejects_section_banner_strings():
    """Banner strings ('Equity & Equity related', 'Money Market Instruments')
    live in the name column, never in the ISIN column. The strict check has
    to reject every non-12-char or non-pattern string so banner rows don't
    accidentally turn into 'holding' rows."""
    assert _is_isin("Equity & Equity related") is False
    assert _is_isin("Money Market Instruments") is False
    assert _is_isin("Subtotal") is False
    assert _is_isin("GRAND TOTAL") is False
    assert _is_isin("") is False


def test_is_isin_rejects_wrong_lengths():
    assert _is_isin("INE090A0102") is False     # 11 chars
    assert _is_isin("INE090A010210") is False   # 13 chars


def test_is_isin_rejects_non_check_digit_terminator():
    """The 12th char must be a digit (the check digit). A letter there =
    not a valid ISIN."""
    assert _is_isin("INE090A0102A") is False


def test_is_isin_accepts_money_market_isin_with_digits_in_middle():
    """Certificate-of-deposit ISINs (INE160A16UF9) have alphanumerics in
    the body — they must still match."""
    assert _is_isin("INE160A16UF9") is True


@pytest.mark.parametrize("label,expected", [
    # Equity banners
    ("Equity & Equity related", "Equity"),
    # Sub-banners like "(a) Listed..." and "(b) UNLISTED" don't carry an
    # instrument keyword on their own — the walker keeps the previously
    # set section. _classify_section returns None for them so the section
    # tracker isn't overwritten.
    ("(a) Listed / awaiting listing on Stock Exchanges", None),
    ("(b) UNLISTED", None),
    # Debt banners
    ("Money Market Instruments", "Debt"),
    ("Certificate of Deposit", "Debt"),
    ("Triparty Repo/ Reverse Repo Instrument", "Debt"),
    ("Triparty Repo", "Debt"),
    ("Government Securities", "Debt"),
    ("Non Convertible Debentures", "Debt"),
    ("Zero Coupon Bonds", "Debt"),
    ("Preference Shares", "Debt"),
    ("Debt Instruments", "Debt"),
    # REIT/InvIT
    ("REIT", "REIT/InvIT"),
    ("InvIT", "REIT/InvIT"),
    # Cash
    ("Net Current Assets", "Cash"),
    ("OTHERS", "Cash"),
    ("Cash Margin - CCIL", "Cash"),
    # Subtotal/total rows are NOT banners — should return None so the
    # walker doesn't overwrite the active section with a footer.
    ("Subtotal", None),
    ("Total", None),
    ("Grand Total", None),
    # Unrelated garbage
    ("Foo Bar", None),
    ("", None),
])
def test_classify_section(label, expected):
    assert _classify_section(label) == expected


# ---------------------------------------------------------------------------
# Month-name + filename parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token,expected", [
    ("April", 4),
    ("Apr", 4),
    ("apr", 4),
    ("APRIL", 4),
    ("Feb", 2),
    ("February", 2),
    ("Sept", 9),   # Nippon occasionally writes "Sept"
    ("Sep", 9),
    ("May", 5),
    ("Jun", 6),
    ("June", 6),
    ("December", 12),
    ("Dec", 12),
])
def test_month_name_to_int_accepts_full_and_abbreviated(token, expected):
    assert _month_name_to_int(token) == expected


def test_month_name_to_int_rejects_unknown():
    assert _month_name_to_int("Smarch") is None
    assert _month_name_to_int("") is None


def test_parse_filename_ym_for_april_2026_full_name():
    assert _parse_filename_ym(
        "NIMF-MONTHLY-PORTFOLIO-30-April-26.xls"
    ) == "2026-04"


def test_parse_filename_ym_for_february_2026_short_name():
    assert _parse_filename_ym(
        "NIMF-MONTHLY-PORTFOLIO-28-Feb-26.xls"
    ) == "2026-02"


def test_parse_filename_ym_for_january_2026_with_underscores():
    """Older filenames used underscores ('NIMF_MONTHLY_PORTFOLIO_31-Jan-25.xls').
    The regex tolerates the underscore variant so we can still discover
    months we backfill from."""
    assert _parse_filename_ym(
        "NIMF_MONTHLY_PORTFOLIO_31-Jan-25.xls"
    ) == "2025-01"


def test_parse_filename_ym_returns_none_for_fortnightly():
    """Fortnightly files share the page but have a different naming root.
    They must not match — otherwise we'd grab the 15th-of-month snapshot."""
    assert _parse_filename_ym(
        "NIMF-FORTNIGHTLY-PORTFOLIO-30-April-26.xls"
    ) is None


def test_parse_filename_ym_returns_none_for_unrelated():
    assert _parse_filename_ym("Mandatory-Investment-disclosure.pdf") is None


# ---------------------------------------------------------------------------
# URL regex contract
# ---------------------------------------------------------------------------


def test_url_re_matches_a_real_nippon_url():
    snippet = (
        'foo <a href="https://mf.nipponindiaim.com/InvestorServices/'
        'FactsheetsDocuments/NIMF-MONTHLY-PORTFOLIO-30-April-26.xls">'
        'bar</a>'
    )
    m = _URL_RE.search(snippet)
    assert m is not None
    assert m.group("day") == "30"
    assert m.group("month") == "April"
    assert m.group("yy") == "26"
    # The URL itself must stop at .xls — no trailing HTML.
    assert m.group(1).endswith(".xls")
    assert '"' not in m.group(1)


def test_url_re_matches_february_month_end():
    """Feb is the canonical 'short-month' edge — adapter must accept day=28
    and the abbreviated 'Feb' token."""
    snippet = (
        'href="https://mf.nipponindiaim.com/InvestorServices/'
        'FactsheetsDocuments/NIMF-MONTHLY-PORTFOLIO-28-Feb-26.xls"'
    )
    m = _URL_RE.search(snippet)
    assert m is not None
    assert m.group("day") == "28"
    assert m.group("month") == "Feb"


def test_url_re_skips_fortnightly_files():
    snippet = (
        'href="https://mf.nipponindiaim.com/InvestorServices/'
        'FactsheetsDocuments/NIMF-FORTNIGHTLY-PORTFOLIO-30-April-26.xls"'
    )
    assert _URL_RE.search(snippet) is None


def test_url_re_skips_debt_only_files():
    snippet = (
        'href="https://mf.nipponindiaim.com/InvestorServices/'
        'FactsheetsDocuments/Debt-Portfolio-31st-Jan-2021.xls"'
    )
    assert _URL_RE.search(snippet) is None


# ---------------------------------------------------------------------------
# Index-cell scheme-name extraction
# ---------------------------------------------------------------------------


def test_extract_scheme_name_strips_sebi_description():
    raw = (
        "NIPPON INDIA LARGE CAP FUND (An Open Ended Equity Scheme "
        "Predominantly Investing In Large Cap Stocks)"
    )
    assert _extract_scheme_name_from_index_row(raw) == "NIPPON INDIA LARGE CAP FUND"


def test_extract_scheme_name_takes_first_line_only():
    """Legacy schemes (Aggressive Hybrid, Credit Risk) have segregated-
    portfolio names appended on a second newline-separated line. We must
    take only the first line as the canonical name."""
    raw = (
        "Nippon India Aggressive Hybrid Fund (An Open Ended Hybrid Scheme "
        "Investing Predominantly In Equity And Equity Related Instruments)\n"
        "Nippon India Aggressive Hybrid Fund-SEGREGATED PORTFOLIO 2\n"
    )
    assert (
        _extract_scheme_name_from_index_row(raw)
        == "Nippon India Aggressive Hybrid Fund"
    )


def test_extract_scheme_name_handles_missing_paren():
    """An entry with no parenthesised description still returns a clean name."""
    assert (
        _extract_scheme_name_from_index_row("Nippon India Foo Fund")
        == "Nippon India Foo Fund"
    )


def test_extract_scheme_name_returns_none_for_non_string():
    assert _extract_scheme_name_from_index_row(None) is None
    assert _extract_scheme_name_from_index_row(42) is None


# ---------------------------------------------------------------------------
# Integration tests on the saved April-2026 master workbook
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> NipponHoldingsAdapter:
    return NipponHoldingsAdapter()


@pytest.fixture(scope="module")
def small_cap_records(adapter: NipponHoldingsAdapter) -> list:
    if not FIXTURE_XLSX.exists():
        pytest.skip(f"Fixture missing: {FIXTURE_XLSX}")
    return list(
        adapter.parse_excel(FIXTURE_XLSX, "NIPPON INDIA SMALL CAP FUND", "2026-04")
    )


@pytest.fixture(scope="module")
def large_cap_records(adapter: NipponHoldingsAdapter) -> list:
    if not FIXTURE_XLSX.exists():
        pytest.skip(f"Fixture missing: {FIXTURE_XLSX}")
    return list(
        adapter.parse_excel(FIXTURE_XLSX, "NIPPON INDIA LARGE CAP FUND", "2026-04")
    )


def test_small_cap_yields_many_holdings(small_cap_records):
    """Sanity floor — Small Cap is Nippon's biggest equity book and should
    always have ≥200 holdings."""
    assert len(small_cap_records) >= 200, (
        f"Small Cap had only {len(small_cap_records)} holdings; "
        "the section walker may be skipping rows."
    )


def test_small_cap_every_record_has_valid_isin(small_cap_records):
    """The whole point of Phase 3.C is ISIN-tagged rows. ZERO rows may
    miss the strict ISIN regex."""
    for r in small_cap_records:
        assert r.isin, f"row missing ISIN: {r}"
        assert _is_isin(r.isin), (
            f"malformed ISIN {r.isin!r} for {r.security_name!r}"
        )


def test_small_cap_no_duplicate_isins(small_cap_records):
    """An ISIN appearing twice = wrap/segregated-portfolio bleed. The
    `(scheme_code, security_name, as_of_month)` PK upstream doesn't catch
    this so the adapter has to."""
    isins = [r.isin for r in small_cap_records]
    dupes = sorted(s for s in set(isins) if isins.count(s) > 1)
    assert not dupes, f"duplicate ISINs: {dupes}"


def test_small_cap_total_equity_weight_is_realistic(small_cap_records):
    """≥85% equity is the floor for an equity fund. Anything lower would
    mean either the equity-section walker missed rows or the unit
    conversion went wrong."""
    eq = sum(
        r.weight_pct for r in small_cap_records if r.instrument_type == "Equity"
    )
    assert eq >= 85.0, f"equity weight={eq:.2f}% suspiciously low"


def test_small_cap_total_weight_under_101_pct(small_cap_records):
    """Total weight should add to roughly 100% (cash + equity + debt +
    everything). If we accidentally read Nippon's fractions AS percent
    we'd be at <2%. If we double-counted by ignoring the GRAND TOTAL
    sentinel we'd be at >100%. Use 101% as a soft ceiling allowing
    Nippon's own rounding."""
    total = sum(r.weight_pct for r in small_cap_records)
    assert 50.0 <= total <= 101.0, (
        f"total weight={total:.2f}% out of plausible range — "
        "check fraction→percent conversion."
    )


def test_small_cap_marquee_holdings_present(small_cap_records):
    """Anchor securities for the parser. If layout drift drops these,
    something material has changed in Nippon's Excel format."""
    by_isin = {r.isin: r for r in small_cap_records}
    must_have = {
        "INE040A01034": "HDFC Bank",            # top holding
        "INE745G01043": "Multi Commodity Exchange of India",
        "INE257A01026": "Bharat Heavy Electricals",
    }
    for isin, label in must_have.items():
        assert isin in by_isin, f"missing anchor {label} (ISIN {isin})"
        assert by_isin[isin].weight_pct > 0.5, (
            f"{label} weight={by_isin[isin].weight_pct:.4f}% — "
            "either the row is misplaced or the unit conversion is off."
        )


def test_small_cap_source_amc_is_nippon(small_cap_records):
    assert all(r.source_amc == "nippon" for r in small_cap_records)


def test_small_cap_scheme_name_propagated(small_cap_records):
    assert all(
        r.scheme_name_printed == "NIPPON INDIA SMALL CAP FUND"
        for r in small_cap_records
    )


def test_small_cap_strips_non_traded_marker(small_cap_records):
    """The '**' suffix Nippon attaches to illiquid securities is a
    presentation marker only — it should not survive into the canonical
    security_name (it would otherwise hurt fuzzy index-constituent
    matching)."""
    for r in small_cap_records:
        assert not r.security_name.endswith("*"), (
            f"non-traded suffix not stripped: {r.security_name!r}"
        )


def test_large_cap_top_holding_is_hdfc_bank_at_realistic_weight(large_cap_records):
    """Large Cap's #1 should be HDFC Bank in the 8-12% band; if we get
    0.0924% that proves we forgot the fraction→percent conversion."""
    top = max(large_cap_records, key=lambda r: r.weight_pct)
    assert top.isin == "INE040A01034", (
        f"expected HDFC Bank as top, got {top.security_name!r}"
    )
    assert 7.0 <= top.weight_pct <= 13.0, (
        f"HDFC Bank weight={top.weight_pct:.4f}% — unit conversion broken?"
    )


def test_large_cap_every_record_has_isin(large_cap_records):
    for r in large_cap_records:
        assert _is_isin(r.isin), (
            f"malformed ISIN {r.isin!r} for {r.security_name!r}"
        )


def test_unknown_scheme_yields_empty(adapter: NipponHoldingsAdapter):
    """If a printed name doesn't appear in the Index sheet (e.g. ETF
    renamed mid-month), parse should return zero records, not crash."""
    if not FIXTURE_XLSX.exists():
        pytest.skip(f"Fixture missing: {FIXTURE_XLSX}")
    records = list(
        adapter.parse_excel(
            FIXTURE_XLSX, "Nippon India Nonexistent Fund", "2026-04"
        )
    )
    assert records == []


def test_grand_total_stops_the_walker():
    """Direct check: build a sheet that has a real holding row, then a
    GRAND TOTAL, then a second 'holding' that should be ignored. If the
    GRAND-TOTAL sentinel is honored, only the first row comes through."""
    # We synthesize via direct workbook construction in-memory.
    wb = openpyxl.Workbook()
    # Build the Index sheet first so the adapter can look up our scheme.
    idx = wb.active
    idx.title = "Index"
    idx.append(["INDEX", None])
    idx.append(["FK", "Fake Test Fund (a fake)"])
    # Now the scheme sheet.
    ws = wb.create_sheet("FK")
    ws.append(["RLMF999", "FAKE TEST FUND (a fake)", None, None, None, None, None, None])
    ws.append([None, "Monthly Portfolio Statement as on April 30,2026"])
    ws.append([None])
    ws.append([None, "ISIN", "Name of the Instrument", "Industry / Rating",
               "Quantity", "Market/Fair Value", "% to NAV", "YIELD"])
    ws.append([None, None, "Equity & Equity related"])
    ws.append([None, None, "(a) Listed / awaiting listing on Stock Exchanges"])
    ws.append(["X1", "INE040A01034", "HDFC Bank Limited", "Banks", 1, 100.0, 0.05, None])
    ws.append([None, None, "GRAND TOTAL", None, None, 100.0, 1.0])
    # This row sits after GRAND TOTAL and should be ignored.
    ws.append(["X2", "INE090A01021", "ICICI Bank Limited", "Banks", 1, 100.0, 0.05, None])

    tmp_path = Path(__file__).parent / "fixtures/holdings/nippon/_grand_total_stop.xlsx"
    wb.save(tmp_path)
    try:
        adapter = NipponHoldingsAdapter()
        records = list(adapter.parse_excel(tmp_path, "Fake Test Fund", "2026-04"))
        isins = [r.isin for r in records]
        assert isins == ["INE040A01034"], (
            f"GRAND TOTAL didn't stop the walker; got ISINs {isins}"
        )
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# discover_scheme_urls — URL-selection slice (pure, no network)
# ---------------------------------------------------------------------------


def test_select_master_url_picks_the_right_month():
    """Given a snippet of the disclosures page with multiple monthly URLs,
    _select_master_url should return ONLY the one matching the requested
    data month."""
    html = """
    <a href="https://mf.nipponindiaim.com/InvestorServices/FactsheetsDocuments/NIMF-MONTHLY-PORTFOLIO-31-Mar-26.xls">Mar 26</a>
    <a href="https://mf.nipponindiaim.com/InvestorServices/FactsheetsDocuments/NIMF-MONTHLY-PORTFOLIO-30-April-26.xls">Apr 26</a>
    <a href="https://mf.nipponindiaim.com/InvestorServices/FactsheetsDocuments/NIMF-MONTHLY-PORTFOLIO-31-May-26.xls">May 26</a>
    """
    adapter = NipponHoldingsAdapter()
    apr = adapter._select_master_url(html, "2026-04")
    assert apr is not None and apr.endswith("NIMF-MONTHLY-PORTFOLIO-30-April-26.xls")
    mar = adapter._select_master_url(html, "2026-03")
    assert mar is not None and mar.endswith("NIMF-MONTHLY-PORTFOLIO-31-Mar-26.xls")
    # Month with no published file → None (caller handles fail-fast).
    assert adapter._select_master_url(html, "2026-12") is None


# ---------------------------------------------------------------------------
# Adapter registry smoke test
# ---------------------------------------------------------------------------


def test_nippon_holdings_adapter_is_registered():
    from mfs.ingest.holdings._registry import registered_adapters
    import mfs.ingest.holdings.nippon  # noqa: F401 — triggers registration
    assert "nippon" in registered_adapters()
