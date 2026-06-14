#!/usr/bin/env python
"""Build a self-contained, offline HTML dashboard for a ranking run.

Reads a run's Stage-1 (full per-category ranking) and Stage-2 (enriched top
picks: TER / AUM / PTR / active-share / flags) outputs from
``data/output/shortlist/<as_of>/`` and emits a single
``dashboard.html`` — no server, no CDN, no build step. Open it in any browser.

The page has two views (Top Picks = Stage 2, Full Ranking = Stage 1), a
category selector, a free-text filter, and click-to-sort columns (numeric-aware,
nulls last). All numbers are formatted to their real units. Honesty banners
carry the C1 backtest caveat (weights not validated as forward-predictive) and
the dormant-active-share note so the page can never overstate the ranking.

Usage:
    uv run python tools/build_dashboard.py                 # latest run
    uv run python tools/build_dashboard.py --as-of 2026-06-13
    uv run python tools/build_dashboard.py --open          # also open it
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import webbrowser
from pathlib import Path

from datetime import date

import polars as pl

from mfs.config import get_pipeline_config
from mfs.rank.score import STAGE1_WEIGHT_TO_COL, STAGE2_WEIGHT_TO_COL

SHORTLIST_ROOT = Path("data") / "output" / "shortlist"

# Non-weighted columns: their role in the ranking (shown under the heading in
# place of a weight). Anything not here and not weighted shows nothing.
COL_ROLE = {
    "composite_score": "Σ weighted z",
    "ter_pct": "tiebreaker",
    "ptr_latest": "soft penalty",
    "aum_crore": "display only",
    "alpha_confidence": "display only",
    "max_dd_3y_pct": "display only",
    "data_quality_flag": "display only",
    "top5_hits": "track record", "top5_pct": "track record",
    "top10_hits": "track record", "top10_pct": "track record",
}


def _col_weights(weights: dict, key_to_col: dict) -> dict:
    """Map a composite weights dict (keyed by weight-name) to {dashboard column
    key: weight}, via the weight→z-column mapping (the dashboard column key is
    the z-column with the 'z_' prefix stripped)."""
    out: dict[str, float] = {}
    for k, w in weights.items():
        zc = key_to_col.get(k)
        if zc and zc.startswith("z_"):
            out[zc[2:]] = float(w)
    return out


def _sub_label(col_key: str, wmap: dict) -> str:
    """Sub-heading text: the composite weight if the metric is scored, else its
    role (tiebreaker / penalty / display)."""
    if col_key in wmap:
        w = wmap[col_key]
        return f"wt {w:+.2f}" if w < 0 else f"wt {w:.2f}"
    return COL_ROLE.get(col_key, "")

# (key, header label, kind). kind drives JS formatting + sort type:
#   pct    -> fraction × 100, 2dp + '%'    (returns, alpha, PTR, active-share)
#   pctraw -> already percent, append '%'  (max-drawdown, TER)
#   ratio  -> 2dp decimal                  (sortino, IR, capture, R², beta, score)
#   cr     -> INR Crore, thousands-grouped
#   rank   -> integer rank
#   text   -> string
#   flags  -> array of short badges
STAGE2_COLS = [
    ("stage2_rank", "#", "rank"),
    ("scheme_name", "Fund", "text"),
    ("composite_score", "Score", "ratio"),
    ("top5_hits", "Top-5 hits", "int"),
    ("top5_pct", "Top-5 %", "pctraw"),
    ("top10_hits", "Top-10 hits", "int"),
    ("top10_pct", "Top-10 %", "pctraw"),
    ("ter_pct", "TER", "pctraw"),
    ("aum_crore", "AUM (Cr)", "cr"),
    ("ret_3y_median", "Ret 3y", "pct"),
    ("ret_3y_p25", "Ret 3y p25", "pct"),
    ("ret_5y_median", "Ret 5y", "pct"),
    ("ret_5y_p25", "Ret 5y p25", "pct"),
    ("alpha_3y_annualized", "Alpha 3y", "pct"),
    ("alpha_confidence", "α conf", "ratio"),
    ("sortino_3y", "Sortino", "ratio"),
    ("info_ratio_3y", "Info Ratio", "ratio"),
    ("capture_efficiency", "Capture", "ratio"),
    ("r_squared_3y", "R²", "ratio"),
    ("beta_3y", "Beta", "ratio"),
    ("max_dd_3y_pct", "Max DD 3y", "pctraw"),
    ("ptr_latest", "PTR", "pct"),
    ("active_share_median_1y", "Active Share", "pct"),
    ("flags", "Flags", "flags"),
]

STAGE1_COLS = [
    ("rank", "#", "rank"),
    ("scheme_name", "Fund", "text"),
    ("composite_score", "Score", "ratio"),
    ("top5_hits", "Top-5 hits", "int"),
    ("top5_pct", "Top-5 %", "pctraw"),
    ("top10_hits", "Top-10 hits", "int"),
    ("top10_pct", "Top-10 %", "pctraw"),
    ("ret_3y_median", "Ret 3y", "pct"),
    ("ret_3y_p25", "Ret 3y p25", "pct"),
    ("ret_5y_median", "Ret 5y", "pct"),
    ("ret_5y_p25", "Ret 5y p25", "pct"),
    ("alpha_3y_annualized", "Alpha 3y", "pct"),
    ("alpha_confidence", "α conf", "ratio"),
    ("sortino_3y", "Sortino", "ratio"),
    ("info_ratio_3y", "Info Ratio", "ratio"),
    ("capture_efficiency", "Capture", "ratio"),
    ("r_squared_3y", "R²", "ratio"),
    ("beta_3y", "Beta", "ratio"),
    ("max_dd_3y_pct", "Max DD 3y", "pctraw"),
    ("data_quality_flag", "Quality", "text"),
]

# "Active vs Index" tab — one row per category (not per fund).
AVI_COLS = [
    ("canonical_category", "Category", "text"),
    ("n3", "n (3y record)", "int"),
    ("idx_3y", "Index 3y", "pct"),
    ("fund_med_3y", "Median fund 3y", "pct"),
    ("spread_3y", "Fund p25→p75 (3y)", "range"),
    ("beat_3y", "% beat (3y)", "pctraw"),
    ("beat_5y", "% beat (5y)", "pctraw"),
    ("excess_3y", "Median excess (3y)", "pct"),
    ("verdict", "Active vs Index", "verdict"),
]


# Plain-English, one-line explanation per column (shown on hover + in the
# on-screen "what the columns mean" panel).
TIPS = {
    "rank": "Rank within the category (1 = best on our score).",
    "stage2_rank": "Rank within the category (1 = best on our score).",
    "scheme_name": "Fund name — Direct plan, Growth option.",
    "composite_score": "Overall ranking score — combines every metric here. Higher = ranked higher.",
    "top5_hits": "Shown as hits / eligible-quarters (e.g. 28 / 33): of the back-tested quarters since 2016 where this fund's category had a meaningful top-5, how many it ranked in the top-5 on the core composite. Higher = more consistently strong; the denominator differs per fund (newer funds have fewer quarters — check Top-5 % for a fair rate). Survivorship-biased; '—' for always-small categories.",
    "top5_pct": "Share of its back-tested quarters the fund was top-5 (a hit-rate — fairer to younger funds than the raw count).",
    "top10_hits": "Like Top-5 hits but for the top-10 — shown as hits / eligible-quarters. Counted only in quarters where the category had MORE than 10 funds, so its denominator is smaller than Top-5's, and it's '—' for categories that rarely exceeded 10 funds.",
    "top10_pct": "Share of its top-10-eligible quarters the fund ranked in the top-10 (hit-rate).",
    "ter_pct": "Annual fee (expense ratio). Lower is better.",
    "aum_crore": "Fund size, in ₹ crore.",
    "ret_3y_median": "Typical annual return over the last 3 years.",
    "ret_5y_median": "Typical annual return over the last 5 years.",
    "ret_3y_p25": "A cautious 'bad-spell' version of the 3-year return (lower-quartile of rolling periods).",
    "ret_5y_p25": "A cautious 'bad-spell' version of the 5-year return (lower-quartile of rolling periods).",
    "alpha_3y_annualized": "Return above what the benchmark index explains — the manager's yearly value-add. Higher is better (trust it only when R² is high).",
    "alpha_confidence": "How statistically reliable the Alpha figure is, 0–1. Higher = more reliable.",
    "sortino_3y": "Return earned per unit of downside risk. Higher is better.",
    "info_ratio_3y": "How consistently the fund beats its benchmark. Higher is better.",
    "capture_efficiency": "Gains kept in up-markets vs losses taken in down-markets. Above 1 is good.",
    "r_squared_3y": "How closely the fund tracks its benchmark, 0–1. If low, the Alpha number isn't trustworthy.",
    "beta_3y": "Market sensitivity. ~1 moves with the index; below 1 is calmer, above 1 is racier.",
    "max_dd_3y_pct": "Worst peak-to-trough fall in the last 3 years. Smaller is better.",
    "ptr_latest": "Portfolio turnover — how much the manager trades per year. Very high can add hidden cost.",
    "active_share_median_1y": "How different the holdings are from the index (high = genuinely active). Blank until ~10 Jul 2026.",
    "data_quality_flag": "Data-quality status for this fund's metrics.",
    "flags": "Caveat tags — hover each badge for what it means.",
    # Active-vs-Index tab
    "canonical_category": "The fund category.",
    "n3": "Funds in the category with a 3-year track record (the basis for the 3-year stats).",
    "idx_3y": "The index (benchmark TRI) median rolling 3-year CAGR — what a near-costless index fund would have tracked.",
    "fund_med_3y": "The category's median fund's 3-year CAGR (net of fees).",
    "spread_3y": "Middle-50% spread of fund 3-year returns (p25→p75). Wider = it matters more which fund you pick. Sort by this for picking risk.",
    "beat_3y": "Share of the category's funds whose 3-year return beat the index. ~50% = a coin flip.",
    "beat_5y": "Share of funds whose 5-year return beat the index.",
    "excess_3y": "Median fund's 3-year CAGR minus the index.",
    "verdict": "Rough active-vs-index call from the hit-rate + dispersion. Survivorship-biased (failed funds excluded), so reality is a bit worse for active; hybrids use a synthetic benchmark.",
}

# One-line, plain-English description of each fund category (shown on hover —
# on the category dropdown and the Category column).
CATEGORY_DESC = {
    "Large Cap": "Invests mainly in India's ~100 largest companies (SEBI: ≥80% large-caps) — relatively stable blue-chips.",
    "Large & Mid Cap": "Holds both large-caps and mid-caps (≥35% each) — balances stability and growth.",
    "Mid Cap": "Mainly mid-sized companies (ranks 101–250 by market cap; ≥65% mid-caps) — more growth and volatility.",
    "Small Cap": "Mainly small companies (rank 251+; ≥65% small-caps) — highest growth potential and risk.",
    "Multi Cap": "Spreads across large, mid and small caps with ≥25% in each — diversified across the size spectrum.",
    "Flexi Cap": "Invests across any market-cap with no fixed split — the manager moves freely between large/mid/small.",
    "Focused": "A concentrated portfolio of at most 30 stocks — high conviction, higher single-stock risk.",
    "ELSS": "Tax-saving equity fund (Section 80C) with a 3-year lock-in; invests across market caps.",
    "Value": "Buys undervalued / out-of-favour stocks expecting them to re-rate — a value tilt.",
    "Contra": "Contrarian strategy — buys beaten-down stocks the market dislikes, betting on a turnaround.",
    "Dividend Yield": "Focuses on high-dividend-paying companies — income plus modest growth.",
    "Aggressive Hybrid": "Equity-heavy hybrid (~65–80% equity, rest debt) — growth with a debt cushion.",
    "Balanced Advantage": "Dynamically shifts between equity and debt based on market valuations — a smoother ride.",
    "Equity Savings": "Conservative hybrid of equity + arbitrage + debt (~30–40% net equity) — lower volatility, equity taxation.",
    "Banking & Financial Services": "Sector fund investing in banks, NBFCs and financial-services companies.",
    "IT": "Sector fund investing in information-technology / software companies.",
    "Pharma & Healthcare": "Sector fund investing in pharmaceutical and healthcare companies.",
    "FMCG": "Sector fund investing in fast-moving consumer-goods companies.",
    "Auto": "Sector fund investing in automobile and auto-component companies.",
    "Energy": "Thematic fund investing in energy / natural-resources companies (power, oil & gas, etc.).",
    "Infrastructure": "Thematic fund investing in infrastructure — construction, capital goods, power, transport.",
    "PSU": "Invests in public-sector / government-owned enterprises.",
    "Consumption": "Thematic fund investing in consumption-driven businesses (consumer goods, retail, autos, etc.).",
    "MNC": "Invests in Indian-listed multinational companies.",
    "ESG": "Invests using environmental, social and governance (ESG) screens.",
    "Manufacturing": "Thematic fund investing in manufacturing / 'Make in India' companies.",
    "Thematic": "Broad thematic/sectoral funds with varied mandates that don't map to a single standard theme.",
}

# Why a cell may be empty ("—"), per column — shown on hover of the dash.
MISSING = {
    "ter_pct": "No TER match — this fund's name didn't match AMFI's monthly TER disclosure (~9% of funds don't), or it's outside the scored pool.",
    "aum_crore": "Fund size not available from AMFI's quarterly AAUM data for this scheme.",
    "ptr_latest": "Portfolio turnover not disclosed — its AMC's factsheet wasn't parsed this month, or it reports turnover only annually.",
    "active_share_median_1y": "Not computed yet — Active Share needs 3 monthly holdings + index snapshots and turns on ~10 Jul 2026 (not a fund problem).",
    "top5_hits": "No back-tested top-5 history — its category never had more than 5 funds (so 'top-5' isn't a real cut), or the fund is too new to have a 3-year track record in any back-tested quarter.",
    "top5_pct": "No eligible back-tested quarter for a top-5 cut in this fund's category (see Top-5 hits).",
    "top10_hits": "No back-tested top-10 history — its category rarely had more than 10 funds, so a 'top-10' cut isn't meaningful for it.",
    "top10_pct": "No eligible back-tested quarter for a top-10 cut in this fund's category (see Top-10 hits).",
    "alpha_confidence": "Alpha-confidence unavailable — alpha couldn't be reliably estimated for this fund.",
}

# Plain-English meaning of each flag badge (shown in the on-screen panel).
FLAG_GLOSSARY = [
    {"t": "partial", "h": "Some disclosure data for this fund (e.g. turnover or liquidity) is missing."},
    {"t": "thin cohort", "h": "Fewer than 5 funds qualified in this category, so the shortlist is thin."},
    {"t": "low R²", "h": "The fund doesn't track its benchmark closely, so its Alpha is unreliable."},
    {"t": "synth-bm", "h": "This hybrid category is measured against a stand-in benchmark, so its Alpha is likely overstated."},
    {"t": "liq:HIGH / SEVERE", "h": "Could take a while to sell out of without moving the price (liquidity risk)."},
]


def _clean(v):
    """JSON-safe scalar: NaN/inf -> None (so the JS renders an em-dash)."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _flags(row: dict) -> list[dict]:
    """Short badges for a Stage-2 row (label + css class + hover title)."""
    out: list[dict] = []
    # 'partial' only for a REAL disclosure gap. active_share is dormant
    # universe-wide by design (explained in the banner), so a fund whose only
    # missing field is active_share is NOT partially disclosed — badging it
    # would fire on ~88% of picks and bury the few with a genuine extra gap.
    miss = [m for m in (row.get("missing_disclosures") or "").split(";") if m]
    real_miss = [m for m in miss if m != "active_share_median_1y"]
    if real_miss:
        out.append({"t": "partial", "c": "warn", "h": "missing: " + ", ".join(real_miss)})
    if row.get("partial_coverage_flag"):
        out.append({"t": "thin cohort", "c": "warn",
                    "h": "fewer than 5 funds survived this category's Stage-2 pool"})
    if row.get("benchmark_fit_low_confidence"):
        out.append({"t": "low R²", "c": "warn",
                    "h": "benchmark explains <80% of returns — alpha is low-confidence"})
    if row.get("benchmark_is_synthetic"):
        out.append({"t": "synth-bm", "c": "warn",
                    "h": "synthetic hybrid benchmark (T-bill debt sleeve) — alpha overstated"})
    lq = row.get("liquidity_flag")
    if lq in ("HIGH", "SEVERE"):
        out.append({"t": f"liq:{lq}", "c": "bad" if lq == "SEVERE" else "warn",
                    "h": "days-to-exit liquidity tier"})
    return out


def _rows(df: pl.DataFrame, cols: list[tuple], with_flags: bool,
          persist: dict) -> list[dict]:
    keys = [k for k, _, kind in cols if kind != "flags"]
    present = [k for k in keys if k in df.columns]
    out = []
    for r in df.iter_rows(named=True):
        rec = {k: _clean(r.get(k)) for k in present}
        rec["canonical_category"] = _clean(r.get("canonical_category"))  # for the All-categories view
        # top-5 persistence is sourced from the backtest, not the run parquet.
        p = persist.get(r.get("scheme_code"))
        rec["top5_hits"] = p["hits5"] if p else None
        rec["top5_pct"] = p["pct5"] if p else None
        rec["top5_q"] = p["q5"] if p else None  # top-5 eligible-quarter denominator
        # top-10 has its own (smaller) denominator: only quarters where the
        # category had >10 funds. None when the category never exceeded 10.
        has10 = bool(p and p["q10"])
        rec["top10_hits"] = p["hits10"] if has10 else None
        rec["top10_pct"] = p["pct10"] if has10 else None
        rec["top10_q"] = p["q10"] if has10 else None
        if with_flags:
            rec["flags"] = _flags(r)
        out.append(rec)
    return out


def _load_cat_ic() -> dict:
    """Per-category 1y mean Spearman IC from the most recent retro-IC backtest
    (``data/output/backtest/<date>/ic_summary.csv``), if any. Drives a
    per-category honesty caveat: categories whose composite had a NEGATIVE 1y
    IC are anti-predictive (top picks historically underperformed the median).
    Returns {} when no backtest artifact exists (graceful degrade)."""
    import csv

    cands = sorted(glob.glob("data/output/backtest/*/ic_summary.csv"))
    if not cands:
        return {}
    out: dict[str, float] = {}
    try:
        with open(cands[-1], newline="") as f:
            for row in csv.DictReader(f):
                if row.get("horizon") == "1y" and row.get("canonical_category") not in (None, "", "ALL"):
                    try:
                        out[row["canonical_category"]] = round(float(row["mean_ic"]), 4)
                    except (ValueError, TypeError):
                        pass
    except OSError:
        return {}
    return out


def _load_top5_persistence() -> dict:
    """Per-scheme top-5 persistence from the most recent backtest
    (``data/output/backtest/<date>/top5_persistence.csv``): how many quarters
    since 2016 the fund ranked in its category's top-5 (on the core Stage-1
    composite, in quarters where the category had >5 funds). Returns {} if
    absent. Keyed by scheme_code -> {hits, pct, q}."""
    import csv

    cands = sorted(glob.glob("data/output/backtest/*/top5_persistence.csv"))
    if not cands:
        return {}
    out: dict[str, dict] = {}
    try:
        with open(cands[-1], newline="") as f:
            for row in csv.DictReader(f):
                try:
                    out[row["scheme_code"]] = {
                        "hits5": int(row["n_top5"]),
                        "pct5": float(row["top5_pct"]),
                        "q5": int(row["n_quarters_eligible_top5"]),
                        "hits10": int(row["n_top10"]),
                        "pct10": float(row["top10_pct"]),
                        "q10": int(row["n_quarters_eligible_top10"]),
                    }
                except (ValueError, TypeError, KeyError):
                    pass
    except OSError:
        return {}
    return out


def _verdict(beat3, n3: int, synthetic: bool) -> tuple[str, str]:
    """Active-vs-index call from the 3y hit-rate + sample/quality, with a css class."""
    if synthetic:
        return ("n/a — synthetic benchmark", "mut")
    if n3 < 8:
        return ("too few funds", "mut")
    if beat3 is None:
        return ("—", "mut")
    if beat3 >= 75:
        return ("active edge", "pos")
    if beat3 >= 60:
        return ("lean active", "pos")
    if beat3 >= 45:
        return ("coin-flip → index", "wk")
    return ("index wins", "neg")


def _load_active_vs_index(as_of: str) -> list[dict]:
    """Per-category active-vs-index comparison: index CAGR, the category fund
    return distribution (median + p25/p75 spread), the share of funds beating
    the index (3y & 5y), and a verdict. Benchmark medians come from the run's
    passive_alternative.csv; the fund dispersion is computed from the full
    computed_metrics population (the same basis passive.py uses). Returns []
    (tab hidden) if either source is unavailable."""
    try:
        from mfs.db import queries as q

        pcsv = SHORTLIST_ROOT / as_of / "passive_alternative.csv"
        if not pcsv.exists():
            return []
        pa = pl.read_csv(pcsv, comment_prefix="#")
        bench = {r["canonical_category"]: r for r in pa.iter_rows(named=True)}
        m = q.computed_metrics_at(date.fromisoformat(as_of))
        if m.is_empty():
            return []
        m = m.select(["canonical_category", "ret_3y_median", "ret_5y_median"])
        out: list[dict] = []
        for cat_t, g in m.group_by("canonical_category"):
            cat = cat_t[0]
            b = bench.get(cat)
            if not b:
                continue
            b3, b5 = b["benchmark_ret_3y_median"], b["benchmark_ret_5y_median"]
            r3 = g["ret_3y_median"].drop_nulls()
            if r3.len() < 4 or b3 is None:
                continue
            p25, p50, p75 = (float(r3.quantile(0.25)), float(r3.median()), float(r3.quantile(0.75)))
            beat3 = round(float((r3 > b3).sum()) / r3.len() * 100.0)
            r5 = g["ret_5y_median"].drop_nulls()
            beat5 = round(float((r5 > b5).sum()) / r5.len() * 100.0) if (b5 is not None and r5.len()) else None
            syn = bool(b["benchmark_is_synthetic"])
            vt, vc = _verdict(beat3, r3.len(), syn)
            out.append({
                "canonical_category": cat, "n3": r3.len(),
                "idx_3y": b3, "fund_med_3y": p50,
                "p25_3y": p25, "p75_3y": p75, "spread_3y": p75 - p25,
                "beat_3y": beat3, "beat_5y": beat5,
                "excess_3y": p50 - b3, "verdict": vt, "verdict_c": vc,
                "synthetic": syn,
            })
        out.sort(key=lambda r: (r["beat_3y"] if r["beat_3y"] is not None else -1), reverse=True)
        return out
    except Exception:  # noqa: BLE001 — tab is best-effort; degrade to hidden
        return []


def collect(as_of: str) -> dict:
    run = SHORTLIST_ROOT / as_of
    if not run.exists():
        raise SystemExit(f"No run at {run}")

    def _read(globpat: str) -> list[Path]:
        return [Path(p) for p in sorted(glob.glob(str(run / globpat)))
                if "mf_report" not in os.path.basename(p)
                and "dropped" not in os.path.basename(p)
                and "excluded" not in os.path.basename(p)]

    s1 = pl.concat([pl.read_parquet(p) for p in _read("stage1/*.parquet")],
                   how="diagonal_relaxed") if _read("stage1/*.parquet") else pl.DataFrame()
    s2_files = _read("stage2/*.parquet")
    s2 = (pl.concat([pl.read_parquet(p) for p in s2_files], how="diagonal_relaxed")
          if s2_files else pl.DataFrame())

    persist = _load_top5_persistence()
    stage1: dict[str, list] = {}
    stage2: dict[str, list] = {}
    cats: set[str] = set()
    if not s1.is_empty():
        for cat, g in s1.group_by("canonical_category"):
            c = cat[0]
            cats.add(c)
            stage1[c] = _rows(g.sort("rank"), STAGE1_COLS, with_flags=False, persist=persist)
    if not s2.is_empty():
        for cat, g in s2.group_by("canonical_category"):
            c = cat[0]
            cats.add(c)
            stage2[c] = _rows(g.sort("stage2_rank"), STAGE2_COLS, with_flags=True, persist=persist)

    cat_list = sorted(cats)
    counts = {c: {"s1": len(stage1.get(c, [])), "s2": len(stage2.get(c, []))}
              for c in cat_list}
    cfg = get_pipeline_config()
    w1 = _col_weights(dict(cfg.composite_weights_stage1), STAGE1_WEIGHT_TO_COL)
    w2 = _col_weights(dict(cfg.composite_weights_stage2), STAGE2_WEIGHT_TO_COL)
    return {
        "as_of": as_of,
        "categories": cat_list,
        "counts": counts,
        "cat_ic": _load_cat_ic(),
        "cat_desc": CATEGORY_DESC,
        "missing": MISSING,
        "flag_glossary": FLAG_GLOSSARY,
        "n_s1": sum(len(v) for v in stage1.values()),
        "n_s2": sum(len(v) for v in stage2.values()),
        "stage1_cols": [{"k": k, "l": l, "kind": kind, "tip": TIPS.get(k, ""), "sub": _sub_label(k, w1)} for k, l, kind in STAGE1_COLS],
        "stage2_cols": [{"k": k, "l": l, "kind": kind, "tip": TIPS.get(k, ""), "sub": _sub_label(k, w2)} for k, l, kind in STAGE2_COLS],
        "avi_cols": [{"k": k, "l": l, "kind": kind, "tip": TIPS.get(k, ""), "sub": ""} for k, l, kind in AVI_COLS],
        "avi": _load_active_vs_index(as_of),
        "stage1": stage1,
        "stage2": stage2,
    }


def render(payload: dict) -> str:
    blob = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    return _TEMPLATE.replace("/*__DATA__*/", blob)


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>MF Rankings — __TITLE__</title>
<style>
  :root{--bg:#0f1419;--panel:#171d26;--line:#2a3340;--fg:#e6edf3;--mut:#8b98a9;
        --accent:#4493f8;--good:#3fb950;--bad:#f85149;--warn:#d29922;--head:#1e2630;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  header{padding:14px 18px;border-bottom:1px solid var(--line);background:var(--panel);
         position:sticky;top:0;z-index:5}
  h1{margin:0 0 2px;font-size:17px}
  .sub{color:var(--mut);font-size:12px}
  .banner{margin:10px 0 0;padding:9px 12px;border-radius:6px;font-size:12.5px;line-height:1.5;
          background:#16202b;border:1px solid #243240;color:#cdd9e5;max-width:1100px}
  .banner b{color:#e6edf3}
  .banner .note{display:block;margin-top:6px;color:var(--mut);font-size:11.5px}
  details{margin-top:6px}
  details summary{cursor:pointer;color:var(--accent);font-size:11.5px;outline:none}
  details .body{margin-top:5px;padding:7px 10px;background:var(--head);border-radius:5px;
                color:var(--mut);font-size:11.5px;line-height:1.55}
  .tech{color:var(--mut);font-size:11px}
  .hlink{color:var(--accent);text-decoration:none;font-size:11.5px;white-space:nowrap}
  .help{padding:8px 18px 40px;max-width:1100px}
  .help details{margin-top:6px}
  #tip{position:fixed;z-index:50;max-width:340px;background:#0b0f14;color:#e6edf3;
       border:1px solid var(--line);border-radius:6px;padding:7px 10px;font-size:12px;
       line-height:1.45;box-shadow:0 6px 24px rgba(0,0,0,.55);display:none;pointer-events:none}
  .controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:12px}
  select,input{background:var(--head);color:var(--fg);border:1px solid var(--line);
               border-radius:6px;padding:6px 9px;font-size:13px}
  input{min-width:240px}
  .toggle{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
  .toggle button{background:var(--head);color:var(--mut);border:0;padding:6px 14px;
                 cursor:pointer;font-size:13px}
  .toggle button.on{background:var(--accent);color:#fff}
  .meta{color:var(--mut);font-size:12px;margin-left:auto}
  .catwarn{margin-top:9px;padding:7px 11px;border-radius:6px;font-size:12px;display:none}
  .catwarn.neg{background:#2a1311;border:1px solid #5a1d1a;color:#f0a39c}
  .catwarn.wk{background:#241f0d;border:1px solid #5a4412;color:#e8cf86}
  .catwarn.pos{background:#10231a;border:1px solid #1f5133;color:#8fe3b0}
  .avinote{margin-top:9px;padding:8px 12px;border-radius:6px;font-size:12px;display:none;line-height:1.5;
           background:#16202b;border:1px solid #243240;color:#cdd9e5;max-width:1100px}
  .vb{font-weight:600}
  .vb.pos{color:var(--good)} .vb.wk{color:var(--warn)} .vb.neg{color:var(--bad)} .vb.mut{color:var(--mut)}
  .scroll{overflow:auto}              /* ONLY the table scrolls (both axes), not the page */
  table{border-collapse:collapse;min-width:100%;font-variant-numeric:tabular-nums}
  thead th{position:sticky;top:0;background:var(--head);border-bottom:2px solid var(--line);
           padding:8px 10px;text-align:right;cursor:pointer;white-space:nowrap;user-select:none}
  thead th:first-child,thead th.lft{text-align:left}
  thead th:hover{color:var(--accent)}
  thead th.tip{text-decoration:underline dotted rgba(139,152,169,.6);text-underline-offset:4px}
  th .arr{color:var(--accent);font-size:10px}
  th .wt{font-size:10px;color:var(--mut);font-weight:400;margin-top:2px;letter-spacing:.2px}
  td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
  td.lft{text-align:left;max-width:360px;overflow:hidden;text-overflow:ellipsis}
  tbody tr:hover{background:#1b222c}
  td.pos{color:var(--good)} td.neg{color:var(--bad)} .mut{color:var(--mut)}
  .badge{display:inline-block;padding:1px 6px;border-radius:10px;font-size:10.5px;
         margin:0 3px 0 0;border:1px solid}
  .badge.warn{color:var(--warn);border-color:#5a4412;background:#2a210f}
  .badge.bad{color:var(--bad);border-color:#5a1d1a;background:#2a1311}
  .rk{color:var(--mut);font-weight:600}
  .empty{padding:40px;text-align:center;color:var(--mut)}
  footer{padding:14px 18px;color:var(--mut);font-size:11.5px;border-top:1px solid var(--line)}
  code{background:var(--head);padding:1px 5px;border-radius:4px}
</style>
</head>
<body>
<header>
  <h1>Mutual-fund rankings <span class="mut" id="asof"></span></h1>
  <div class="sub">Indian equity &amp; hybrid funds (Direct-Growth), ranked within each category — a quality shortlist, <b>not</b> a prediction. Click a heading to sort, type to filter. <a class="hlink" href="#help">How to read this &amp; how the Score works ↓</a></div>
  <div class="controls">
    <div class="toggle">
      <button id="btnS2" class="on" onclick="setView('stage2')">Top picks</button>
      <button id="btnS1" onclick="setView('stage1')">Full ranking</button>
      <button id="btnAVI" onclick="setView('avi')">Active vs Index</button>
    </div>
    <select id="cat" onchange="render()"></select>
    <input id="q" type="search" placeholder="Filter funds… (name)" oninput="render()"/>
    <span class="meta" id="meta"></span>
  </div>
  <div class="catwarn" id="catwarn"></div>
  <div class="avinote" id="avinote"></div>
</header>
<div class="scroll" id="scroll"><table id="tbl"><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table></div>
<div class="empty" id="empty" style="display:none">No funds match.</div>
<div id="tip"></div>
<footer id="foot"></footer>
<section class="help" id="help">
  <details><summary>How to read this</summary>
    <div class="body">Funds are ranked within each category on past <b>cost, consistency and risk-adjusted returns</b> — a quality shortlist, <b>not</b> a prediction. Our 2016→ backtest found a high rank did <b>not</b> reliably beat going forward (rank-vs-future correlation ≈ 0, survivorship-biased) — so use it to narrow the field, then dig deeper. “Active Share” is blank until ~10 Jul 2026 (data not ready, not a fund problem). Hover any column heading for its meaning; the grey line under it is its <b>weight in the Score</b>.</div>
  </details>
  <details><summary>How the Score is calculated</summary>
    <div class="body"><b>1.</b> Each metric → a within-category <b>z-score</b> = (fund value − category average) ÷ category std-dev, capped ±3 (puts %s and ratios on one scale). <b>2.</b> Score = weighted sum of those z-scores (weights under each heading; Full-ranking sums to 1.0; higher = better; a fund with no 5-yr history reuses its 3-yr figure). <b>3.</b> Top-picks also adds Active Share, a style-drift penalty and PTR/liquidity penalties; TER only breaks ties. Scores are category-relative — compare <b>within</b> a category, not across.</div>
  </details>
</section>
<script>
const D = /*__DATA__*/;
let view='stage1', sortKey='composite_score', sortDir=-1;  // open on all funds, sorted by score

function colsFor(v){return v==='stage2'?D.stage2_cols:D.stage1_cols;}
function dataFor(v){return v==='stage2'?D.stage2:D.stage1;}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

function fmt(val,kind){
  if(val===null||val===undefined||val==='') return ['<span class="mut">—</span>','',null];
  if(kind==='pct'){const n=val*100;return [n.toFixed(2)+'%','',n];}
  if(kind==='pctraw'){const n=+val;return [n.toFixed(2)+'%','',n];}
  if(kind==='ratio'){const n=+val;return [n.toFixed(2),'',n];}
  if(kind==='cr'){const n=+val;return [n.toLocaleString('en-IN',{maximumFractionDigits:0}),'',n];}
  if(kind==='int'){return [String(+val),'',+val];}
  if(kind==='rank'){return [String(+val),'rk',+val];}
  if(kind==='text'){const s=String(val);return [esc(s),'',s.toLowerCase()];}
  return [esc(String(val)),'',val];
}
function flagHtml(arr){if(!arr||!arr.length)return '<span class="mut">—</span>';
  return arr.map(f=>`<span class="badge ${f.c}" data-tip="${esc(f.h||'')}">${esc(f.t)}</span>`).join('');}
// A "—" cell that explains, on hover, WHY it has no data (per column).
function miss(k){
  const why=(D.missing&&D.missing[k])||'Not available for this fund (insufficient history or data).';
  return `<span class="mut" data-tip="${esc(why)}">—</span>`;
}

function initCats(){
  const sel=document.getElementById('cat');
  const total=view==='stage2'?D.n_s2:D.n_s1;
  let html=`<option value="__all__">All categories (${total})</option>`;
  html+=D.categories.map(c=>{
    const n=D.counts[c]; const k=view==='stage2'?n.s2:n.s1;
    return `<option value="${esc(c)}">${esc(c)} (${k})</option>`;
  }).join('');
  sel.innerHTML=html;
}
function setView(v){
  view=v; sortKey=(v==='avi')?'beat_3y':'composite_score'; sortDir=-1;
  document.getElementById('btnS2').classList.toggle('on',v==='stage2');
  document.getElementById('btnS1').classList.toggle('on',v==='stage1');
  document.getElementById('btnAVI').classList.toggle('on',v==='avi');
  if(v!=='avi'){
    const cur=document.getElementById('cat').value; initCats();
    if([...document.getElementById('cat').options].some(o=>o.value===cur)) document.getElementById('cat').value=cur;
  }
  render();
}
function sortBy(k){ if(sortKey===k){sortDir*=-1;} else {sortKey=k; sortDir= (k==='rank'||k==='stage2_rank')?1:-1;} render(); }

function render(){
  const isAvi = view==='avi';
  const catEl=document.getElementById('cat'), qEl=document.getElementById('q');
  catEl.style.display=isAvi?'none':''; qEl.style.display=isAvi?'none':'';
  document.getElementById('catwarn').style.display=isAvi?'none':'';
  document.getElementById('avinote').style.display=isAvi?'block':'none';
  let cols, rows;
  if(isAvi){
    cols=D.avi_cols.slice();
    rows=(D.avi||[]).slice();
    document.getElementById('avinote').innerHTML =
      `Per category: <b>% of funds that beat the index</b> (3y &amp; 5y) and the fund-return spread. High % + tight spread → active adds value; ~50% + wide spread → index is the safer bet. <span class="tech">Survivorship-biased; hybrids synthetic; returns-only.</span>`;
  } else {
    cols=colsFor(view).slice();
    const cat=catEl.value;
    const q=qEl.value.trim().toLowerCase();
    // Per-category honesty caveat from the retro-IC backtest (C1).
    const cw=document.getElementById('catwarn');
    const ic=(D.cat_ic||{})[cat];
    if(ic===undefined){cw.style.display='none';}
    else if(ic<=0){cw.className='catwarn neg';cw.style.display='block';
      cw.innerHTML=`⚠ <b>Be careful with the order in ${esc(cat)}.</b> In our back-test, the funds ranked near the top of this category tended to do <b>slightly worse</b> than a typical ${esc(cat)} fund afterwards — so this isn't a reliable best-to-worst list here. Use it as a rough shortlist only. <span class="tech">(rank-vs-future-return correlation ${ic.toFixed(2)})</span>`;}
    else if(ic<0.05){cw.className='catwarn wk';cw.style.display='block';
      cw.innerHTML=`• <b>${esc(cat)}:</b> the ranking only loosely matched what happened next — treat the order as approximate. <span class="tech">(correlation ${ic.toFixed(2)})</span>`;}
    else {cw.className='catwarn pos';cw.style.display='block';
      cw.innerHTML=`✓ <b>The order is more trustworthy in ${esc(cat)}.</b> Historically, funds ranked near the top here did tend to do better afterwards. <span class="tech">(correlation +${ic.toFixed(2)})</span>`;}
    const cdesc=(cat!=='__all__'&&D.cat_desc)?D.cat_desc[cat]:'';
    if(cdesc) catEl.setAttribute('data-tip',cdesc); else catEl.removeAttribute('data-tip');
    if(cat==='__all__'){
      rows=Object.values(dataFor(view)).flat();
      cols=[{k:'canonical_category',l:'Category',kind:'text',
             tip:'Which category this fund is ranked within. Scores are relative WITHIN a category, so a high score = stands out in its own category (not directly comparable across categories).'}, ...cols];
    } else {
      rows=(dataFor(view)[cat]||[]).slice();
    }
    if(q) rows=rows.filter(r=>String(r.scheme_name||'').toLowerCase().includes(q));
  }
  if(sortKey){
    const kind=(cols.find(c=>c.k===sortKey)||{}).kind;
    rows.sort((a,b)=>{
      let x=a[sortKey],y=b[sortKey];
      if(kind==='flags'){x=(x||[]).length;y=(y||[]).length;}
      const xn=(x===null||x===undefined||x===''), yn=(y===null||y===undefined||y==='');
      if(xn&&yn)return 0; if(xn)return 1; if(yn)return -1;   // nulls always last
      if(typeof x==='number'&&typeof y==='number')return (x-y)*sortDir;
      return String(x).localeCompare(String(y))*sortDir;
    });
  }
  // header
  document.getElementById('head').innerHTML = cols.map(c=>{
    const lft=(c.kind==='text'||c.kind==='flags'||c.kind==='verdict')?'lft':'';
    const tc=c.tip?'tip':''; const tip=c.tip?` data-tip="${esc(c.tip)}"`:'';
    const arr=sortKey===c.k?`<span class="arr">${sortDir<0?'▼':'▲'}</span>`:'';
    const sub=c.sub?`<div class="wt">${esc(c.sub)}</div>`:'';
    return `<th class="${lft} ${tc}" onclick="sortBy('${c.k}')"${tip}>${esc(c.l)} ${arr}${sub}</th>`;
  }).join('');
  // body
  document.getElementById('body').innerHTML = rows.map(r=>'<tr>'+cols.map(c=>{
    if(c.kind==='flags'){
      const a=r.flags;
      return a&&a.length ? `<td class="lft">${flagHtml(a)}</td>`
        : `<td class="lft"><span class="mut" data-tip="No data-quality caveats flagged for this fund — a good thing.">—</span></td>`;
    }
    if(c.k==='canonical_category'){            // hover shows what the category is
      const cc=r.canonical_category||'', dsc=(D.cat_desc&&D.cat_desc[cc])||'';
      return `<td class="lft"${dsc?` data-tip="${esc(dsc)}"`:''}>${esc(cc)}</td>`;
    }
    if(c.k==='verdict'){                        // Active-vs-Index call, colour-coded
      return `<td class="lft"><span class="vb ${r.verdict_c||'mut'}">${esc(r.verdict||'—')}</span></td>`;
    }
    if(c.k==='spread_3y'){                      // p25→p75 range; sort key is the IQR width
      const a=r.p25_3y, b=r.p75_3y;
      if(a===null||a===undefined) return `<td>${miss('spread_3y')}</td>`;
      return `<td>${(a*100).toFixed(1)}%<span class="mut"> → </span>${(b*100).toFixed(1)}%</td>`;
    }
    const v=r[c.k], isnull=(v===null||v===undefined||v==='');
    if(c.k==='top5_hits'||c.k==='top10_hits'){   // "hits / eligible-quarters"; sort by hits
      if(isnull) return `<td>${miss(c.k)}</td>`;
      const q=(c.k==='top5_hits')?r.top5_q:r.top10_q;
      return `<td>${v} <span class="mut">/ ${q}</span></td>`;
    }
    if(isnull) return `<td class="${(c.kind==='text')?'lft':''}">${miss(c.k)}</td>`;
    const [txt,cls,_n]=fmt(v,c.kind);
    let extra=cls; const lft=(c.kind==='text')?'lft':'';
    if((c.k==='alpha_3y_annualized'||c.k==='excess_3y')&&typeof v==='number') extra=v>=0?'pos':'neg';
    return `<td class="${lft} ${extra}">${txt}</td>`;
  }).join('')+'</tr>').join('');
  document.getElementById('empty').style.display = rows.length?'none':'block';
  const vlabel = isAvi?'Active vs Index (per category)':(view==='stage2'?'Top picks (Stage 2, enriched)':'Full ranking (Stage 1)');
  const scope = isAvi?`${rows.length} categories`:`${rows.length} funds · ${catEl.value==='__all__'?'All categories':catEl.value}`;
  document.getElementById('meta').textContent = `${scope} · ${vlabel}`;
  fit();
}

// Size the table's scroll box to the space below the header, so ONLY the table
// scrolls (both axes) — the page itself never scrolls sideways.
function fit(){
  const s=document.getElementById('scroll'); if(!s) return;
  const top=s.getBoundingClientRect().top;
  s.style.maxHeight=Math.max(220,Math.floor(window.innerHeight-top-8))+'px';
}
window.addEventListener('resize',fit);

// Instant styled tooltip for anything with data-tip (column headers, flag badges).
(function(){
  const tip=document.getElementById('tip');
  document.addEventListener('mouseover',e=>{
    const t=e.target.closest&&e.target.closest('[data-tip]'); if(!t) return;
    tip.textContent=t.getAttribute('data-tip'); tip.style.display='block';
    const r=t.getBoundingClientRect();
    tip.style.left=Math.max(6,Math.min(r.left,window.innerWidth-tip.offsetWidth-8))+'px';
    tip.style.top=(r.bottom+6)+'px';
  });
  document.addEventListener('mouseout',e=>{
    if(e.target.closest&&e.target.closest('[data-tip]')) tip.style.display='none';
  });
})();

document.getElementById('asof').textContent = '· run '+D.as_of;
document.getElementById('foot').innerHTML =
  `Run <code>${D.as_of}</code> · ${D.categories.length} categories · ${D.n_s2} picks · ${D.n_s1} ranked funds · `+
  `hover a heading for its meaning &amp; weight · built by <code>tools/build_dashboard.py</code>.`;
initCats(); setView('stage1');
</script>
</body>
</html>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--as-of", default=None, help="run date (default: latest under data/output/shortlist)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser after building")
    args = ap.parse_args(argv)

    as_of = args.as_of
    if as_of is None:
        runs = sorted(p.name for p in SHORTLIST_ROOT.iterdir() if p.is_dir()) if SHORTLIST_ROOT.exists() else []
        if not runs:
            raise SystemExit(f"No runs under {SHORTLIST_ROOT}")
        as_of = runs[-1]

    payload = collect(as_of)
    html = render(payload).replace("__TITLE__", as_of)
    out = SHORTLIST_ROOT / as_of / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    print(f"dashboard: {out}  ({payload['n_s2']} top picks, {payload['n_s1']} ranked funds, "
          f"{len(payload['categories'])} categories, {out.stat().st_size//1024} KB)")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
