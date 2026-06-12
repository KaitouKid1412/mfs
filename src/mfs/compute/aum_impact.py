"""AUM Impact Cost compute layer (Phase 2.3).

Reports the worst-case (max) days-to-exit across all ADV-resolved equity
holdings — i.e. how many trading days the fund would need to liquidate its
single most illiquid position. For each holding:

    days_to_exit = holding_value_INR / ADV_INR

where:
    holding_value_INR = (weight_pct / 100) × fund_AUM_INR
    ADV_INR          = trailing-60-day median of total_traded_value (INR) on NSE

ADV-coverage guard (A1-11): ``adv_unresolved_fraction`` measures the share of
total portfolio weight with no NSE EQ-series ADV match (missing/blank ISIN, or
an ISIN absent from the bhavcopy — e.g. international stocks, debt, cash).
When more than ``MAX_ADV_UNRESOLVED_FRAC`` of the portfolio weight is
unresolved, the metric is meaningless (it would describe a sliver of the book,
e.g. ICICI Pru US Bluechip computing liquidity over <5% of weight) and
``aum_impact_cost`` returns None. The fraction itself is always surfaced as
``computed_metrics.adv_unresolved_pct`` so stage 2 / reports can show why.

Returns None when:
- The scheme's AUM is unknown
- No holdings have ISINs with an associated ADV value
- Fewer than 3 holdings have an associated ADV value
- More than MAX_ADV_UNRESOLVED_FRAC of portfolio weight has no ADV match

Stored in computed_metrics.aum_impact_cost_days (days, float).
"""

from __future__ import annotations

from typing import Optional, cast

import polars as pl


# Minimum number of ADV-resolved holdings required to compute a meaningful
# AUM Impact Cost. Below this, return None — too little data.
MIN_HOLDINGS_FOR_IMPACT = 3

# A1-11: maximum fraction (0-1) of portfolio weight allowed to have no NSE
# EQ-series ADV match. Above this, aum_impact_cost returns None.
MAX_ADV_UNRESOLVED_FRAC = 0.20

# Trailing-day window for the ADV median.
ADV_WINDOW_DAYS = 60


def _median_adv(adv_history: pl.DataFrame, window_days: int = ADV_WINDOW_DAYS) -> dict[str, float]:
    """Return {isin: median_total_traded_value} over the trailing window.

    adv_history is expected to have columns: isin, date, total_traded_value.
    """
    if adv_history.is_empty():
        return {}
    end_date_raw = adv_history["date"].max()
    if end_date_raw is None:
        return {}
    from datetime import date, timedelta
    end_date = cast(date, end_date_raw)
    cutoff = end_date - timedelta(days=window_days)
    recent = adv_history.filter(pl.col("date") > cutoff)
    if recent.is_empty():
        return {}
    grouped = (
        recent
        .group_by("isin")
        .agg(pl.col("total_traded_value").median().alias("adv"))
    )
    return {r["isin"]: float(r["adv"]) for r in grouped.iter_rows(named=True)}


def adv_unresolved_fraction(
    holdings_df: pl.DataFrame,
    adv_by_isin: dict[str, float],
) -> Optional[float]:
    """Fraction (0-1) of total portfolio weight with no NSE EQ-series ADV match.

    A holding is unresolved when its ISIN is null/blank or maps to no positive
    ADV value. ``weight_pct`` is percent-of-portfolio, so the denominator is
    100; the result is clamped to 1.0 against slightly over-100 weight sums.
    Returns None when there are no holdings at all (nothing to measure).
    """
    if holdings_df.is_empty():
        return None
    unresolved_weight = 0.0
    for r in holdings_df.iter_rows(named=True):
        isin = r["isin"]
        adv = adv_by_isin.get(isin) if isin else None
        if not adv or adv <= 0:
            unresolved_weight += float(r["weight_pct"])
    return min(unresolved_weight / 100.0, 1.0)


def aum_impact_cost(
    holdings_df: pl.DataFrame,
    adv_by_isin: dict[str, float],
    aum_crore: float,
    *,
    min_holdings: int = MIN_HOLDINGS_FOR_IMPACT,
    max_unresolved_frac: float = MAX_ADV_UNRESOLVED_FRAC,
) -> Optional[float]:
    """Compute the worst-case (max) days-to-exit across all ADV-resolved
    equity holdings.

    Args:
      holdings_df: latest holdings for one scheme. Expected columns:
        isin, weight_pct, instrument_type. Rows without isin are skipped.
      adv_by_isin: trailing-window median ADV in INR by ISIN.
      aum_crore: scheme AUM in Crore (1 Cr = 1e7 INR).
      min_holdings: minimum number of ISIN-resolved equity holdings required.
      max_unresolved_frac: A1-11 ADV-coverage guard — when more than this
        fraction of total portfolio weight has no ADV match, return None.

    Returns: max days-to-exit (float) or None.
    """
    if holdings_df.is_empty() or aum_crore <= 0 or not adv_by_isin:
        return None
    # A1-11: refuse to report a days-to-exit computed over a sliver of the
    # portfolio (international/FoF-style books resolve almost nothing on NSE).
    unresolved = adv_unresolved_fraction(holdings_df, adv_by_isin)
    if unresolved is not None and unresolved > max_unresolved_frac:
        return None
    # Filter to equity rows with an ISIN we have an ADV for.
    eq = holdings_df.filter(
        (pl.col("instrument_type") == "Equity")
        & pl.col("isin").is_not_null()
        & (pl.col("isin") != "")
    )
    if eq.is_empty():
        return None

    aum_inr = aum_crore * 1e7  # Crore → INR
    days_to_exit: list[float] = []
    for r in eq.iter_rows(named=True):
        isin = r["isin"]
        adv = adv_by_isin.get(isin)
        if not adv or adv <= 0:
            continue
        holding_value = float(r["weight_pct"]) / 100.0 * aum_inr
        days_to_exit.append(holding_value / adv)

    if len(days_to_exit) < min_holdings:
        return None
    return max(days_to_exit)
