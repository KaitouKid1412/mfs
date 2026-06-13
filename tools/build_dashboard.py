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


def _rows(df: pl.DataFrame, cols: list[tuple], with_flags: bool) -> list[dict]:
    keys = [k for k, _, kind in cols if kind != "flags"]
    present = [k for k in keys if k in df.columns]
    out = []
    for r in df.iter_rows(named=True):
        rec = {k: _clean(r.get(k)) for k in present}
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

    stage1: dict[str, list] = {}
    stage2: dict[str, list] = {}
    cats: set[str] = set()
    if not s1.is_empty():
        for cat, g in s1.group_by("canonical_category"):
            c = cat[0]
            cats.add(c)
            stage1[c] = _rows(g.sort("rank"), STAGE1_COLS, with_flags=False)
    if not s2.is_empty():
        for cat, g in s2.group_by("canonical_category"):
            c = cat[0]
            cats.add(c)
            stage2[c] = _rows(g.sort("stage2_rank"), STAGE2_COLS, with_flags=True)

    cat_list = sorted(cats)
    counts = {c: {"s1": len(stage1.get(c, [])), "s2": len(stage2.get(c, []))}
              for c in cat_list}
    return {
        "as_of": as_of,
        "categories": cat_list,
        "counts": counts,
        "cat_ic": _load_cat_ic(),
        "n_s1": sum(len(v) for v in stage1.values()),
        "n_s2": sum(len(v) for v in stage2.values()),
        "stage1_cols": [{"k": k, "l": l, "kind": kind} for k, l, kind in STAGE1_COLS],
        "stage2_cols": [{"k": k, "l": l, "kind": kind} for k, l, kind in STAGE2_COLS],
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
  .banner{margin:10px 0 0;padding:8px 12px;border-radius:6px;font-size:12px;
          background:#2a210f;border:1px solid #5a4412;color:#f0d68a}
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
  <div class="banner">⚠ <b>Descriptive quality screen, not a validated forecast.</b>
    The composite weights were <b>not validated as forward-predictive</b> (2026-06 retro-IC backtest: pooled 1-year IC −0.05, verdict REFUTED — survivorship-biased upper bound). Treat ranks as a low-cost / consistent / risk-adjusted screen, not a performance prediction. Active-share is dormant (activates ~2026-07-10).</div>
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
    cw.innerHTML=`⚠ Backtest: <b>${esc(cat)}</b> composite had a <b>negative 1-year IC (${ic.toFixed(3)})</b> on the survivor backtest — its top-ranked funds <b>historically underperformed the category median</b>. Treat this ranking as anti-predictive.`;}
  else if(ic<0.05){cw.className='catwarn wk';cw.style.display='block';
    cw.innerHTML=`• Backtest: <b>${esc(cat)}</b> composite 1-year IC ${ic.toFixed(3)} — weak / inconclusive predictive value.`;}
  else {cw.className='catwarn pos';cw.style.display='block';
    cw.innerHTML=`✓ Backtest: <b>${esc(cat)}</b> composite 1-year IC <b>${ic.toFixed(3)}</b> — ranking was forward-predictive on the survivor backtest.`;}
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
    const arr=sortKey===c.k?`<span class="arr">${sortDir<0?'▼':'▲'}</span>`:'';
    return `<th class="${lft}" onclick="sortBy('${c.k}')">${c.l} ${arr}</th>`;
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

document.getElementById('asof').textContent = '· run '+D.as_of;
document.getElementById('foot').innerHTML =
  `Run <code>${D.as_of}</code> · ${D.categories.length} categories · `+
  `${D.n_s2} top picks (Stage 2) · ${D.n_s1} ranked funds (Stage 1). `+
  `Generated by <code>tools/build_dashboard.py</code> from <code>data/output/shortlist/${D.as_of}/</code>. `+
  `Returns/alpha/PTR/active-share are annualised fractions shown as %; Max-DD &amp; TER are already %; `+
  `Sortino/Info-Ratio/Capture/R²/Beta/Score are ratios. `+
  `<b>α conf</b> (0–1) = share of rolling-3y windows where alpha was statistically significant (|t|≥1) — display-only, not a filter. `+
  `Per-category backtest notes (when shown) come from the latest retro-IC run. `+
  `“—” = not available (e.g. active-share while dormant, or a fund outside the Stage-2 enrichment pool).`;
initCats(); setView('stage2');
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
