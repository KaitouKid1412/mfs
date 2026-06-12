"""Thin operator wrapper over ``mfs.ingest.constituents.derive`` (D9).

The derivation core lives in ``src/mfs/ingest/constituents/derive.py`` and
runs automatically as the pipeline stage 'derive constituents (latest month)'.
This wrapper exists for operator-driven runs: backfilling a specific month or
re-deriving after a tracker fix. After deriving, ingest the CSVs with
``uv run mfs ingest constituents``.

Usage:
    uv run python tools/derive_constituents.py [--ym 2026-05] [--dry-run]

Omit --ym to derive the most recent holdings data month (what the pipeline
stage does).
"""

from __future__ import annotations

import argparse

from mfs.ingest.constituents import derive
from mfs.utils.logging import configure_logging


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--ym", default=None,
        help="Data month YYYY-MM (default: latest month in holdings_monthly)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    configure_logging()
    ym = args.ym or derive.latest_holdings_ym()
    result = derive.derive_month(ym, dry_run=args.dry_run)
    print(derive.render_summary(result))
    if args.dry_run:
        print("(dry-run, nothing written)")
    else:
        print("Next: uv run mfs ingest constituents")


if __name__ == "__main__":
    main()
