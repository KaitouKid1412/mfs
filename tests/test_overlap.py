"""Tests for the pairwise portfolio overlap math (Phase 3 Stage 3).

Overlap is the sum of `min(weight_A[s], weight_B[s])` across the union of
two funds' equity holdings, keyed by ISIN when present and by normalized
security_name as fallback. Both funds' weights are renormalized to 100%
on the equity sub-portfolio before comparison.
"""

from __future__ import annotations

import polars as pl
import pytest

from mfs.rank.overlap import (
    OverlapResult,
    format_top_shared,
    pairwise_overlap,
)


def _holding(name: str, weight: float, isin: str | None = None,
             instrument_type: str = "Equity"):
    return {
        "security_name": name,
        "weight_pct": weight,
        "isin": isin,
        "instrument_type": instrument_type,
    }


def _df(rows):
    return pl.DataFrame(rows)


# ---------------------------------------------------------------------------
# Basic overlap math
# ---------------------------------------------------------------------------


def test_identical_portfolios_overlap_100():
    rows = [
        _holding("HDFC Bank Ltd.", 30.0, "INE040A01034"),
        _holding("ICICI Bank Ltd.", 25.0, "INE090A01021"),
        _holding("Reliance Industries Ltd.", 20.0, "INE002A01018"),
        _holding("Infosys Ltd.", 25.0, "INE009A01021"),
    ]
    a = _df(rows)
    b = _df(rows)
    r = pairwise_overlap(a, b)
    assert r.overlap_pct == 100.0
    assert r.n_overlapping_securities == 4
    assert r.reason is None


def test_disjoint_portfolios_overlap_0():
    a = _df([
        _holding("HDFC Bank Ltd.", 50.0, "INE040A01034"),
        _holding("ICICI Bank Ltd.", 50.0, "INE090A01021"),
    ])
    b = _df([
        _holding("Tata Motors Ltd.", 50.0, "INE155A01022"),
        _holding("Maruti Suzuki Ltd.", 50.0, "INE585B01010"),
    ])
    r = pairwise_overlap(a, b)
    assert r.overlap_pct == 0.0
    assert r.n_overlapping_securities == 0


def test_partial_overlap_uses_min_weight():
    """Two stocks shared: A has them at 40+20, B at 30+30. Overlap =
    min(40,30) + min(20,30) = 30 + 20 = 50."""
    a = _df([
        _holding("Stock A", 40.0, "INE_A"),
        _holding("Stock B", 20.0, "INE_B"),
        _holding("Stock C", 40.0, "INE_C"),
    ])
    b = _df([
        _holding("Stock A", 30.0, "INE_A"),
        _holding("Stock B", 30.0, "INE_B"),
        _holding("Stock D", 40.0, "INE_D"),
    ])
    r = pairwise_overlap(a, b)
    assert r.overlap_pct == 50.0
    assert r.n_overlapping_securities == 2


# ---------------------------------------------------------------------------
# Key resolution: ISIN preferred, name fallback
# ---------------------------------------------------------------------------


def test_isin_match_wins_when_both_have_isin():
    """Even if security_name differs ('HDFC Bank Ltd.' vs 'HDFC BANK LIMITED'),
    a matching ISIN should still produce a match."""
    a = _df([_holding("HDFC Bank Ltd.", 100.0, "INE040A01034")])
    b = _df([_holding("HDFC BANK LIMITED", 100.0, "INE040A01034")])
    r = pairwise_overlap(a, b)
    assert r.overlap_pct == 100.0


def test_name_normalization_handles_ltd_variants():
    """When neither row has ISIN, normalized-name match should equate
    'HDFC Bank Ltd.' with 'HDFC BANK LIMITED'."""
    a = _df([_holding("HDFC Bank Ltd.", 100.0)])
    b = _df([_holding("HDFC BANK LIMITED", 100.0)])
    r = pairwise_overlap(a, b)
    assert r.overlap_pct == 100.0


def test_mixed_isin_and_name_keys_are_independent():
    """One fund has ISIN, the other doesn't — they don't match (we use a
    namespaced key `isin:X` vs `name:y`). This is intentional: assuming
    ISIN-less rows correspond to the same instruments as ISIN-tagged rows
    of the same name across funds would mask data-quality issues."""
    a = _df([_holding("HDFC Bank Ltd.", 100.0, "INE040A01034")])
    b = _df([_holding("HDFC Bank Ltd.", 100.0)])  # no ISIN
    r = pairwise_overlap(a, b)
    assert r.overlap_pct == 0.0


# ---------------------------------------------------------------------------
# Equity-only filter and renormalization
# ---------------------------------------------------------------------------


def test_non_equity_rows_excluded():
    """Debt / cash / REIT rows are excluded; equity weights renormalize to 100%."""
    a = _df([
        _holding("Stock A", 50.0, "INE_A", instrument_type="Equity"),
        _holding("Bond X",  50.0, "INE_BOND", instrument_type="Debt"),
    ])
    b = _df([_holding("Stock A", 100.0, "INE_A", instrument_type="Equity")])
    r = pairwise_overlap(a, b)
    # A's equity sub-portfolio is 100% Stock A after renormalization.
    assert r.overlap_pct == 100.0


def test_weights_renormalized_to_100():
    """Even if reported weights don't sum to 100, renormalization makes the
    overlap math meaningful."""
    a = _df([
        _holding("Stock A", 25.0, "INE_A"),
        _holding("Stock B", 25.0, "INE_B"),
    ])  # equity total = 50
    b = _df([
        _holding("Stock A", 50.0, "INE_A"),
        _holding("Stock B", 50.0, "INE_B"),
    ])  # equity total = 100
    r = pairwise_overlap(a, b)
    # After renormalization both funds are 50% Stock A + 50% Stock B → 100% overlap.
    assert r.overlap_pct == 100.0


# ---------------------------------------------------------------------------
# Missing data handling
# ---------------------------------------------------------------------------


def test_empty_holdings_returns_missing_holdings_reason():
    a = _df([_holding("Stock A", 100.0, "INE_A")])
    b = pl.DataFrame()
    r = pairwise_overlap(a, b)
    assert r.overlap_pct is None
    assert r.reason == "missing_holdings"


def test_all_debt_holdings_returns_missing_holdings_reason():
    """A debt-only fund has no equity exposure to compare against."""
    a = _df([_holding("Stock A", 100.0, "INE_A")])
    b = _df([_holding("Bond X", 100.0, "INE_BOND", instrument_type="Debt")])
    r = pairwise_overlap(a, b)
    assert r.overlap_pct is None
    assert r.reason == "missing_holdings"


# ---------------------------------------------------------------------------
# Top-shared reporting
# ---------------------------------------------------------------------------


def test_top_shared_holdings_sorted_descending():
    a = _df([
        _holding("Big Stock", 50.0, "INE_BIG"),
        _holding("Mid Stock", 30.0, "INE_MID"),
        _holding("Small Stock", 20.0, "INE_SMALL"),
    ])
    b = _df([
        _holding("Big Stock", 50.0, "INE_BIG"),
        _holding("Mid Stock", 30.0, "INE_MID"),
        _holding("Small Stock", 20.0, "INE_SMALL"),
    ])
    r = pairwise_overlap(a, b, top_k_shared=2)
    assert len(r.top_shared_holdings) == 2
    assert r.top_shared_holdings[0][0] == "Big Stock"
    assert r.top_shared_holdings[0][1] == 50.0


def test_format_top_shared_renders_as_human_cell():
    out = format_top_shared([("HDFC Bank Ltd.", 5.2), ("Reliance", 4.1)])
    assert out == "HDFC Bank Ltd. (5.2%), Reliance (4.1%)"


# ---------------------------------------------------------------------------
# Real-world scenario: cross-AMC funds with overlapping bluechips
# ---------------------------------------------------------------------------


def test_two_large_cap_funds_with_shared_bluechips():
    """Realistic scenario: two large-cap funds both holding the top
    bluechips at similar weights → high overlap."""
    a = _df([
        _holding("HDFC Bank Ltd.", 9.0, "INE040A01034"),
        _holding("ICICI Bank Ltd.", 8.0, "INE090A01021"),
        _holding("Reliance Industries Ltd.", 7.5, "INE002A01018"),
        _holding("Infosys Ltd.", 6.0, "INE009A01021"),
        _holding("TCS Ltd.", 5.0, "INE467B01029"),
        _holding("Other 1", 30.0, "INE_O1"),
        _holding("Other 2", 34.5, "INE_O2"),
    ])
    b = _df([
        _holding("HDFC Bank Ltd.", 10.0, "INE040A01034"),
        _holding("ICICI Bank Ltd.", 7.5, "INE090A01021"),
        _holding("Reliance Industries Ltd.", 7.0, "INE002A01018"),
        _holding("Infosys Ltd.", 5.5, "INE009A01021"),
        _holding("Other 3", 35.0, "INE_O3"),
        _holding("Other 4", 35.0, "INE_O4"),
    ])
    r = pairwise_overlap(a, b)
    # Expect 9 + 7.5 + 7 + 5.5 = 29.0% overlap on the four shared stocks.
    assert 28.0 <= r.overlap_pct <= 30.0
    assert r.n_overlapping_securities == 4
