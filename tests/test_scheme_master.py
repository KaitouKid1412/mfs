"""Tests for scheme_master category classification.

Focus on the closed-ended / interval guard: closed-ended tax-saver series must
NOT leak into the equity universe via the "ELSS"/"Tax Saver" substring rules.
"""

from __future__ import annotations

import pytest

from mfs.master.scheme_master import (
    _canonical_category,
    _classify_sector,
    _strip_amc_name,
)


@pytest.mark.parametrize(
    "amfi_category",
    [
        "Close Ended Schemes(ELSS)",
        "Close Ended Schemes(Equity Scheme - ELSS)",
        "Closed Ended Schemes(ELSS)",
        "Interval Fund Schemes(Debt)",
        "Interval Fund Schemes(Equity)",
    ],
)
def test_closed_ended_and_interval_excluded(amfi_category):
    """Closed-ended / interval schemes resolve to None (dropped from universe)."""
    assert _canonical_category(amfi_category) is None


def test_open_ended_elss_still_classified():
    """Open-ended ELSS (the legitimate case) must still map to ELSS."""
    assert _canonical_category("Open Ended Schemes(ELSS)") == "ELSS"
    assert _canonical_category("Open Ended Schemes(Equity Scheme - ELSS)") == "ELSS"


def test_open_ended_equity_unaffected():
    assert _canonical_category("Open Ended Schemes(Large Cap Fund)") == "Large Cap"
    assert _canonical_category("Open Ended Schemes(Mid Cap Fund)") == "Mid Cap"
    assert _canonical_category("Open Ended Schemes(Flexi Cap Fund)") == "Flexi Cap"


def test_non_equity_returns_none():
    assert _canonical_category("Open Ended Schemes(Debt Scheme - Liquid Fund)") is None
    assert _canonical_category(None) is None
    assert _canonical_category("") is None


# ---------------------------------------------------------------------------
# Sector classification must key off the fund mandate, not the AMC house name.
# ---------------------------------------------------------------------------

def test_amc_name_does_not_drive_sector():
    """AMCs whose name contains a sector token must not mis-classify their own
    sector/thematic funds (regression: "BANK OF INDIA Manufacturing &
    Infrastructure" → Banking, "Bajaj Finserv Healthcare" → Banking)."""
    cases = [
        ("BANK OF INDIA Manufacturing & Infrastructure Fund", "Bank of India Mutual Fund", "Infrastructure"),
        ("Bank of India Consumption Fund", "Bank of India Mutual Fund", "Consumption"),
        ("Bank of India Business Cycle Fund", "Bank of India Mutual Fund", "Thematic"),
        ("BAJAJ FINSERV HEALTHCARE FUND", "Bajaj Finserv Mutual Fund", "Pharma & Healthcare"),
        ("BAJAJ FINSERV CONSUMPTION FUND", "Bajaj Finserv Mutual Fund", "Consumption"),
    ]
    for scheme, amc, expected in cases:
        assert _classify_sector(_strip_amc_name(scheme, amc)) == expected, scheme


def test_genuine_banking_fund_still_classifies():
    """Stripping the AMC name must NOT break a real banking fund — the mandate
    word survives."""
    assert _classify_sector(_strip_amc_name(
        "Bank of India Financial Services Fund", "Bank of India Mutual Fund"
    )) == "Banking & Financial Services"
    assert _classify_sector(_strip_amc_name(
        "SBI Banking & Financial Services Fund", "SBI Mutual Fund"
    )) == "Banking & Financial Services"
