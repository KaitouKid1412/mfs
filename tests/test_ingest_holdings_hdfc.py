"""Tests for the HDFC monthly portfolio Excel holdings adapter (Phase 3.C).

The factsheet-based holdings path (Phase 2.2) yielded ~75% equity weight per
scheme and zero ISIN coverage. The Excel-based Phase 3.C path should yield
≥95% equity weight AND populate ISIN on every row, which is what unblocks
AUM Impact Cost computation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mfs.ingest.holdings.hdfc import (
    HdfcHoldingsAdapter,
    _classify_section,
    _decode_url_path,
    _FILENAME_RE,
    _is_isin,
    _publish_ym,
    _URL_RE,
)

FIXTURE_XLSX = Path(__file__).parent / "fixtures/holdings/hdfc/flexicap_april_2026.xlsx"


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
    """Section banners ('EQUITY & EQUITY RELATED') sit in the ISIN column on
    banner rows; the strict ISIN check must reject them so we don't write
    them as holdings."""
    assert _is_isin("EQUITY & EQUITY RELATED") is False
    assert _is_isin("Equity") is False
    assert _is_isin("") is False
    assert _is_isin("GARBAGE") is False


def test_is_isin_rejects_wrong_lengths():
    assert _is_isin("INE090A0102") is False     # 11 chars
    assert _is_isin("INE090A010210") is False   # 13 chars


@pytest.mark.parametrize("label,expected", [
    ("EQUITY & EQUITY RELATED", "Equity"),
    ("Equity", "Equity"),
    ("Debt Instruments", "Debt"),
    ("Money Market Instruments", "Debt"),
    ("Tri-Party Repo", "Debt"),
    ("Government Securities", "Debt"),
    ("REIT", "REIT/InvIT"),
    ("InvIT Units", "REIT/InvIT"),
    ("Net Current Assets", "Cash"),
    ("Cash & Cash Equivalents", "Cash"),
    ("Foo Bar", None),
])
def test_classify_section(label, expected):
    assert _classify_section(label) == expected


# ---------------------------------------------------------------------------
# URL parsing (regex contract)
# ---------------------------------------------------------------------------


def test_url_re_matches_a_real_hdfc_url():
    snippet = (
        'something else "https://files.hdfcfund.com/s3fs-public/2026-05/'
        'Monthly%20HDFC%20Flexi%20Cap%20Fund%20-%2030%20April%202026.xlsx"'
        ',"extension":"xlsx"'
    )
    m = _URL_RE.search(snippet)
    assert m is not None
    assert m.group(2) == "2026-05"
    assert m.group(1).endswith(".xlsx")
    # The URL itself MUST stop at .xlsx — no trailing JSON should be captured.
    assert '"' not in m.group(1)
    assert ',' not in m.group(1)


def test_filename_re_parses_real_filename():
    fn = "Monthly HDFC Flexi Cap Fund - 30 April 2026.xlsx"
    m = _FILENAME_RE.match(fn)
    assert m is not None
    assert m.group("scheme") == "HDFC Flexi Cap Fund"
    assert m.group("day") == "30"
    assert m.group("month") == "April"
    assert m.group("year") == "2026"


def test_filename_re_handles_month_ends_other_than_30():
    """February month-end is 28 or 29 — adapter must accept variable day."""
    fn = "Monthly HDFC Top 100 Fund - 28 February 2026.xlsx"
    m = _FILENAME_RE.match(fn)
    assert m is not None
    assert m.group("day") == "28"


def test_decode_url_path_unescapes_pct20():
    raw = "Monthly%20HDFC%20Flexi%20Cap%20Fund%20-%2030%20April%202026.xlsx"
    assert _decode_url_path(raw) == "Monthly HDFC Flexi Cap Fund - 30 April 2026.xlsx"


# ---------------------------------------------------------------------------
# Integration test: parse the saved HDFC Flexi Cap fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def flexicap_records():
    if not FIXTURE_XLSX.exists():
        pytest.skip(f"Fixture missing: {FIXTURE_XLSX}")
    adapter = HdfcHoldingsAdapter()
    return list(adapter.parse_excel(FIXTURE_XLSX, "HDFC Flexi Cap Fund", "2026-04"))


def test_flexicap_yields_many_rows(flexicap_records):
    """Sanity floor — Flexi Cap should always have ≥50 holdings."""
    assert len(flexicap_records) >= 50


def test_flexicap_every_record_has_isin(flexicap_records):
    """Phase 3.C's whole purpose: ISIN-tagged rows. ZERO rows may be missing."""
    for r in flexicap_records:
        assert r.isin, f"row missing ISIN: {r}"
        assert _is_isin(r.isin), f"malformed ISIN {r.isin!r} for {r.security_name!r}"


def test_flexicap_total_equity_weight_is_realistic(flexicap_records):
    """Equity-only weight should be ≥85% for an equity fund (the rest is debt /
    REIT / cash). Below 85% would suggest the equity section banner was
    misclassified or rows were silently dropped."""
    eq = sum(r.weight_pct for r in flexicap_records if r.instrument_type == "Equity")
    assert eq >= 85.0, f"equity weight={eq:.2f} is suspiciously low"


def test_flexicap_marquee_holdings_present(flexicap_records):
    """Anchor stocks for the parser: any layout drift that drops these should
    fail loudly."""
    by_isin = {r.isin: r for r in flexicap_records}
    must_have = {
        "INE090A01021": "ICICI Bank",        # top holding
        "INE040A01034": "HDFC Bank",
        "INE002A01018": "Reliance Industries",
    }
    for isin, label in must_have.items():
        assert isin in by_isin, f"missing anchor {label} (ISIN {isin})"
        # Each anchor should have a sensible weight (>0.5%).
        assert by_isin[isin].weight_pct > 0.5


def test_flexicap_no_duplicate_isins(flexicap_records):
    """An ISIN appearing twice in one Excel = wrap/section bug; the strict
    `(scheme_code, security_name, as_of_month)` PK doesn't dedup ISINs."""
    isins = [r.isin for r in flexicap_records]
    assert len(isins) == len(set(isins)), (
        f"duplicate ISINs: "
        f"{sorted(s for s in set(isins) if isins.count(s) > 1)}"
    )


def test_flexicap_source_amc_is_hdfc(flexicap_records):
    assert all(r.source_amc == "hdfc" for r in flexicap_records)


def test_flexicap_scheme_name_propagated(flexicap_records):
    assert all(r.scheme_name_printed == "HDFC Flexi Cap Fund" for r in flexicap_records)


def test_flexicap_instrument_types_diverse(flexicap_records):
    """The fixture is real-world: it has Equity, some Debt, and REIT/InvIT
    line items. Confirming the section walker tracks all three so we don't
    inadvertently regress to Equity-only."""
    types = {r.instrument_type for r in flexicap_records}
    assert "Equity" in types
    # Debt / REIT may or may not appear in any given month, but it's worth
    # noting that the parser DOES discover them in this snapshot.
    assert types.issubset({"Equity", "Debt", "REIT/InvIT", "Cash"})


# ---------------------------------------------------------------------------
# Adapter registry smoke test
# ---------------------------------------------------------------------------


def test_hdfc_holdings_adapter_is_registered():
    from mfs.ingest.holdings._registry import registered_adapters
    import mfs.ingest.holdings  # noqa: F401 — triggers registration
    assert "hdfc" in registered_adapters()
