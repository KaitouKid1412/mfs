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

import polars as pl

SHORTLIST_ROOT = Path("data") / "output" / "shortlist"

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
    ("ter_pct", "TER", "pctraw"),
    ("aum_crore", "AUM (Cr)", "cr"),
    ("ret_3y_median", "Ret 3y", "pct"),
    ("ret_5y_median", "Ret 5y", "pct"),
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


# Plain-English, one-line explanation per column (shown on hover + in the
# on-screen "what the columns mean" panel).
TIPS = {
    "rank": "Rank within the category (1 = best on our score).",
    "stage2_rank": "Rank within the category (1 = best on our score).",
    "scheme_name": "Fund name — Direct plan, Growth option.",
    "composite_score": "Overall ranking score — combines every metric here. Higher = ranked higher.",
    "top5_hits": "How many past quarters (since 2016) this fund ranked in its category's top-5 on the core composite — a consistency tally. Higher = more consistently strong. Older funds can rack up more, so check Top-5 % too. Survivorship-biased; blank for always-small categories where 'top-5' isn't selective.",
    "top5_pct": "Share of its back-tested quarters the fund was top-5 (a hit-rate — fairer to younger funds than the raw count).",
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
        # top-5 persistence is sourced from the backtest, not the run parquet.
        p = persist.get(r.get("scheme_code"))
        rec["top5_hits"] = p["hits"] if p else None
        rec["top5_pct"] = p["pct"] if p else None
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
                        "hits": int(row["n_top5"]),
                        "pct": float(row["top5_pct"]),
                        "q": int(row["n_quarters_eligible"]),
                    }
                except (ValueError, TypeError, KeyError):
                    pass
    except OSError:
        return {}
    return out


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
    return {
        "as_of": as_of,
        "categories": cat_list,
        "counts": counts,
        "cat_ic": _load_cat_ic(),
        "flag_glossary": FLAG_GLOSSARY,
        "n_s1": sum(len(v) for v in stage1.values()),
        "n_s2": sum(len(v) for v in stage2.values()),
        "stage1_cols": [{"k": k, "l": l, "kind": kind, "tip": TIPS.get(k, "")} for k, l, kind in STAGE1_COLS],
        "stage2_cols": [{"k": k, "l": l, "kind": kind, "tip": TIPS.get(k, "")} for k, l, kind in STAGE2_COLS],
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
  .gloss{margin-top:8px;max-width:1100px}
  .gloss .grid{display:grid;grid-template-columns:160px 1fr;gap:3px 14px;margin-top:6px;
               background:var(--head);padding:9px 12px;border-radius:6px}
  .gloss .k{color:var(--fg);font-weight:600;font-size:12px}
  .gloss .v{color:var(--mut);font-size:12px}
  .gloss h4{margin:10px 0 2px;color:var(--accent);font-size:11.5px;font-weight:600}
  .tech{color:var(--mut);font-size:11px}
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
  .wrap{padding:0 0 40px}
  table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
  thead th{position:sticky;top:0;background:var(--head);border-bottom:2px solid var(--line);
           padding:8px 10px;text-align:right;cursor:pointer;white-space:nowrap;user-select:none}
  thead th:first-child,thead th.lft{text-align:left}
  thead th:hover{color:var(--accent)}
  thead th.tip{text-decoration:underline dotted rgba(139,152,169,.6);text-underline-offset:4px}
  th .arr{color:var(--accent);font-size:10px}
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
  <div class="sub">Indian equity/hybrid funds (Direct + Growth), ranked per category. Click a column header to sort; type to filter.</div>
  <div class="banner">
    <b>How to read this:</b> funds are ranked within each category by their <b>past</b> numbers — low fees, steady performance, and good risk-adjusted returns. Think of it as a <b>quality shortlist, not a prediction</b> of next year's winner. When we tested it, ranking near the top did <b>not</b> reliably lead to better future returns — so use it to narrow the field to solid, low-cost, consistent funds, then dig deeper before deciding.
    <details><summary>Why we say it's "not a prediction" (the technical bit)</summary>
      <div class="body">We replayed this ranking back to 2016 and checked whether higher-ranked funds went on to beat lower-ranked ones. The match was about <b>zero / slightly negative</b> (a rank-vs-future-return correlation, or "IC", of −0.05 over 1 year) — i.e. no better than chance. The test can only include funds that still exist today; closed funds (usually the poor ones) have vanished from the data, so the real figure is, if anything, a bit worse. Bottom line: the scoring is a sound <i>quality screen</i> but is <b>not validated as a performance forecast.</b></div>
    </details>
    <span class="note">The “Active Share” column is blank until ~10 Jul 2026 — that data isn't ready yet, it's not a fund problem.</span>
  </div>
  <details class="gloss" id="gloss"><summary>ℹ︎ What the columns &amp; flags mean (plain English)</summary></details>
  <div class="controls">
    <div class="toggle">
      <button id="btnS2" class="on" onclick="setView('stage2')">Top picks</button>
      <button id="btnS1" onclick="setView('stage1')">Full ranking</button>
    </div>
    <select id="cat" onchange="render()"></select>
    <input id="q" type="search" placeholder="Filter funds… (name)" oninput="render()"/>
    <span class="meta" id="meta"></span>
  </div>
  <div class="catwarn" id="catwarn"></div>
</header>
<div class="wrap"><table id="tbl"><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table>
  <div class="empty" id="empty" style="display:none">No funds match.</div>
</div>
<footer id="foot"></footer>
<script>
const D = /*__DATA__*/;
let view='stage2', sortKey=null, sortDir=-1;

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
  return arr.map(f=>`<span class="badge ${f.c}" title="${esc(f.h||'')}">${esc(f.t)}</span>`).join('');}

function initCats(){
  const sel=document.getElementById('cat');
  sel.innerHTML = D.categories.map(c=>{
    const n=D.counts[c]; const k=view==='stage2'?n.s2:n.s1;
    return `<option value="${esc(c)}">${esc(c)} (${k})</option>`;
  }).join('');
}
function setView(v){
  view=v; sortKey=null; sortDir=-1;
  document.getElementById('btnS2').classList.toggle('on',v==='stage2');
  document.getElementById('btnS1').classList.toggle('on',v==='stage1');
  const cur=document.getElementById('cat').value; initCats();
  if([...document.getElementById('cat').options].some(o=>o.value===cur)) document.getElementById('cat').value=cur;
  render();
}
function sortBy(k){ if(sortKey===k){sortDir*=-1;} else {sortKey=k; sortDir= (k==='rank'||k==='stage2_rank')?1:-1;} render(); }

function render(){
  const cols=colsFor(view), cat=document.getElementById('cat').value;
  const q=document.getElementById('q').value.trim().toLowerCase();
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
  let rows=(dataFor(view)[cat]||[]).slice();
  if(q) rows=rows.filter(r=>String(r.scheme_name||'').toLowerCase().includes(q));
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
    const lft=(c.kind==='text'||c.kind==='flags')?'lft':'';
    const tc=c.tip?'tip':''; const tip=c.tip?` title="${esc(c.tip)}"`:'';
    const arr=sortKey===c.k?`<span class="arr">${sortDir<0?'▼':'▲'}</span>`:'';
    return `<th class="${lft} ${tc}" onclick="sortBy('${c.k}')"${tip}>${esc(c.l)} ${arr}</th>`;
  }).join('');
  // body
  document.getElementById('body').innerHTML = rows.map(r=>'<tr>'+cols.map(c=>{
    if(c.kind==='flags') return `<td class="lft">${flagHtml(r.flags)}</td>`;
    const [txt,cls,_n]=fmt(r[c.k],c.kind);
    let extra=cls; const lft=(c.kind==='text')?'lft':'';
    if(c.k==='alpha_3y_annualized'&&typeof r[c.k]==='number') extra=r[c.k]>=0?'pos':'neg';
    return `<td class="${lft} ${extra}">${txt}</td>`;
  }).join('')+'</tr>').join('');
  document.getElementById('empty').style.display = rows.length?'none':'block';
  document.getElementById('meta').textContent =
    `${rows.length} funds · ${view==='stage2'?'Top picks (Stage 2, enriched)':'Full ranking (Stage 1)'}`;
}

function initGloss(){
  const seen={}, cols=[...D.stage2_cols, ...D.stage1_cols];
  let rows='';
  cols.forEach(c=>{ if(c.tip && !seen[c.l]){seen[c.l]=1; rows+=`<div class="k">${esc(c.l)}</div><div class="v">${esc(c.tip)}</div>`; }});
  const flags=(D.flag_glossary||[]).map(f=>`<div class="k">${esc(f.t)}</div><div class="v">${esc(f.h)}</div>`).join('');
  document.getElementById('gloss').insertAdjacentHTML('beforeend',
    `<div class="grid">${rows}</div><h4>Flag badges (the coloured tags in the Flags column)</h4><div class="grid">${flags}</div>`);
}

document.getElementById('asof').textContent = '· run '+D.as_of;
document.getElementById('foot').innerHTML =
  `Run <code>${D.as_of}</code> · ${D.categories.length} categories · ${D.n_s2} shortlisted picks · ${D.n_s1} ranked funds. `+
  `<b>Hover any column heading</b> for what it means, or open “What the columns &amp; flags mean” near the top. `+
  `“—” means we don't have that number yet (e.g. Active Share until ~10 Jul 2026). `+
  `Built by <code>tools/build_dashboard.py</code>.`;
initGloss(); initCats(); setView('stage2');
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
