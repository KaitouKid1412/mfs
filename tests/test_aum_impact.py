"""Tests for the AUM Impact Cost compute layer (Phase 2.3.A)."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from mfs.compute.aum_impact import (
    ADV_WINDOW_DAYS,
    MIN_HOLDINGS_FOR_IMPACT,
    TOP_N_ILLIQUID,
    _median_adv,
    aum_impact_cost,
)


def _holding(isin="INE001", weight=10.0, instrument_type="Equity"):
    return {
        "scheme_code": "S1",
        "security_name": f"Stock {isin}",
        "as_of_month": date(2026, 4, 1),
        "weight_pct": float(weight),
        "isin": isin,
        "instrument_type": instrument_type,
    }


def _adv_row(isin, day, ttv):
    return {"isin": isin, "date": day, "total_traded_value": float(ttv)}


# ---------------------------------------------------------------------------
# _median_adv — trailing-window median behavior
# ---------------------------------------------------------------------------


def test_median_adv_single_isin_full_window():
    """30 days × identical TTV → median equals that TTV."""
    today = date(2026, 5, 1)
    rows = [_adv_row("INE_A", today - timedelta(days=i), 1_000_000.0) for i in range(30)]
    df = pl.DataFrame(rows)
    med = _median_adv(df, window_days=60)
    assert med == {"INE_A": 1_000_000.0}


def test_median_adv_skips_data_outside_window():
    today = date(2026, 5, 1)
    rows = [
        _adv_row("INE_A", today - timedelta(days=10), 5_000_000.0),
        _adv_row("INE_A", today - timedelta(days=20), 5_000_000.0),
        _adv_row("INE_A", today - timedelta(days=30), 5_000_000.0),
        # Outside the 60-day window
        _adv_row("INE_A", today - timedelta(days=200), 999_999_999.0),
    ]
    df = pl.DataFrame(rows)
    med = _median_adv(df, window_days=60)
    # Outlier is filtered, median is 5M
    assert med == {"INE_A": 5_000_000.0}


def test_median_adv_empty_input():
    assert _median_adv(pl.DataFrame()) == {}


# ---------------------------------------------------------------------------
# aum_impact_cost — math against synthetic portfolios
# ---------------------------------------------------------------------------


def test_aum_impact_basic_math():
    """Fund AUM 100 Cr, 1 holding at 5% (= 5 Cr = 50M INR), ADV 25M → 2 days.

    But we need at least MIN_HOLDINGS_FOR_IMPACT (=3) ISIN-resolved holdings,
    so add two cheap holdings with very fast ADV (so they don't dominate).
    """
    holdings = pl.DataFrame([
        _holding(isin="INE_SLOW", weight=5.0),  # 5% of 100 Cr = 50M INR
        _holding(isin="INE_FAST1", weight=2.0),
        _holding(isin="INE_FAST2", weight=2.0),
    ])
    adv = {
        "INE_SLOW": 25_000_000.0,    # 25M INR/day → exit takes 2 days
        "INE_FAST1": 1_000_000_000.0,  # very liquid
        "INE_FAST2": 1_000_000_000.0,
    }
    out = aum_impact_cost(holdings, adv, aum_crore=100.0)
    assert out is not None
    assert abs(out - 2.0) < 1e-6


def test_aum_impact_returns_max_across_top_n():
    """Five holdings; max days_to_exit wins."""
    holdings = pl.DataFrame([
        _holding(isin="INE_A", weight=10.0),  # 10% of 50Cr = 5Cr = 50M
        _holding(isin="INE_B", weight=10.0),
        _holding(isin="INE_C", weight=10.0),
        _holding(isin="INE_D", weight=10.0),
        _holding(isin="INE_E", weight=10.0),
    ])
    adv = {
        "INE_A": 5_000_000.0,    # 50M / 5M = 10 days
        "INE_B": 10_000_000.0,   # 5 days
        "INE_C": 25_000_000.0,   # 2 days
        "INE_D": 50_000_000.0,   # 1 day
        "INE_E": 100_000_000.0,  # 0.5 day
    }
    out = aum_impact_cost(holdings, adv, aum_crore=50.0)
    assert abs(out - 10.0) < 1e-6  # worst-case is INE_A


def test_aum_impact_filters_non_equity():
    """Debt / Cash rows must be excluded from the computation."""
    holdings = pl.DataFrame([
        _holding(isin="INE_DEBT", weight=80.0, instrument_type="Debt"),
        _holding(isin="INE_EQ1", weight=8.0, instrument_type="Equity"),
        _holding(isin="INE_EQ2", weight=6.0, instrument_type="Equity"),
        _holding(isin="INE_EQ3", weight=6.0, instrument_type="Equity"),
    ])
    adv = {
        "INE_DEBT": 1_000_000.0,     # would be slowest by far if included
        "INE_EQ1": 100_000_000.0,
        "INE_EQ2": 100_000_000.0,
        "INE_EQ3": 100_000_000.0,
    }
    out = aum_impact_cost(holdings, adv, aum_crore=100.0)
    # If debt was included, INE_DEBT would give 80M INR / 1M ADV = 80 days.
    # Since it's filtered, max is from the equity holdings (8 Cr / 10 Cr = 0.8 days).
    assert out is not None
    assert out < 1.0


def test_aum_impact_skips_isin_without_adv():
    """Holdings whose ISIN has no ADV row are skipped, not failed."""
    holdings = pl.DataFrame([
        _holding(isin="INE_KNOWN1", weight=5.0),
        _holding(isin="INE_KNOWN2", weight=5.0),
        _holding(isin="INE_KNOWN3", weight=5.0),
        _holding(isin="INE_UNKNOWN", weight=5.0),  # missing from adv dict
    ])
    adv = {
        "INE_KNOWN1": 50_000_000.0,
        "INE_KNOWN2": 50_000_000.0,
        "INE_KNOWN3": 50_000_000.0,
    }
    out = aum_impact_cost(holdings, adv, aum_crore=100.0)
    # Each known holding: 5% of 100Cr = 5Cr = 50M; ADV 50M → 1 day each.
    # Unknown holding is skipped. Three resolved → meets min_holdings.
    assert out is not None
    assert abs(out - 1.0) < 1e-6


def test_aum_impact_returns_none_below_min_holdings():
    """Only 2 ISIN-resolved holdings is below the default min of 3."""
    holdings = pl.DataFrame([
        _holding(isin="INE_A", weight=5.0),
        _holding(isin="INE_B", weight=5.0),
    ])
    adv = {"INE_A": 50_000_000.0, "INE_B": 50_000_000.0}
    assert aum_impact_cost(holdings, adv, aum_crore=100.0) is None


def test_aum_impact_returns_none_when_aum_unknown():
    holdings = pl.DataFrame([_holding(weight=5.0)] * 3)
    adv = {"INE001": 50_000_000.0}
    assert aum_impact_cost(holdings, adv, aum_crore=0.0) is None


def test_aum_impact_returns_none_when_adv_empty():
    holdings = pl.DataFrame([
        _holding(isin="INE_A", weight=5.0),
        _holding(isin="INE_B", weight=5.0),
        _holding(isin="INE_C", weight=5.0),
    ])
    assert aum_impact_cost(holdings, {}, aum_crore=100.0) is None


def test_aum_impact_skips_zero_or_negative_adv():
    """A stock with 0 ADV (e.g. illiquid penny stock with no trades) must
    not produce infinite days. It's dropped from the calc."""
    holdings = pl.DataFrame([
        _holding(isin="INE_ZERO", weight=5.0),  # ADV=0, drop
        _holding(isin="INE_A", weight=5.0),
        _holding(isin="INE_B", weight=5.0),
        _holding(isin="INE_C", weight=5.0),
    ])
    adv = {
        "INE_ZERO": 0.0,
        "INE_A": 50_000_000.0,
        "INE_B": 50_000_000.0,
        "INE_C": 50_000_000.0,
    }
    out = aum_impact_cost(holdings, adv, aum_crore=100.0)
    assert out is not None
    assert out < 10.0  # finite, not exploded


def test_top_n_truncates_to_worst_holdings():
    """20 holdings each take a different number of days; we expect max of
    top-10 worst (= holding 1 with days=20)."""
    holdings = pl.DataFrame([
        _holding(isin=f"INE_{i:02d}", weight=2.0)
        for i in range(20)
    ])
    # Holding i takes (i+1) days
    adv = {
        f"INE_{i:02d}": (100.0 / (i + 1)) * 1e6   # weight 2% of 50Cr = 1Cr = 10M
        for i in range(20)
    }
    out = aum_impact_cost(holdings, adv, aum_crore=50.0)
    # weight 2% * 50Cr = 1Cr = 10M; days = 10M / adv.
    # holding 0: 10M / 100M = 0.1 days
    # holding 19: 10M / (100/20)M = 10M / 5M = 2 days
    # Max is holding 19 → 2 days.
    assert out is not None
    assert abs(out - 2.0) < 1e-6
