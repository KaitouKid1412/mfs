"""Synthetic hybrid TRI construction.

NSE publishes hybrid indices (Nifty 50 Hybrid Composite Debt 65:35, 50:50, and
Nifty Equity Savings) but does not expose them via the public Backpage.aspx TRI
endpoint that we use for equity indices. The data is reachable only via NSE's
interactive ASP.NET WebForms UI (requires browser automation) or via paid feeds.

For v1 we synthesize approximations using:

  hybrid_daily_return_t = w_eq * nifty50_daily_log_return_t
                       + w_debt * rf_daily_log_return_t

with weights matching each index's stated composition. The result is then
exponentiated and cumulatively multiplied to produce a daily TRI close.

  - 65:35 → w_eq=0.65, w_debt=0.35
  - 50:50 → w_eq=0.50, w_debt=0.50
  - Equity Savings: ~30% directional equity + ~30% arbitrage (T-bill+spread) +
    ~40% debt → net ≈ 30% equity / 70% T-bill-like.

The approximation under-counts the arbitrage spread (typically 50-100 bp) and
the daily rebalancing drag. Expected tracking error: ~50-100 bp/year vs the
official NSE-published series. We persist these as standard benchmark_daily
partitions and tag them with `is_synthetic=True` so consumers can disclose the
methodology if needed.

ONE-SIDED BIAS — HYBRID ALPHA IS OVERSTATED (D6, PATH B decision 2026-06-13,
see docs/audit/hybrid_debt_sleeve_spike.md):

The debt sleeve here compounds the 91-day T-BILL rate, not a real bond index.
The official NSE hybrids use the NIFTY Composite Debt Index (long-duration
credit + G-sec), which has historically returned ~1-2%/yr MORE than T-bills
(term + credit premium). Substituting T-bills therefore makes the synthetic
benchmark systematically EASIER to beat — a one-sided, not symmetric, error:

  * ~35-140 bp/yr benchmark-too-easy at the 35-70% debt weights used above
    (35% sleeve × 1-2% premium ≈ 35-70 bp; 70% sleeve ≈ 70-140 bp).
  * Measured beat rates for hybrid categories (Balanced Advantage 100%,
    Aggressive Hybrid 96.7%, Equity Savings 82.6%) are inflated by this bias;
    hybrid fund alpha/IR vs these tickers reads correspondingly high.

NIFTY's fixed-income index values are not exposed via the public equity TRI
endpoint (Backpage.aspx/getTotalReturnIndexString) — until a scrapeable
fixed-income source passes the no-manual-entry invariant, this bias stands
and is DISCLOSED downstream: rank stage2/stage3 report rows and the D7
passive-alternative table carry `benchmark_is_synthetic=true` for these
tickers (see SYNTHETIC_BENCHMARK_TICKERS).
"""

from __future__ import annotations

import math
from datetime import date

import polars as pl

from mfs.db import queries as q
from mfs.db import writers as w
from mfs.utils.logging import get_logger

log = get_logger(__name__)

# (canonical_ticker, w_equity, w_debt_compounding)
SYNTHETIC_HYBRID_SPECS: list[tuple[str, float, float]] = [
    ("NIFTY 50 Hybrid 65:35 TRI", 0.65, 0.35),
    ("NIFTY 50 Hybrid 50:50 TRI", 0.50, 0.50),
    ("NIFTY Equity Savings TRI", 0.30, 0.70),
]

# D6: every benchmark ticker synthesized here (vs fetched from NSE). The rank
# layer joins `benchmark_is_synthetic` onto stage2/stage3 report rows and the
# passive-alternative table from this set, so the T-bill-sleeve bias above is
# disclosed wherever hybrid alpha is shown.
SYNTHETIC_BENCHMARK_TICKERS: frozenset[str] = frozenset(
    t for t, _, _ in SYNTHETIC_HYBRID_SPECS
)

BASE_INDEX_VALUE = 1000.0


def _ticker_slug(ticker: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9]+", "_", ticker).strip("_").lower()


def synthesize_one(
    ticker: str,
    w_equity: float,
    w_debt: float,
    *,
    start: date | None = None,
) -> int:
    """Build one synthetic hybrid TRI from Nifty 50 TRI + risk-free rate.

    Aligns the two series on common dates, computes a weighted daily log return,
    and produces a cumulative close-price series with `is_synthetic=True`.

    Returns the row count written.
    """
    if abs(w_equity + w_debt - 1.0) > 1e-9:
        raise ValueError(
            f"weights must sum to 1.0; got {w_equity} + {w_debt} = {w_equity + w_debt}"
        )

    nifty = q.benchmark_series("NIFTY 50 TRI")
    if nifty.is_empty():
        raise RuntimeError(
            "NIFTY 50 TRI missing in benchmark_daily — run `mfs ingest benchmarks` first."
        )
    nifty = nifty.sort("date").with_columns(
        (pl.col("close").log() - pl.col("close").shift(1).log()).alias("eq_log_ret")
    ).select(["date", "eq_log_ret"])

    rf = q.risk_free_series()
    if rf.is_empty():
        raise RuntimeError(
            "Risk-free daily series missing — run `mfs ingest tbill` first."
        )

    # Align on date intersection
    joined = (
        nifty.join(rf, on="date", how="inner")
        .with_columns(
            # rate_daily is a simple daily return; convert to log
            (pl.col("rate_daily") + 1.0).log().alias("rf_log_ret"),
        )
        .sort("date")
    )
    if start is not None:
        joined = joined.filter(pl.col("date") >= start)
    if joined.is_empty():
        log.warning("synthetic_hybrid.empty_after_align", ticker=ticker)
        return 0

    # Drop the first row's NaN equity return (no prior close to diff against)
    joined = joined.with_columns(
        pl.col("eq_log_ret").fill_null(0.0).alias("eq_log_ret")
    )

    weighted = joined.with_columns(
        (w_equity * pl.col("eq_log_ret") + w_debt * pl.col("rf_log_ret"))
        .alias("weighted_log_ret")
    )

    # Cumulative product → close series, scaled to start at BASE_INDEX_VALUE
    log_rets = weighted["weighted_log_ret"].to_numpy()
    cum = log_rets.cumsum()
    closes = BASE_INDEX_VALUE * pl.Series("close", [math.exp(x) for x in cum])

    out = pl.DataFrame(
        {
            "ticker": [ticker] * weighted.height,
            "date": weighted["date"],
            "close": closes,
            "is_synthetic": [True] * weighted.height,
        }
    ).with_columns(pl.col("date").cast(pl.Date))

    w.upsert_benchmark_daily(
        out.select(["ticker", "date", "close", "is_synthetic"])
    )
    log.info(
        "synthetic_hybrid.built",
        ticker=ticker,
        rows=out.height,
        w_eq=w_equity,
        w_debt=w_debt,
        start=str(out["date"].min()),
        end=str(out["date"].max()),
    )
    return out.height


def synthesize_all(start: date | None = None) -> dict[str, int]:
    """Build all three synthetic hybrid TRIs. Returns {ticker: row_count}."""
    out: dict[str, int] = {}
    for ticker, w_eq, w_debt in SYNTHETIC_HYBRID_SPECS:
        out[ticker] = synthesize_one(ticker, w_eq, w_debt, start=start)
    return out
