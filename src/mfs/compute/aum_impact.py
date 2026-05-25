"""AUM Impact Cost compute layer (Phase 2.3).

Measures how many trading days the fund would need to liquidate its most
illiquid major positions. For each holding:

    days_to_exit = holding_value_INR / ADV_INR

where:
    holding_value_INR = (weight_pct / 100) × fund_AUM_INR
    ADV_INR          = trailing-60-day median of total_traded_value (INR) on NSE

We then take the **top 10 LEAST LIQUID holdings** (highest days_to_exit)
and report the MAX — i.e. the worst-case exit time for any major position.

Returns None when:
- The scheme's AUM is unknown
- No holdings have ISINs (factsheets often don't print ISINs; the join is
  via security_name → constituent ISIN, which only covers benchmark stocks)
- Fewer than 3 holdings have an associated ADV value

Stored in computed_metrics.aum_impact_cost_days (days, float).
"""

from __future__ import annotations

from typing import Optional

import polars as pl


# Minimum number of ADV-resolved holdings required to compute a meaningful
# AUM Impact Cost. Below this, return None — too little data.
MIN_HOLDINGS_FOR_IMPACT = 3

# How many "least liquid" holdings to consider before taking the max.
TOP_N_ILLIQUID = 10

# Trailing-day window for the ADV median.
ADV_WINDOW_DAYS = 60


def _median_adv(adv_history: pl.DataFrame, window_days: int = ADV_WINDOW_DAYS) -> dict[str, float]:
    """Return {isin: median_total_traded_value} over the trailing window.

    adv_history is expected to have columns: isin, date, total_traded_value.
    """
    if adv_history.is_empty():
        return {}
    end_date = adv_history["date"].max()
    if end_date is None:
        return {}
    from datetime import timedelta
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


def aum_impact_cost(
    holdings_df: pl.DataFrame,
    adv_by_isin: dict[str, float],
    aum_crore: float,
    *,
    top_n: int = TOP_N_ILLIQUID,
    min_holdings: int = MIN_HOLDINGS_FOR_IMPACT,
) -> Optional[float]:
    """Compute the max days-to-exit across the top-N least-liquid holdings.

    Args:
      holdings_df: latest holdings for one scheme. Expected columns:
        isin, weight_pct, instrument_type. Rows without isin are skipped.
      adv_by_isin: trailing-window median ADV in INR by ISIN.
      aum_crore: scheme AUM in Crore (1 Cr = 1e7 INR).
      top_n: how many illiquid holdings to inspect.
      min_holdings: minimum number of ISIN-resolved equity holdings required.

    Returns: max days-to-exit (float) or None.
    """
    if holdings_df.is_empty() or aum_crore <= 0 or not adv_by_isin:
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
    days_to_exit.sort(reverse=True)
    worst = days_to_exit[:top_n]
    return max(worst)
