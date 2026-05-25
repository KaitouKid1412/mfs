"""Derive benchmark constituent CSVs from ingested index-fund holdings.

NSE/niftyindices.com publishes index *membership* (no weights) for free.
But Phase 3.C ingested HDFC + SBI + Nippon monthly portfolio Excels, several
of which are passive index funds that track the benchmarks we care about.
A passive tracker's holdings ≈ the index weights (tracking drift typically
<1% per stock), so we can derive constituent weights from those holdings.

This script writes one CSV per benchmark at
``data/raw/index_constituents/manual/<slug>/<ym>.csv`` with columns
``isin,weight_pct,security_name``. Equity-only, weights renormalized to
100% on the equity sub-portfolio.

For NIFTY 100 (which we don't have a clean tracker for), we derive the top
100 stocks by weight from the NIFTY 500 tracker and renormalize. NIFTY 100
is by construction the top-100 cap-weighted subset of NIFTY 500, so this
gives a ~1% drift approximation.

Usage:
    uv run python tools/derive_constituents.py [--ym 2026-04] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import polars as pl

from mfs import paths
from mfs.db import queries as q
from mfs.utils.logging import configure_logging, get_logger

log = get_logger(__name__)

# Benchmark slug → (source scheme_code, derivation_note). NIFTY 100 has no
# direct tracker in our ingest; we derive from NIFTY 500's top-100 by weight.
_SOURCE_TRACKERS: dict[str, dict] = {
    "nifty_500_tri": {
        "scheme_code": "152908",  # SBI Nifty 500 Index Fund
        "tracker_name": "SBI Nifty 500 Index Fund",
        "derive_from": None,
    },
    "nifty_largemidcap_250_tri": {
        "scheme_code": "152889",  # HDFC NIFTY LargeMidcap 250 Index Fund
        "tracker_name": "HDFC NIFTY LargeMidcap 250 Index Fund",
        "derive_from": None,
    },
    "nifty_smallcap_250_tri": {
        "scheme_code": "151727",  # HDFC NIFTY Smallcap 250 Index Fund
        "tracker_name": "HDFC NIFTY Smallcap 250 Index Fund",
        "derive_from": None,
    },
    "nifty_midcap_150_tri": {
        "scheme_code": "151724",  # HDFC NIFTY Midcap 150 Index Fund
        "tracker_name": "HDFC NIFTY Midcap 150 Index Fund",
        "derive_from": None,
    },
    "nifty_100_tri": {
        # Derived from NIFTY 500's top 100 by weight, renormalized.
        "scheme_code": None,
        "tracker_name": "derived from NIFTY 500 top-100 by weight",
        "derive_from": "nifty_500_tri",
    },
}


def _fetch_equity_holdings(scheme_code: str) -> pl.DataFrame:
    """Return equity holdings for the most recent month available."""
    df = q.holdings_for_scheme(scheme_code)
    if df.is_empty():
        return df
    last_month = df["as_of_month"].max()
    eq = df.filter(
        (pl.col("as_of_month") == last_month)
        & (pl.col("instrument_type") == "Equity")
        & pl.col("security_name").is_not_null()
        & (pl.col("security_name") != "")
        & pl.col("isin").is_not_null()
        & (pl.col("isin") != "")
    )
    return eq.select(["isin", "security_name", "weight_pct"])


def _dedupe_and_renormalize(rows: list[dict]) -> list[dict]:
    """Collapse rows with the same ISIN (sum weights), then renormalize the
    full set so weights sum to 100%."""
    bucket: dict[str, dict] = {}
    for r in rows:
        isin = r["isin"]
        if isin not in bucket:
            bucket[isin] = {
                "isin": isin,
                "security_name": r["security_name"],
                "weight_pct": 0.0,
            }
        bucket[isin]["weight_pct"] += float(r["weight_pct"])
    total = sum(b["weight_pct"] for b in bucket.values())
    if total <= 0:
        return []
    for b in bucket.values():
        b["weight_pct"] = round(b["weight_pct"] / total * 100.0, 4)
    out = list(bucket.values())
    out.sort(key=lambda b: -b["weight_pct"])
    return out


def _derive_top_n_from(
    source_rows: list[dict], n: int,
) -> list[dict]:
    """Take top-n by weight and renormalize to 100%."""
    top = source_rows[:n]
    total = sum(r["weight_pct"] for r in top)
    if total <= 0:
        return []
    for r in top:
        r["weight_pct"] = round(r["weight_pct"] / total * 100.0, 4)
    return top


def derive_one(slug: str, ym: str, source_cache: dict[str, list[dict]]) -> list[dict]:
    """Derive constituent rows for one benchmark slug. Returns the list of
    {isin, security_name, weight_pct} dicts ready to write."""
    spec = _SOURCE_TRACKERS[slug]
    if spec["derive_from"]:
        parent_rows = source_cache.get(spec["derive_from"])
        if not parent_rows:
            raise RuntimeError(
                f"{slug}: can't derive because parent {spec['derive_from']}"
                " was not generated"
            )
        # NIFTY 100 = top 100 of NIFTY 500.
        n = int(re.search(r"(\d+)", slug).group(1))
        return _derive_top_n_from(list(parent_rows), n)

    scheme_code = spec["scheme_code"]
    eq = _fetch_equity_holdings(scheme_code)
    if eq.is_empty():
        raise RuntimeError(
            f"{slug}: no equity holdings on disk for scheme_code={scheme_code} "
            f"({spec['tracker_name']}). Run `mfs ingest holdings` first."
        )
    rows = eq.to_dicts()
    return _dedupe_and_renormalize(rows)


def write_csv(slug: str, ym: str, rows: list[dict]) -> Path:
    out = paths.index_constituents_manual_dir(slug) / f"{ym}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["isin", "weight_pct", "security_name"])
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "isin": r["isin"],
                "weight_pct": r["weight_pct"],
                "security_name": r["security_name"],
            })
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ym", default="2026-04", help="Data month YYYY-MM")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print summary but don't write CSV files",
    )
    args = parser.parse_args()
    configure_logging()

    cache: dict[str, list[dict]] = {}
    # Process in dependency order: parents (NIFTY 500) first, then derivatives.
    order = sorted(
        _SOURCE_TRACKERS.keys(),
        key=lambda s: 0 if _SOURCE_TRACKERS[s]["derive_from"] is None else 1,
    )

    print(f"{'benchmark':35s} {'n_const':>8s}  {'tot_wt':>7s}  {'top1_wt':>8s}  {'source'}")
    print("-" * 100)
    for slug in order:
        try:
            rows = derive_one(slug, args.ym, cache)
        except RuntimeError as e:
            print(f"{slug:35s} SKIPPED: {e}")
            continue
        cache[slug] = rows
        total_wt = sum(r["weight_pct"] for r in rows)
        top1 = rows[0]["weight_pct"] if rows else 0.0
        spec = _SOURCE_TRACKERS[slug]
        src = spec["tracker_name"]
        print(
            f"{slug:35s} {len(rows):>8d}  {total_wt:>6.2f}%  {top1:>7.2f}%  {src}"
        )
        if not args.dry_run:
            path = write_csv(slug, args.ym, rows)
            log.info("constituents.derived", slug=slug, ym=args.ym,
                     n_rows=len(rows), path=str(path))


if __name__ == "__main__":
    main()
