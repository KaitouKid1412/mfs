"""Active Share compute layer (Phase 2.2).

Active Share for one (fund, benchmark, month):
    Active Share = (1/2) Σ |w_fund_i − w_bench_i|

over the union of holdings i (in fund OR in benchmark).

Result is a percentage (0-100). High Active Share means the fund's portfolio
diverges materially from its benchmark; low means it's closet-indexing.

**Implementation note**: matching is ISIN-first (A1-8). In the Phase-2.2
factsheet-PDF era holdings carried no ISINs, so matching was name-only; that
is obsolete — post-Phase-5 SEBI-Excel ingestion both sides carry ISINs at
100% coverage (holdings_monthly and index_constituents_monthly, verified
live). Each row is keyed on `isin` when non-null/non-empty, else on the
normalized `security_name`; the two key kinds coexist in one weight dict, so
legacy/partial sources still interoperate via the name fallback.

Locked choices:
- **Equity-only filter**: only holdings whose `instrument_type` normalizes
  to 'Equity' contribute to the fund side. Derivatives, debt, REITs, cash
  are excluded — Active Share is by convention an equity-equity comparison.
- **Hybrid benchmarks = equity sleeve only**: for hybrid categories
  (Aggressive Hybrid, Balanced Advantage, Equity Savings) the benchmark is the
  NIFTY 50 equity sleeve and Active Share compares the fund's equity book
  against it, *both renormalized to 100%* — the standard Cremers–Petajisto
  equity-portfolio definition. The benchmark's debt sleeve is **intentionally
  excluded**: it has no decomposable per-security weights (NIFTY Composite Debt
  Index publishes none, and no tracker fund exists), and injecting an aggregate
  debt row would spuriously inflate Active Share because the fund's own debt
  holdings are already dropped by the equity-only filter (they would not
  cancel). The benchmark's fixed equity/debt split is recorded as metadata in
  the `composite_recipe` column of `configs/benchmarks.csv` for transparency.
- **ISIN-first match, normalized-name fallback**: rows with an ISIN match on
  it directly. Rows without one fall back to `normalize_name`, which
  lowercases, strips punctuation, canonicalizes '&' to 'and' (so "Larsen &
  Toubro" matches "Larsen and Toubro"), and removes Ltd./Limited/Corp
  suffixes so "HDFC Bank Ltd." matches "HDFC Bank Limited" across sources.
  Bare 'Co'/'Inc' tokens are intentionally NOT stripped (over-merge risk).
- **Trailing 1Y median**: per (scheme, as_of_compute_date), look at trailing
  12 monthly snapshots and report the median Active Share. Schemes with
  <3 usable snapshots in the window get a null result.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

import polars as pl


# Minimum number of monthly snapshots required for a meaningful median.
MIN_SNAPSHOTS_FOR_MEDIAN = 3

# Suffixes safe to strip for name matching. Bare 'co'/'inc' tokens are
# deliberately excluded (A1-8): stripping them over-merged distinct issuers.
_LTD_RE = re.compile(r"\b(ltd|limited|corp)\.?\b", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Normalize a security name for cross-source name matching (the fallback
    key for rows without an ISIN).

    'HDFC Bank Ltd.' / 'HDFC BANK LIMITED' / 'HDFC Bank Limited.' all → 'hdfc bank'.
    '&' canonicalizes to 'and' (before punctuation stripping) so
    'Larsen & Toubro' matches 'Larsen and Toubro'.
    """
    if not name:
        return ""
    s = name.replace("&", " and ")
    s = _LTD_RE.sub("", s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip().lower()
    return s


def _row_key(isin: object, security_name: object) -> str | None:
    """ISIN-first match key (A1-8): the ISIN when non-null/non-empty, else the
    normalized security name; None when neither is usable. ISIN keys are
    uppercased and name keys lowercased, so the two key kinds cannot collide
    in a shared dict."""
    if isinstance(isin, str) and isin.strip():
        return isin.strip().upper()
    if isinstance(security_name, str) and security_name:
        return normalize_name(security_name) or None
    return None


def _renormalize_on_key(df: pl.DataFrame) -> dict[str, float]:
    """Renormalize weight_pct to 100% over rows with a usable match key,
    accumulating on the ISIN-first key. Returns {} if nothing is keyable or
    total weight is non-positive."""
    keyed: list[tuple[str, float]] = []
    for r in df.iter_rows(named=True):
        key = _row_key(r.get("isin"), r.get("security_name"))
        if key is None:
            continue
        keyed.append((key, float(r["weight_pct"])))
    total = sum(wt for _, wt in keyed)
    if total <= 0:
        return {}
    out: dict[str, float] = {}
    for key, wt in keyed:
        out[key] = out.get(key, 0.0) + wt / total * 100.0
    return out


def _normalize_equity_weights(holdings_df: pl.DataFrame) -> dict[str, float]:
    """Filter to equity rows and renormalize weights to 100% on the ISIN-first
    match key (ISIN when present, else normalized security_name). Returns
    {key: weight_pct} summing to 100.0 (or empty dict if no equity rows)."""
    if holdings_df.is_empty():
        return {}
    eq = holdings_df.filter(pl.col("instrument_type") == "Equity")
    if eq.is_empty():
        return {}
    return _renormalize_on_key(eq)


def _normalize_constituent_weights(constituents_df: pl.DataFrame) -> dict[str, float]:
    """Renormalize benchmark constituent weights to 100% on the ISIN-first
    match key. Constituents are all equity by definition."""
    if constituents_df.is_empty():
        return {}
    return _renormalize_on_key(constituents_df)


def active_share_one_month(
    holdings: pl.DataFrame, constituents: pl.DataFrame
) -> Optional[float]:
    """Compute Active Share (0-100 in percent) for one (fund, benchmark) pair
    at one month. Returns None if either side has no equity holdings."""
    fund_w = _normalize_equity_weights(holdings)
    bench_w = _normalize_constituent_weights(constituents)
    if not fund_w or not bench_w:
        return None
    universe = set(fund_w) | set(bench_w)
    diff = sum(abs(fund_w.get(k, 0.0) - bench_w.get(k, 0.0)) for k in universe)
    return diff / 2.0


def _month_floor(d: date) -> date:
    return date(d.year, d.month, 1)


def _trailing_months(end_month: date, n: int) -> list[date]:
    """Return the n most recent month-first dates ending at end_month (inclusive)."""
    months: list[date] = []
    cur_y, cur_m = end_month.year, end_month.month
    for _ in range(n):
        months.append(date(cur_y, cur_m, 1))
        if cur_m == 1:
            cur_y -= 1
            cur_m = 12
        else:
            cur_m -= 1
    return sorted(months)


def trailing_median_active_share(
    holdings_df: pl.DataFrame,
    constituents_df: pl.DataFrame,
    as_of: date,
    *,
    window_months: int = 12,
    min_snapshots: int = MIN_SNAPSHOTS_FOR_MEDIAN,
) -> Optional[float]:
    """Compute the trailing N-month median Active Share for one scheme.

    Args:
      holdings_df: rows from holdings_monthly for one scheme (all months).
        Expected columns: as_of_month, security_name, weight_pct, instrument_type.
      constituents_df: rows from index_constituents_monthly for the scheme's
        benchmark. Expected columns: as_of_month, security_name, weight_pct.
      as_of: today. Window ends at the most recent month strictly before
        as_of's month (current month rarely has data — disclosures arrive
        on the 10th of the FOLLOWING month).
      window_months: number of monthly snapshots in the trailing window.
      min_snapshots: refuse to report median below this many usable monthly
        Active Share values.

    Returns: median Active Share in % (0-100), or None.
    """
    if holdings_df.is_empty() or constituents_df.is_empty():
        return None

    end = _month_floor(as_of) - timedelta(days=1)
    end = _month_floor(end)
    window = set(_trailing_months(end, window_months))

    fund_by_month = holdings_df.partition_by("as_of_month", as_dict=True)
    bench_by_month = constituents_df.partition_by("as_of_month", as_dict=True)

    per_month_values: list[float] = []
    for m in sorted(window):
        f = fund_by_month.get((m,))
        b = bench_by_month.get((m,))
        if f is None or b is None:
            continue
        val = active_share_one_month(f, b)
        if val is None:
            continue
        per_month_values.append(val)

    if len(per_month_values) < min_snapshots:
        return None
    per_month_values.sort()
    n = len(per_month_values)
    if n % 2:
        return per_month_values[n // 2]
    return 0.5 * (per_month_values[n // 2 - 1] + per_month_values[n // 2])
