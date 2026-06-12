"""Tests for the Active Share compute layer (Phase 2.2)."""

from __future__ import annotations

from datetime import date, datetime

import polars as pl

from mfs.compute.active_share import (
    MIN_SNAPSHOTS_FOR_MEDIAN,
    active_share_one_month,
    trailing_median_active_share,
    _trailing_months,
)


def _holding(
    scheme="S1",
    name="HDFC Bank Ltd.",
    month=date(2026, 4, 1),
    weight=10.0,
    instrument_type="Equity",
):
    """Holding row using security_name as the join key (no ISIN required)."""
    return {
        "scheme_code": scheme,
        "security_name": name,
        "as_of_month": month,
        "weight_pct": float(weight),
        "isin": None,
        "instrument_type": instrument_type,
    }


def _constituent(
    ticker="NIFTY 100 TRI",
    name="HDFC Bank Ltd.",
    month=date(2026, 4, 1),
    weight=10.0,
):
    return {
        "ticker": ticker,
        "isin": None,
        "as_of_month": month,
        "weight_pct": float(weight),
        "security_name": name,
    }


# ---------------------------------------------------------------------------
# active_share_one_month — golden math
# ---------------------------------------------------------------------------


def test_identical_portfolio_gives_zero_active_share():
    fund = pl.DataFrame([
        _holding(name="HDFC Bank Ltd.", weight=60),
        _holding(name="ICICI Bank Ltd.", weight=40),
    ])
    bench = pl.DataFrame([
        _constituent(name="HDFC Bank Ltd.", weight=60),
        _constituent(name="ICICI Bank Ltd.", weight=40),
    ])
    assert abs(active_share_one_month(fund, bench) - 0.0) < 1e-9


def test_completely_disjoint_portfolio_gives_100_pct_active_share():
    fund = pl.DataFrame([
        _holding(name="Reliance Industries Ltd.", weight=60),
        _holding(name="Bharti Airtel Ltd.", weight=40),
    ])
    bench = pl.DataFrame([
        _constituent(name="HDFC Bank Ltd.", weight=60),
        _constituent(name="ICICI Bank Ltd.", weight=40),
    ])
    assert abs(active_share_one_month(fund, bench) - 100.0) < 1e-9


def test_partial_overlap_arithmetic():
    """Fund holds A 80%, B 20%. Bench holds A 50%, B 30%, C 20%.
       |80-50| + |20-30| + |0-20| = 60 → AS = 30%."""
    fund = pl.DataFrame([
        _holding(name="A Co Ltd.", weight=80),
        _holding(name="B Co Ltd.", weight=20),
    ])
    bench = pl.DataFrame([
        _constituent(name="A Co Ltd.", weight=50),
        _constituent(name="B Co Ltd.", weight=30),
        _constituent(name="C Co Ltd.", weight=20),
    ])
    assert abs(active_share_one_month(fund, bench) - 30.0) < 1e-9


def test_normalization_matches_ltd_variants():
    """'HDFC Bank Ltd.' and 'HDFC Bank Limited' should be treated as the same
    security (normalize_name strips Ltd./Limited)."""
    fund = pl.DataFrame([_holding(name="HDFC Bank Ltd.", weight=100)])
    bench = pl.DataFrame([_constituent(name="HDFC Bank Limited", weight=100)])
    assert abs(active_share_one_month(fund, bench) - 0.0) < 1e-9


def test_normalization_handles_punctuation_and_case():
    """Lowercase, punctuation-stripped names should match."""
    fund = pl.DataFrame([_holding(name="ICICI Bank Ltd.", weight=100)])
    bench = pl.DataFrame([_constituent(name="icici  BANK ltd", weight=100)])
    assert abs(active_share_one_month(fund, bench) - 0.0) < 1e-9


def test_active_share_renormalizes_fund_weights():
    """Equity 60/30 plus 10% debt → equity renormalizes to 66.67/33.33; vs
    bench HDFC Bank 100% → AS = 33.33%."""
    fund = pl.DataFrame([
        _holding(name="HDFC Bank Ltd.", weight=60),
        _holding(name="ICICI Bank Ltd.", weight=30),
        _holding(name="Govt Bond 2030", weight=10, instrument_type="Debt"),
    ])
    bench = pl.DataFrame([_constituent(name="HDFC Bank Ltd.", weight=100)])
    val = active_share_one_month(fund, bench)
    assert abs(val - 33.333333) < 1e-3


def test_active_share_filters_debt_and_cash():
    fund = pl.DataFrame([
        _holding(name="HDFC Bank Ltd.", weight=50),
        _holding(name="GOI 2030", weight=50, instrument_type="Debt"),
    ])
    bench = pl.DataFrame([_constituent(name="HDFC Bank Ltd.", weight=100)])
    val = active_share_one_month(fund, bench)
    assert abs(val - 0.0) < 1e-9


def test_empty_holdings_returns_none():
    assert active_share_one_month(pl.DataFrame(), pl.DataFrame([
        _constituent(name="HDFC Bank Ltd.", weight=100),
    ])) is None


def test_empty_constituents_returns_none():
    assert active_share_one_month(pl.DataFrame([_holding()]), pl.DataFrame()) is None


def test_holdings_with_only_debt_returns_none():
    fund = pl.DataFrame([_holding(name="GOI 2030", weight=100, instrument_type="Debt")])
    bench = pl.DataFrame([_constituent(name="HDFC Bank Ltd.", weight=100)])
    assert active_share_one_month(fund, bench) is None


# ---------------------------------------------------------------------------
# A1-8 — ISIN-first matching with canonicalized-name fallback
# ---------------------------------------------------------------------------


def test_same_isins_different_spellings_give_zero_active_share():
    """(a) Identical portfolios under different spellings but same ISINs →
    AS == 0.0. Pre-A1-8 name-only matching double-counted '&' vs 'and'
    spellings as disjoint securities and reported 100."""
    fund = pl.DataFrame([
        {**_holding(name="Larsen & Toubro Ltd", weight=60), "isin": "INE018A01030"},
        {**_holding(name="HDFC Bank Ltd.", weight=40), "isin": "INE040A01034"},
    ])
    bench = pl.DataFrame([
        {**_constituent(name="Larsen and Toubro Limited", weight=60),
         "isin": "INE018A01030"},
        {**_constituent(name="HDFC BANK LIMITED", weight=40), "isin": "INE040A01034"},
    ])
    assert abs(active_share_one_month(fund, bench) - 0.0) < 1e-9


def test_isin_match_despite_entirely_different_names():
    """(b) Rows sharing an ISIN match even when the printed names share no
    tokens at all."""
    fund = pl.DataFrame([
        {**_holding(name="L&T (formerly Larsen)", weight=100), "isin": "INE018A01030"},
    ])
    bench = pl.DataFrame([
        {**_constituent(name="Completely Unrelated Display String", weight=100),
         "isin": "INE018A01030"},
    ])
    assert abs(active_share_one_month(fund, bench) - 0.0) < 1e-9


def test_null_isin_falls_back_to_canonicalized_name():
    """(c) isin=None rows still match by normalized name, with '&'
    canonicalized to 'and' before punctuation stripping."""
    fund = pl.DataFrame([_holding(name="Larsen & Toubro Ltd", weight=100)])
    bench = pl.DataFrame([_constituent(name="Larsen and Toubro Limited", weight=100)])
    assert abs(active_share_one_month(fund, bench) - 0.0) < 1e-9


def test_mixed_isin_and_name_fallback_in_one_computation():
    """(d) One ISIN-keyed match plus one name-fallback match in the same
    portfolio → both cancel, AS == 0.0."""
    fund = pl.DataFrame([
        {**_holding(name="Mystery Display Name", weight=60), "isin": "INE018A01030"},
        _holding(name="ICICI Bank Ltd.", weight=40),  # isin=None → name key
    ])
    bench = pl.DataFrame([
        {**_constituent(name="Larsen and Toubro Limited", weight=60),
         "isin": "INE018A01030"},
        _constituent(name="ICICI BANK LIMITED", weight=40),  # isin=None
    ])
    assert abs(active_share_one_month(fund, bench) - 0.0) < 1e-9


def test_bare_co_inc_tokens_no_longer_over_merge():
    """(e) 'Apollo Co' and 'Apollo Inc' (no ISINs) are distinct issuers.
    Pre-A1-8 _LTD_RE stripped bare 'co'/'inc' tokens, merging both with any
    plain 'Apollo' — AS would have been 0.0. Now fully disjoint → 100."""
    fund = pl.DataFrame([_holding(name="Apollo Co", weight=100)])
    bench = pl.DataFrame([_constituent(name="Apollo Inc", weight=100)])
    assert abs(active_share_one_month(fund, bench) - 100.0) < 1e-9


# ---------------------------------------------------------------------------
# trailing_median_active_share — windowing + median
# ---------------------------------------------------------------------------


def test_trailing_months_basic():
    months = _trailing_months(date(2026, 4, 1), 6)
    assert months == [
        date(2025, 11, 1), date(2025, 12, 1),
        date(2026, 1, 1), date(2026, 2, 1),
        date(2026, 3, 1), date(2026, 4, 1),
    ]


def test_trailing_months_handles_year_boundary():
    """Last 3 months ending Feb 2026 = Dec 2025, Jan 2026, Feb 2026."""
    months = _trailing_months(date(2026, 2, 1), 3)
    assert months == [date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)]


def _build_monthly_fund(months: list[date], weight_a: float = 60.0):
    return pl.DataFrame([
        _holding(name="HDFC Bank Ltd.", weight=weight_a, month=m) for m in months
    ] + [
        _holding(name="ICICI Bank Ltd.", weight=100 - weight_a, month=m) for m in months
    ])


def _build_monthly_bench(months: list[date]):
    return pl.DataFrame([
        _constituent(name="HDFC Bank Ltd.", weight=50, month=m) for m in months
    ] + [
        _constituent(name="ICICI Bank Ltd.", weight=50, month=m) for m in months
    ])


def test_trailing_median_with_full_window():
    """12 monthly snapshots, each with Active Share = 10%. Median = 10%."""
    months = _trailing_months(date(2026, 4, 1), 12)
    fund = _build_monthly_fund(months, weight_a=60.0)   # |60-50|+|40-50| = 20 → AS=10
    bench = _build_monthly_bench(months)
    # as_of=2026-05-15: prior-month-floor is 2026-04-01, look back 12 months
    val = trailing_median_active_share(fund, bench, as_of=date(2026, 5, 15))
    assert val is not None
    assert abs(val - 10.0) < 1e-9


def test_trailing_median_below_min_snapshots_returns_none():
    """Only 2 monthly snapshots available; min_snapshots default is 3."""
    months = _trailing_months(date(2026, 4, 1), 2)
    fund = _build_monthly_fund(months)
    bench = _build_monthly_bench(months)
    assert trailing_median_active_share(fund, bench, as_of=date(2026, 5, 15)) is None


def test_trailing_median_works_with_exactly_min_snapshots():
    months = _trailing_months(date(2026, 4, 1), MIN_SNAPSHOTS_FOR_MEDIAN)
    fund = _build_monthly_fund(months, weight_a=60.0)
    bench = _build_monthly_bench(months)
    val = trailing_median_active_share(fund, bench, as_of=date(2026, 5, 15))
    assert val is not None
    assert abs(val - 10.0) < 1e-9


def test_trailing_median_ignores_months_outside_window():
    """A snapshot from 2 years ago must NOT count toward the trailing 12 median."""
    # 12 months in window with AS=10, plus an outlier 24 months back with AS=100
    months_in_window = _trailing_months(date(2026, 4, 1), 12)
    fund = _build_monthly_fund(months_in_window, weight_a=60.0)
    bench = _build_monthly_bench(months_in_window)
    # Add the outlier
    outlier_month = date(2024, 1, 1)
    extra_fund = pl.DataFrame([
        _holding(name="Disjoint Co Ltd.", weight=100, month=outlier_month),
    ])
    extra_bench = pl.DataFrame([
        _constituent(name="HDFC Bank Ltd.", weight=100, month=outlier_month),
    ])
    fund_all = pl.concat([fund, extra_fund], how="diagonal_relaxed")
    bench_all = pl.concat([bench, extra_bench], how="diagonal_relaxed")
    val = trailing_median_active_share(fund_all, bench_all, as_of=date(2026, 5, 15))
    assert abs(val - 10.0) < 1e-9  # outlier ignored


def test_trailing_median_handles_partial_window():
    """6 of 12 months have data → 6 snapshots → enough for median."""
    full = _trailing_months(date(2026, 4, 1), 12)
    months = full[-6:]  # last 6 months only
    fund = _build_monthly_fund(months, weight_a=60.0)
    bench = _build_monthly_bench(months)
    val = trailing_median_active_share(fund, bench, as_of=date(2026, 5, 15))
    assert val is not None
    assert abs(val - 10.0) < 1e-9


def test_trailing_median_picks_middle_of_varying_values():
    """5 monthly snapshots with Active Share values 5, 10, 15, 20, 25 → median = 15.

    For each month i (0..4), fund weight on INE_A is 50 + 5*(i+1), bench is 50/50.
    |(50+5(i+1))-50| + |(50-5(i+1))-50| = 10(i+1), divided by 2 → AS = 5*(i+1).
    """
    months = _trailing_months(date(2026, 4, 1), 5)
    rows = []
    bench_rows = []
    for i, m in enumerate(months):
        wa = 50 + 5 * (i + 1)
        rows.append(_holding(name="HDFC Bank Ltd.", weight=wa, month=m))
        rows.append(_holding(name="ICICI Bank Ltd.", weight=100 - wa, month=m))
        bench_rows.append(_constituent(name="HDFC Bank Ltd.", weight=50, month=m))
        bench_rows.append(_constituent(name="ICICI Bank Ltd.", weight=50, month=m))
    val = trailing_median_active_share(
        pl.DataFrame(rows), pl.DataFrame(bench_rows), as_of=date(2026, 5, 15),
    )
    assert val is not None
    assert abs(val - 15.0) < 1e-9
