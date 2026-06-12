"""Tests for scheme_master category classification.

Focus on the closed-ended / interval guard: closed-ended tax-saver series must
NOT leak into the equity universe via the "ELSS"/"Tax Saver" substring rules.
"""

from __future__ import annotations

import polars as pl
import pytest

from mfs.master.scheme_master import (
    _apply_single_option_default,
    _base_fund_id,
    _canonical_category,
    _classify_plan_option,
    _classify_sector,
    _direct_unknown_rankable,
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


# ---------------------------------------------------------------------------
# Option parsing: 'Cumulative' → GROWTH, spelled-out / typo'd IDCW → IDCW,
# single-option default, DIRECT+UNKNOWN sanity listing.
# ---------------------------------------------------------------------------

# Real AMFI names of the 4 large ICICI Pru growth plans previously excluded
# from the universe (option_type stayed UNKNOWN).
ICICI_CUMULATIVE_NAMES = [
    "ICICI Prudential Equity Savings Fund - Direct Plan - Cumulative option",
    "ICICI Prudential Manufacturing Fund - Direct Plan - Cumulative Option",
    "ICICI Prudential India Opportunities Fund - Direct Plan - Cumulative Option",
    "ICICI Prudential Pharma Healthcare and Diagnostics (P.H.D) Fund"
    " - Direct Plan - Cumulative Option",
]


@pytest.mark.parametrize("name", ICICI_CUMULATIVE_NAMES)
def test_cumulative_option_is_growth(name):
    assert _classify_plan_option(name) == ("DIRECT", "GROWTH")


def test_cumulative_idcw_keeps_idcw_precedence():
    """'Cumulative IDCW' oddities must resolve to IDCW, not GROWTH."""
    assert _classify_plan_option("X Fund - Direct Plan - Cumulative IDCW") == (
        "DIRECT",
        "IDCW",
    )


@pytest.mark.parametrize(
    "name",
    [
        # Spelled-out IDCW phrase (real AMFI names)
        "Kotak Flexicap Fund - Payout of Income Distribution cum capital"
        " withdrawal option- Direct",
        "TATA Small Cap Fund Direct Plan - Reinvestment of Income Distribution"
        " cum capital withdrawal option",
        "360 ONE QUANT FUND DIRECT INCOME DISTRIBUTION CUM CAPITAL WITHDRAWAL",
        # Recurring AMFI typos
        "Canara Robeco Manufacturing Fund - Direct Plan - IDWC Option",
        "PGIM India Aggressive Hybrid Equity Fund-Direct Plan-Quarterly Divdend Option",
        "Baroda BNP Paribas Energy Opportunities Fund - Regular Plan - ICDW Option",
        # Abbreviated "Div" option token
        "UTI FTIF Series XXVII-VI (1113 Days) - Direct Plan - Annual Div Option",
    ],
)
def test_spelled_out_and_typo_idcw(name):
    assert _classify_plan_option(name)[1] == "IDCW"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # 'Dividend Yield' is the fund mandate, not an option token: growth
        # plans of the whole category were previously misclassified IDCW and
        # silently excluded from the universe. Real AMFI names.
        ("ICICI Prudential Dividend Yield Equity Fund Direct Plan Growth Option", "GROWTH"),
        ("UTI-Dividend Yield Fund.-Growth-Direct", "GROWTH"),
        ("HDFC Dividend Yield Fund - Growth Option Direct Plan", "GROWTH"),
        # ... while genuine IDCW plans of the same funds stay IDCW.
        ("HDFC Dividend Yield Fund - IDCW Option Direct Plan", "IDCW"),
        ("Tata Dividend Yield Fund-Direct Plan-IDCW Payout", "IDCW"),
        (
            "SBI Dividend Yield Fund - Direct Plan - Income Distribution cum"
            " Capital Withdrawal (IDCW) Option",
            "IDCW",
        ),
    ],
)
def test_dividend_yield_mandate_not_option_token(name, expected):
    assert _classify_plan_option(name)[1] == expected


def test_growth_and_bonus_unchanged():
    assert _classify_plan_option("Axis Bluechip Fund - Direct Plan - Growth") == (
        "DIRECT",
        "GROWTH",
    )
    # Legacy bonus-unit plans are neither growth nor IDCW: stay UNKNOWN.
    assert _classify_plan_option(
        "JM Aggressive Hybrid Fund (Direct) - Annual Bonus Option"
    ) == ("DIRECT", "UNKNOWN")


def _option_df(names: list[str], amc_slug: str = "samco") -> pl.DataFrame:
    """Build the minimal frame the post-pass operates on, via the real parsers."""
    rows = []
    for name in names:
        plan, option = _classify_plan_option(name)
        rows.append(
            {
                "scheme_name": name,
                "plan_type": plan,
                "option_type": option,
                "base_fund_id": _base_fund_id(name, amc_slug),
            }
        )
    return pl.DataFrame(rows)


def test_single_option_default_no_sibling_is_growth():
    """A token-less single-option fund (real Samco case) defaults to GROWTH."""
    df = _apply_single_option_default(
        _option_df(
            ["Samco Mid Cap Fund - Direct Plan", "Samco Mid Cap Fund - Regular Plan"]
        )
    )
    assert df["option_type"].to_list() == ["GROWTH", "GROWTH"]


def test_single_option_default_with_idcw_sibling_stays_unknown():
    """A resolved sibling in the same (base_fund_id, plan_type) group means
    genuine ambiguity — the token-less row must stay UNKNOWN."""
    df = _apply_single_option_default(
        _option_df(
            [
                "Samco Mid Cap Fund - Direct Plan",
                "Samco Mid Cap Fund - Direct Plan - IDCW",
            ]
        )
    )
    by_name = dict(df.select("scheme_name", "option_type").rows())
    assert by_name["Samco Mid Cap Fund - Direct Plan"] == "UNKNOWN"
    assert by_name["Samco Mid Cap Fund - Direct Plan - IDCW"] == "IDCW"


def test_single_option_default_skips_token_carrying_rows():
    """Rows that DO carry an option token (e.g. Bonus) never get the default,
    even with no resolved sibling."""
    df = _apply_single_option_default(
        _option_df(["PGIM India Large Cap Fund - Direct Plan - Bonus"], amc_slug="pgim")
    )
    assert df["option_type"].to_list() == ["UNKNOWN"]


def test_direct_unknown_rankable_listing():
    """Sanity listing: active DIRECT+UNKNOWN rows in rankable categories only."""
    df = pl.DataFrame(
        {
            "scheme_code": ["1", "2", "3", "4", "5"],
            "scheme_name": ["a", "b", "c", "d", "e"],
            "plan_type": ["DIRECT", "DIRECT", "REGULAR", "DIRECT", "DIRECT"],
            "option_type": ["UNKNOWN", "GROWTH", "UNKNOWN", "UNKNOWN", "UNKNOWN"],
            "canonical_category": ["Mid Cap", "Mid Cap", "Mid Cap", None, "Mid Cap"],
            "is_active": [True, True, True, True, False],
        }
    )
    assert _direct_unknown_rankable(df)["scheme_code"].to_list() == ["1"]
