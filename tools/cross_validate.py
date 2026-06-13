#!/usr/bin/env python
"""External cross-validation spot-check (F-11). READ-ONLY — never writes to
Postgres or to data/; output is stdout only.

No computed number in this pipeline had ever been reconciled against an
external source. This tool prints, for N scheme codes, OUR side of that
reconciliation so an operator can compare it line-by-line against published
numbers (AMC factsheet trailing returns, ValueResearch, niftyindices.com):

  * point-to-point 3y and 5y CAGR straight from nav_daily (latest NAV vs the
    NAV nearest 3y/5y prior) — the closest analogue to the "trailing return"
    every factsheet publishes;
  * the stored rolling ret_3y_median / ret_5y_median for context (these are
    MEDIANS over rolling windows, deliberately NOT comparable 1:1 to a
    point-to-point trailing return — the point-to-point columns are the
    external-comparison numbers);
  * the stored alpha_3y_annualized + alpha_confidence (signed alpha, median
    over all rolling windows — see compute/alpha.py; external "alpha"
    figures use different conventions, so compare sign and rough magnitude
    only);
  * optionally, hand-entered external values from a CSV, with deltas and a
    PASS/FAIL verdict at the ±0.3pp tolerance (date-mismatch allowance).

Benchmark mode prints benchmark_daily TRI closes for a ticker on given dates
— compare against niftyindices.com historical data (exact match expected).

Checklist + recording procedure: docs/ops/spot_check.md; per-run results are
recorded from docs/audit/cross_validation_template.md.

Usage:
    uv run python tools/cross_validate.py --schemes 120465,120503,118825
    uv run python tools/cross_validate.py --schemes ... --external ext.csv
        # ext.csv columns: scheme_code,ext_3y_cagr_pct,ext_5y_cagr_pct,source
    uv run python tools/cross_validate.py --ticker "NIFTY 50 TRI" \
        --dates 2026-06-10,2025-06-10
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

#: Max calendar-day distance between the ideal lookback date and the nearest
#: available NAV before the point-to-point CAGR refuses to report.
NEAREST_NAV_TOLERANCE_DAYS = 10

#: External-comparison tolerance for trailing returns, in percentage points
#: (generous for publication-date mismatch vs our latest-NAV anchor).
EXTERNAL_TOLERANCE_PP = 0.30

DAYS_PER_YEAR = 365.25


class CagrPoint(NamedTuple):
    cagr: float          # fraction, e.g. 0.1842
    start_date: date
    start_nav: float
    end_date: date
    end_nav: float
    years_actual: float


def point_to_point_cagr(
    series: list[tuple[date, float]],
    years: float,
    *,
    tolerance_days: int = NEAREST_NAV_TOLERANCE_DAYS,
) -> CagrPoint | None:
    """Point-to-point CAGR from the LAST point of ``series`` back ``years``.

    ``series`` is (date, nav) pairs, assumed sorted ascending and positive.
    The start point is the NAV nearest ``end_date - years``; if no NAV falls
    within ``tolerance_days`` of that target (listing gap, young fund),
    returns None rather than reporting a mislabeled horizon. The exponent
    uses the ACTUAL day span, so small date slips don't bias the rate.
    """
    if len(series) < 2:
        return None
    end_date, end_nav = series[-1]
    if end_nav <= 0:
        return None
    target = end_date - timedelta(days=round(years * DAYS_PER_YEAR))
    start = min(series, key=lambda p: abs((p[0] - target).days))
    start_date, start_nav = start
    if abs((start_date - target).days) > tolerance_days:
        return None
    if start_nav <= 0 or start_date >= end_date:
        return None
    years_actual = (end_date - start_date).days / DAYS_PER_YEAR
    cagr = (end_nav / start_nav) ** (1.0 / years_actual) - 1.0
    return CagrPoint(cagr, start_date, start_nav, end_date, end_nav, years_actual)


def _fmt_pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100.0:+.2f}%"


def _load_external(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            out[row["scheme_code"].strip()] = row
    return out


def _delta_line(label: str, ours: float | None, ext_pct_str: str | None,
                source: str) -> str:
    if ours is None or not ext_pct_str:
        return f"    {label}: ours={_fmt_pct(ours)} external=- (no comparison)"
    ext = float(ext_pct_str) / 100.0
    delta_pp = (ours - ext) * 100.0
    verdict = "PASS" if abs(delta_pp) <= EXTERNAL_TOLERANCE_PP else "FAIL"
    return (
        f"    {label}: ours={_fmt_pct(ours)} external={_fmt_pct(ext)} "
        f"delta={delta_pp:+.2f}pp [{verdict} @ ±{EXTERNAL_TOLERANCE_PP}pp] "
        f"({source})"
    )


def run_schemes(codes: list[str], external_csv: Path | None) -> int:
    from mfs.db import queries as q

    as_of = q.latest_computed_metrics_date()
    metrics = (
        q.computed_metrics_for_schemes(as_of, codes) if as_of else None
    )
    master = q.scheme_master()
    names: dict[str, str] = {}
    if not master.is_empty():
        names = dict(
            master.select(["scheme_code", "scheme_name"]).iter_rows()
        )
    external = _load_external(external_csv) if external_csv else {}

    print(f"# cross_validate — our-side numbers (computed_metrics as_of={as_of})")
    print(f"# tolerance for external trailing-return comparison: "
          f"±{EXTERNAL_TOLERANCE_PP}pp")
    failures = 0
    for code in codes:
        nav_df = q.nav_series(code)
        series = (
            [(d, float(n)) for d, n in nav_df.iter_rows()]
            if not nav_df.is_empty()
            else []
        )
        print(f"\n{code}  {names.get(code, '<not in scheme_master>')}")
        if not series:
            print("    NO NAV HISTORY in nav_daily")
            failures += 1
            continue
        p3 = point_to_point_cagr(series, 3.0)
        p5 = point_to_point_cagr(series, 5.0)
        last_d, last_n = series[-1]
        print(f"    latest NAV: {last_n:.4f} on {last_d.isoformat()}")
        for label, p in (("3y point-to-point CAGR", p3),
                         ("5y point-to-point CAGR", p5)):
            if p is None:
                print(f"    {label}: unavailable (insufficient history "
                      f"within ±{NEAREST_NAV_TOLERANCE_DAYS}d of target)")
            else:
                print(f"    {label}: {_fmt_pct(p.cagr)}  "
                      f"[{p.start_date} nav {p.start_nav:.4f} -> "
                      f"{p.end_date} nav {p.end_nav:.4f}, "
                      f"{p.years_actual:.2f}y actual]")
        row = None
        if metrics is not None and not metrics.is_empty():
            hit = metrics.filter(metrics["scheme_code"] == code)
            row = hit.to_dicts()[0] if hit.height else None
        if row:
            print(f"    stored rolling medians: ret_3y_median="
                  f"{_fmt_pct(row['ret_3y_median'])} "
                  f"ret_5y_median={_fmt_pct(row['ret_5y_median'])} "
                  f"(rolling-window medians, NOT trailing returns)")
            conf = row["alpha_confidence"]
            print(f"    stored alpha_3y_annualized="
                  f"{_fmt_pct(row['alpha_3y_annualized'])} "
                  f"alpha_confidence="
                  f"{'-' if conf is None else f'{conf:.2f}'} "
                  f"vs {row['benchmark_ticker']}")
        else:
            print("    no computed_metrics row at latest as_of")
        ext = external.get(code)
        if ext:
            for label, ours, key in (
                ("3y vs external", p3.cagr if p3 else None, "ext_3y_cagr_pct"),
                ("5y vs external", p5.cagr if p5 else None, "ext_5y_cagr_pct"),
            ):
                line = _delta_line(label, ours, ext.get(key),
                                   ext.get("source", "?"))
                print(line)
                if "FAIL" in line:
                    failures += 1
    return 1 if failures else 0


def run_benchmark(ticker: str, dates: list[date]) -> int:
    from mfs.db import queries as q

    series = q.benchmark_series(ticker)
    if series.is_empty():
        print(f"{ticker}: no rows in benchmark_daily")
        return 1
    by_date = dict(series.iter_rows())
    print(f"# {ticker} TRI closes (compare vs niftyindices.com — exact match "
          f"expected)")
    missing = 0
    for d in dates:
        close = by_date.get(d)
        if close is None:
            print(f"  {d.isoformat()}: NO ROW (holiday/weekend or gap)")
            missing += 1
        else:
            print(f"  {d.isoformat()}: {float(close):.2f}")
    return 1 if missing else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--schemes", help="comma-separated scheme codes")
    ap.add_argument("--external", type=Path,
                    help="CSV of hand-entered external values: "
                         "scheme_code,ext_3y_cagr_pct,ext_5y_cagr_pct,source")
    ap.add_argument("--ticker", help="benchmark ticker for TRI-close mode")
    ap.add_argument("--dates", help="comma-separated YYYY-MM-DD dates "
                                    "(TRI-close mode)")
    args = ap.parse_args(argv)

    if args.ticker:
        if not args.dates:
            ap.error("--ticker requires --dates")
        dates = [date.fromisoformat(s.strip()) for s in args.dates.split(",")]
        return run_benchmark(args.ticker, dates)
    if not args.schemes:
        ap.error("provide --schemes or --ticker/--dates")
    codes = [c.strip() for c in args.schemes.split(",") if c.strip()]
    return run_schemes(codes, args.external)


if __name__ == "__main__":
    raise SystemExit(main())
