"""Derive benchmark constituent weight CSVs from ingested index-tracker holdings.

NSE/niftyindices.com publishes index *membership* for free but NOT
machine-readable constituent *weights*. A passive index tracker's holdings
≈ the index weights (tracking drift typically <1% per stock), so we derive
constituent weights from the monthly portfolios we already ingest into
``holdings_monthly`` (Phase 3.C).

This rewrite (Phase 5) generalizes the original five-index hardcode to the
full benchmark set. For each target benchmark ticker it AUTO-SELECTS the
best-ingested tracker fund by name pattern + an index-size sanity check
(rejecting broad-index or factor-variant funds that happen to match the
name), so the derivation is robust to which AMC adapters have run. Indices
whose tracker isn't ingested yet are reported SKIPPED rather than guessed.

Three hybrid benchmarks (NIFTY 50 Hybrid 50:50 / 65:35, NIFTY Equity Savings)
are modeled by their NIFTY 50 EQUITY SLEEVE only — a deliberate methodology
choice, not a coverage gap. Active Share is an equity-portfolio measure
(Cremers–Petajisto): it compares the fund's equity book against the equity
benchmark, so the equity sleeve (NIFTY 50) is the correct constituent set for a
hybrid. The benchmark's debt sleeve is excluded because it has no decomposable
per-security weights (NIFTY Composite Debt Index publishes none and no tracker
fund exists), and adding an aggregate debt row would distort Active Share
(the fund's own debt holdings are dropped by the equity-only filter, so a
benchmark debt row would not cancel). The fixed equity/debt split is recorded
in the `composite_recipe` column of `configs/benchmarks.csv`. These rows are
flagged `OK*` (equity sleeve) in the run summary.

Output: one CSV per benchmark at
``data/raw/index_constituents/manual/<slug>/<ym>.csv`` with columns
``isin,weight_pct,security_name``. Equity-only, renormalized to 100%.

Usage:
    uv run python tools/derive_constituents.py [--ym 2026-04] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from mfs import paths
from mfs.db.connection import connect
from mfs.utils.logging import configure_logging, get_logger

log = get_logger(__name__)

# Indices with NO Direct+Growth index-fund tracker (ETF-only / niche sector).
# We derive their weights from an exact-index ETF's monthly portfolio (the
# ETF's holdings ARE the index). The ETF is AMC-direct but isn't plan/option-
# typed, so it never enters the holdings_monthly universe matcher — we go
# through the AMC's own holdings adapter (discover → fetch → parse_excel) so
# both per-scheme and consolidated-workbook AMCs work and the source stays
# re-runnable. Map: index_slug → (holdings_adapter_slug, [name keywords]).
_ETF_TRACKERS: dict[str, tuple[str, list[str]]] = {
    "nifty_energy_tri": ("mirae", ["nifty", "energy", "etf"]),
    "nifty_fmcg_tri": ("icici_pru", ["nifty", "fmcg", "etf"]),
    "nifty_mnc_tri": ("motilal_oswal", ["nifty", "mnc", "etf"]),
    "nifty_pse_tri": ("motilal_oswal", ["nifty", "pse", "etf"]),
}


def _derive_from_etf(adapter_slug: str, keywords: list[str], ym: str) -> list[dict]:
    """Discover + fetch + parse an exact-index ETF via its AMC holdings adapter."""
    import importlib

    from mfs.ingest.holdings._registry import get_adapter
    importlib.import_module(f"mfs.ingest.holdings.{adapter_slug}")
    a = get_adapter(adapter_slug)
    try:
        urls = a.discover_scheme_urls(ym)
    except Exception as e:  # noqa: BLE001
        log.warning("constituents.etf_discover_failed", adapter=adapter_slug, err=str(e))
        return []
    cand = [(n, u) for n, u in urls.items() if all(k in n.lower() for k in keywords)]
    if not cand:
        return []
    name, url = cand[0]
    try:
        p = a.fetch_excel(url, name + ".xlsx", ym)
        recs = list(a.parse_excel(p, name, ym))
    except Exception as e:  # noqa: BLE001
        log.warning("constituents.etf_parse_failed", adapter=adapter_slug, scheme=name, err=str(e))
        return []
    return [
        {"isin": r.isin, "security_name": r.security_name, "weight_pct": r.weight_pct}
        for r in recs
        if r.instrument_type == "Equity" and r.isin
    ]


# Per-benchmark derivation spec.
#   primary  : ILIKE phrase identifying the tracker family (besides "index")
#   excludes : ILIKE phrases that disqualify factor/leveraged/debt variants
#   expected : approximate constituent count, used for the size sanity check
#   derive_from / top_n : derive a subset of another resolved index instead
#   sleeve_of: this hybrid ticker's equity sleeve == the named index
# A tracker is accepted only if its latest-month equity count lands within
# [0.5x, 1.8x] of `expected`; among those the count closest to `expected`
# wins. This rejects e.g. a 264-stock "Nifty Index Fund" mis-tagged file
# when we want the 50-stock Nifty 50 tracker.
_COMMON_EXCLUDES = [
    "%g-sec%", "%gsec%", "% sdl%", "%ibx%", "%bond%", "% debt%", "%months%",
    "%maturity%", "%gilt%", "%liquid%", "%overnight%", "%money market%",
    "%target maturity%", "%fund of fund%", "% fof%", "%arbitrage%",
]
_FACTOR_EXCLUDES = [
    "%equal weight%", "%equal-weight%", "%momentum%", "%quality%",
    "%low volatility%", "%low vol%", "%alpha%", "%value 20%", "%value 50%",
    "%enhanced%", "%top 10%", "%top 15%", "%growth sector%",
]

_SPECS: dict[str, dict] = {
    # --- broad market (already had hardcoded trackers; now auto-selected) ---
    "nifty_500_tri": {"primary": "%nifty 500%", "excludes": _FACTOR_EXCLUDES + ["%multicap%", "%esg%", "%next%"], "expected": 500},
    "nifty_100_tri": {"derive_from": "nifty_500_tri", "top_n": 100, "expected": 100},
    "nifty_midcap_150_tri": {"primary": "%midcap 150%", "excludes": _FACTOR_EXCLUDES, "expected": 150},
    "nifty_smallcap_250_tri": {"primary": "%smallcap 250%", "excludes": _FACTOR_EXCLUDES, "expected": 250},
    "nifty_largemidcap_250_tri": {"primary": "%largemidcap 250%", "excludes": _FACTOR_EXCLUDES, "expected": 250},
    # No plain "Nifty 500 Multicap 50:25:25" index fund exists (only factor
    # variants). The index reweights the SAME Nifty 500 securities to a fixed
    # 50:25:25 large:mid:small split, so for security-overlap (Active Share)
    # the Nifty 500 constituent SET is the right proxy. Flagged as approximation.
    "nifty_500_multicap_50_25_25_tri": {"sleeve_of": "nifty_500_tri", "expected": 500},
    "nifty_500_value_50_tri": {"primary": "%500 value 50%", "excludes": [], "expected": 50},
    # --- sectoral / thematic ---
    "nifty_financial_services_tri": {"primary": "%financial services%", "excludes": _COMMON_EXCLUDES + ["%ex-bank%", "%ex bank%", "%25 50%"], "expected": 20},
    "nifty_it_tri": {"primary": "%nifty it%", "excludes": _FACTOR_EXCLUDES, "expected": 10},
    "nifty_healthcare_tri": {"primary": "%healthcare%", "excludes": _FACTOR_EXCLUDES, "expected": 20},
    "nifty_auto_tri": {"primary": "%nifty auto%", "excludes": _FACTOR_EXCLUDES, "expected": 15},
    "nifty_fmcg_tri": {"primary": "%fmcg%", "excludes": _FACTOR_EXCLUDES, "expected": 15},
    "nifty_energy_tri": {"primary": "%nifty energy%", "excludes": _FACTOR_EXCLUDES, "expected": 10},
    "nifty_infrastructure_tri": {"primary": "%infrastructure%", "excludes": _FACTOR_EXCLUDES, "expected": 30},
    "nifty_pse_tri": {"primary": "%pse%", "excludes": _FACTOR_EXCLUDES + ["%psu bank%", "%psu bond%"], "expected": 20},
    "nifty_india_consumption_tri": {"primary": "%india consumption%", "excludes": _FACTOR_EXCLUDES, "expected": 30},
    "nifty_mnc_tri": {"primary": "%mnc%", "excludes": _FACTOR_EXCLUDES, "expected": 30},
    "nifty100_esg_tri": {"primary": "%esg%", "excludes": _FACTOR_EXCLUDES + ["%enhanced%", "%sector leaders%"], "expected": 90},
    "nifty_india_manufacturing_tri": {"primary": "%nifty india manufacturing%", "excludes": _FACTOR_EXCLUDES + ["%multicap%", "%50 30 20%", "%50:30:20%"], "expected": 80},
    # --- helper: NIFTY 50 (not a ranked ticker on its own here, but the
    #     equity sleeve for the three synthetic hybrids) ---
    "_nifty_50": {"primary": "%nifty 50%", "excludes": _FACTOR_EXCLUDES + ["%next 50%", "%500%", "%100%", "%200%", "%midcap%", "%smallcap%", "%bank%"], "expected": 50,
                  "alt_primary": "%nifty index%"},
    # --- synthetic hybrids: equity sleeve == NIFTY 50 ---
    "nifty_50_hybrid_50_50_tri": {"sleeve_of": "_nifty_50", "expected": 50},
    "nifty_50_hybrid_65_35_tri": {"sleeve_of": "_nifty_50", "expected": 50},
    "nifty_equity_savings_tri": {"sleeve_of": "_nifty_50", "expected": 50},
}


def _candidate_trackers(primary: str, excludes: list[str], ym_first: str) -> list[tuple[str, str, int]]:
    """Return [(scheme_code, scheme_name, equity_count_latest_month)] for
    Direct+Growth index funds matching `primary`, minus `excludes`, that have
    equity holdings ingested for the given data month."""
    exclude_sql = ""
    excl = excludes or []
    if excl:
        exclude_sql = " AND " + " AND ".join(["s.scheme_name NOT ILIKE %s"] * len(excl))
    # '%index%' is bound as a parameter (not inlined) so psycopg doesn't read
    # the '%i' as a placeholder.
    params: list = ["%index%", primary] + excl + [ym_first]
    sql = f"""
        SELECT s.scheme_code, s.scheme_name, count(*) AS eqn
        FROM scheme_master s
        JOIN holdings_monthly h ON h.scheme_code = s.scheme_code
        WHERE s.is_active AND s.plan_type='DIRECT' AND s.option_type='GROWTH'
          AND s.scheme_name ILIKE %s
          AND s.scheme_name ILIKE %s
          {exclude_sql}
          AND h.instrument_type = 'Equity'
          AND h.as_of_month = %s
          AND h.isin IS NOT NULL
        GROUP BY 1, 2
        ORDER BY eqn DESC
    """
    with connect() as c:
        rows = c.execute(sql, params).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _select_tracker(spec: dict, ym_first: str) -> tuple[str, str] | None:
    """Pick the best tracker scheme_code for a spec via size-sanity. Returns
    (scheme_code, scheme_name) or None if no acceptable tracker is ingested."""
    expected = spec["expected"]
    lo, hi = expected * 0.5, expected * 1.8
    cands = _candidate_trackers(spec["primary"], spec.get("excludes", []), ym_first)
    if not cands and spec.get("alt_primary"):
        cands = _candidate_trackers(spec["alt_primary"], spec.get("excludes", []), ym_first)
    # Size sanity is authoritative: a tracker whose equity count is wildly off
    # `expected` is a broad-index or factor mis-match, not the index we want.
    in_band = [c for c in cands if lo <= c[2] <= hi]
    if not in_band:
        return None
    best = min(in_band, key=lambda c: abs(c[2] - expected))
    return best[0], best[1]


def _equity_holdings(scheme_code: str, ym_first: str) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT isin, security_name, weight_pct FROM holdings_monthly "
            "WHERE scheme_code=%s AND as_of_month=%s AND instrument_type='Equity' "
            "AND isin IS NOT NULL AND security_name IS NOT NULL",
            (scheme_code, ym_first),
        ).fetchall()
    return [{"isin": r[0], "security_name": r[1], "weight_pct": float(r[2])} for r in rows]


def _dedupe_renormalize(rows: list[dict]) -> list[dict]:
    bucket: dict[str, dict] = {}
    for r in rows:
        isin = r["isin"]
        b = bucket.setdefault(isin, {"isin": isin, "security_name": r["security_name"], "weight_pct": 0.0})
        b["weight_pct"] += float(r["weight_pct"])
    total = sum(b["weight_pct"] for b in bucket.values())
    if total <= 0:
        return []
    for b in bucket.values():
        b["weight_pct"] = round(b["weight_pct"] / total * 100.0, 4)
    out = sorted(bucket.values(), key=lambda b: -b["weight_pct"])
    return out


def _top_n(rows: list[dict], n: int) -> list[dict]:
    top = rows[:n]
    total = sum(r["weight_pct"] for r in top)
    if total <= 0:
        return []
    for r in top:
        r["weight_pct"] = round(r["weight_pct"] / total * 100.0, 4)
    return top


def write_csv(slug: str, ym: str, rows: list[dict]) -> Path:
    out = paths.index_constituents_manual_dir(slug) / f"{ym}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=["isin", "weight_pct", "security_name"])
        wr.writeheader()
        for r in rows:
            wr.writerow({"isin": r["isin"], "weight_pct": r["weight_pct"], "security_name": r["security_name"]})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ym", default="2026-04", help="Data month YYYY-MM")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    configure_logging()
    ym = args.ym
    ym_first = f"{ym}-01"

    # Resolve in dependency order: plain indices first, then derive_from/sleeve.
    plain = [s for s, sp in _SPECS.items() if not sp.get("derive_from") and not sp.get("sleeve_of")]
    derived = [s for s, sp in _SPECS.items() if sp.get("derive_from") or sp.get("sleeve_of")]
    resolved: dict[str, list[dict]] = {}
    summary: list[tuple[str, str, int, str]] = []  # slug, status, n, note

    # ETF-Excel-sourced indices (no Direct+Growth index fund exists).
    for slug, (adapter_slug, keywords) in _ETF_TRACKERS.items():
        rows = _dedupe_renormalize(_derive_from_etf(adapter_slug, keywords, ym))
        if rows:
            resolved[slug] = rows
            summary.append((slug, "OK^", len(rows), f"from {adapter_slug} ETF ({' '.join(keywords)})"))
        else:
            summary.append((slug, "SKIPPED", 0, f"ETF via {adapter_slug} not found/empty"))

    for slug in plain:
        if slug in _ETF_TRACKERS:
            continue
        spec = _SPECS[slug]
        sel = _select_tracker(spec, ym_first)
        if sel is None:
            summary.append((slug, "SKIPPED", 0, "no ingested tracker"))
            continue
        code, name = sel
        rows = _dedupe_renormalize(_equity_holdings(code, ym_first))
        if not rows:
            summary.append((slug, "SKIPPED", 0, f"tracker {code} has no equity rows"))
            continue
        resolved[slug] = rows
        summary.append((slug, "OK", len(rows), f"from {name[:48]}"))

    for slug in derived:
        spec = _SPECS[slug]
        if spec.get("sleeve_of"):
            parent = resolved.get(spec["sleeve_of"])
            if not parent:
                summary.append((slug, "SKIPPED", 0, f"sleeve {spec['sleeve_of']} unresolved"))
                continue
            rows = [dict(r) for r in parent]
            resolved[slug] = rows
            note = ("NIFTY 50 equity sleeve (debt sleeve excluded by design)"
                    if spec["sleeve_of"] == "_nifty_50"
                    else f"approximation: same securities as {spec['sleeve_of']}")
            summary.append((slug, "OK*", len(rows), note))
        else:  # derive_from top_n
            parent = resolved.get(spec["derive_from"])
            if not parent:
                summary.append((slug, "SKIPPED", 0, f"parent {spec['derive_from']} unresolved"))
                continue
            rows = _top_n([dict(r) for r in parent], spec["top_n"])
            resolved[slug] = rows
            summary.append((slug, "OK", len(rows), f"top-{spec['top_n']} of {spec['derive_from']}"))

    # Skip the helper from output (it's only a sleeve source).
    print(f"\n{'benchmark slug':36s} {'status':7s} {'n':>4s}  note")
    print("-" * 100)
    n_ok = 0
    for slug, status, n, note in sorted(summary):
        if slug == "_nifty_50":
            continue
        print(f"{slug:36s} {status:7s} {n:>4d}  {note}")
        if status.startswith("OK"):
            n_ok += 1
            if not args.dry_run:
                p = write_csv(slug, ym, resolved[slug])
                log.info("constituents.derived", slug=slug, ym=ym, n_rows=n, path=str(p))
    ranked = [s for s in _SPECS if not s.startswith("_")]
    print(f"\nResolved {n_ok}/{len(ranked)} benchmark tickers"
          f"{' (dry-run, nothing written)' if args.dry_run else ''}.")
    print("OK* = hybrid benchmark modeled by its NIFTY 50 equity sleeve "
          "(by design; debt sleeve excluded — see configs/benchmarks.csv).")


if __name__ == "__main__":
    main()
