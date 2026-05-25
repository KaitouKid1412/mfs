"""Pairwise portfolio overlap math (Phase 3 Stage 3).

Computes the maximum overlap between two funds' equity portfolios as the
sum of `min(weight_A[s], weight_B[s])` across the union of holdings,
keyed first by ISIN (when both rows carry it — Phase 3.C-sourced) and
falling back to normalized security_name (Phase 2.2 factsheet rows).

This is a pure-function module: pass two holdings DataFrames, get an
overlap percentage and the top-shared securities. No DB I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from mfs.compute.active_share import normalize_name


@dataclass(frozen=True)
class OverlapResult:
    """Outcome of one pair's overlap computation."""

    overlap_pct: float | None
    n_overlapping_securities: int
    top_shared_holdings: list[tuple[str, float]]  # [(display_name, min_weight), ...]
    reason: str | None  # 'missing_holdings' or None when overlap is computed


def _normalize_keyed_weights(
    holdings_df: pl.DataFrame,
) -> tuple[dict[str, float], dict[str, str]]:
    """Filter to equity rows, renormalize weights to 100%, and key by ISIN
    when present else by normalized security_name. Returns:
        (key → weight_pct, key → display_security_name)

    The display map preserves the original printed name (best-effort: the
    first one seen for that key) so the caller can report which stocks
    are shared without re-normalizing for display.
    """
    if holdings_df.is_empty():
        return {}, {}
    eq = holdings_df.filter(
        (pl.col("instrument_type") == "Equity")
        & pl.col("security_name").is_not_null()
        & (pl.col("security_name") != "")
    )
    if eq.is_empty():
        return {}, {}
    total = float(eq["weight_pct"].sum())
    if total <= 0:
        return {}, {}

    weights: dict[str, float] = {}
    display: dict[str, str] = {}
    for r in eq.iter_rows(named=True):
        isin = r.get("isin")
        if isin:
            key = f"isin:{isin}"
        else:
            n = normalize_name(r["security_name"])
            if not n:
                continue
            key = f"name:{n}"
        weights[key] = weights.get(key, 0.0) + float(r["weight_pct"]) / total * 100.0
        # Keep the first display name we see (best-effort; both funds may
        # have slightly different printings of the same stock).
        display.setdefault(key, r["security_name"].strip())
    return weights, display


def pairwise_overlap(
    holdings_a: pl.DataFrame,
    holdings_b: pl.DataFrame,
    top_k_shared: int = 3,
) -> OverlapResult:
    """Compute pairwise overlap between two funds' holdings frames.

    Each holdings frame must have columns: ``security_name``, ``weight_pct``,
    ``instrument_type``, and optionally ``isin``. Both frames should be the
    most-recent-month snapshots for the respective funds; small staleness
    between A and B is acceptable (overlap is a structural similarity metric).

    Returns an OverlapResult. If either frame has no equity rows,
    ``overlap_pct`` is None and ``reason='missing_holdings'``.

    Math: overlap_pct = sum_over_keys min(weight_A[key], weight_B[key])
    where weights are renormalized to 100% on the equity sub-portfolio.
    Result range: [0, 100].
    """
    a_w, a_disp = _normalize_keyed_weights(holdings_a)
    b_w, b_disp = _normalize_keyed_weights(holdings_b)
    if not a_w or not b_w:
        return OverlapResult(
            overlap_pct=None,
            n_overlapping_securities=0,
            top_shared_holdings=[],
            reason="missing_holdings",
        )
    shared_keys = a_w.keys() & b_w.keys()
    per_share: list[tuple[str, float]] = []
    overlap = 0.0
    for k in shared_keys:
        m = min(a_w[k], b_w[k])
        overlap += m
        per_share.append((a_disp.get(k, b_disp.get(k, k)), m))
    per_share.sort(key=lambda t: -t[1])
    return OverlapResult(
        overlap_pct=round(overlap, 2),
        n_overlapping_securities=len(shared_keys),
        top_shared_holdings=per_share[:top_k_shared],
        reason=None,
    )


def format_top_shared(top: list[tuple[str, float]]) -> str:
    """Render top-shared holdings as a single human-readable cell value:
    ``"HDFC Bank Ltd. (5.2%), ICICI Bank Ltd. (4.1%), Reliance (3.0%)"``."""
    return ", ".join(f"{name} ({w:.1f}%)" for name, w in top)
