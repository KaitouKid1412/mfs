"""Top-level orchestration for the per-AMC holdings ingest stage (Phase 3.C).

For one AMC and one data month:
  1. Discover per-scheme Excel URLs from the AMC's disclosures page.
  2. Download each (cached on disk).
  3. Parse each into ParsedHoldingRecord rows (ISIN-tagged).
  4. Fuzzy-match printed scheme names → scheme_codes via the existing matcher.
  5. Upsert into holdings_monthly with source_amc=<amc> and ISIN populated.

This is structurally similar to managers/_run.py but with one Excel per
scheme rather than one PDF per AMC.
"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl

from mfs.db import queries as q
from mfs.db import writers as w
from mfs.errors import IngestError
from mfs.ingest.holdings._registry import get_adapter, registered_adapters
from mfs.ingest.managers._scheme_match import (
    DEFAULT_THRESHOLD,
    build_candidate_index,
    match_one,
)
from mfs.utils.logging import get_logger

log = get_logger(__name__)


def _default_data_month(today: date | None = None) -> str:
    d = today or date.today()
    if d.month == 1:
        return f"{d.year - 1:04d}-12"
    return f"{d.year:04d}-{d.month - 1:02d}"


def run_for_amc(
    amc_slug: str,
    ym: str | None = None,
    match_threshold: int = DEFAULT_THRESHOLD,
) -> dict:
    """Run one AMC's holdings adapter end-to-end."""
    adapter = get_adapter(amc_slug)
    ym = ym or _default_data_month()
    log.info("holdings.run.start", amc=amc_slug, ym=ym)

    urls = adapter.discover_scheme_urls(ym)
    if not urls:
        raise IngestError(
            f"{amc_slug}: 0 scheme Excel URLs discovered for ym={ym}. "
            "The disclosures page layout or URL pattern may have changed."
        )

    sm = q.scheme_master()
    candidates = build_candidate_index(sm, amc_slug)
    if not candidates:
        raise IngestError(
            f"{amc_slug}: scheme_master has no DIRECT+GROWTH schemes for "
            f"amc_code={amc_slug!r}. Run `mfs build scheme-master` first."
        )

    now = datetime.utcnow()
    as_of_month = date(int(ym.split("-")[0]), int(ym.split("-")[1]), 1)

    matched_rows: list[dict] = []
    unmatched: list[str] = []
    n_excels_parsed = 0
    n_records_total = 0

    for scheme_name_printed, url in urls.items():
        mr = match_one(scheme_name_printed, candidates, threshold=match_threshold)
        if mr.matched_scheme_code is None:
            unmatched.append(scheme_name_printed)
            continue
        # Download + parse Excel only for matched schemes (saves bandwidth on
        # ETFs/FoFs that aren't ranked).
        excel_filename = f"{scheme_name_printed}.xlsx"
        try:
            excel_path = adapter.fetch_excel(url, excel_filename, ym)
        except Exception as e:  # noqa: BLE001
            log.error(
                "holdings.fetch_failed",
                amc=amc_slug, scheme=scheme_name_printed, url=url, err=str(e),
            )
            continue
        n_excels_parsed += 1
        try:
            records = list(adapter.parse_excel(excel_path, scheme_name_printed, ym))
        except Exception as e:  # noqa: BLE001
            log.error(
                "holdings.parse_failed",
                amc=amc_slug, scheme=scheme_name_printed,
                path=str(excel_path), err=str(e),
            )
            continue
        n_records_total += len(records)
        for rec in records:
            matched_rows.append({
                "scheme_code": mr.matched_scheme_code,
                "security_name": rec.security_name,
                "as_of_month": as_of_month,
                "weight_pct": float(rec.weight_pct),
                "isin": rec.isin,
                "instrument_type": rec.instrument_type,
                "source_amc": amc_slug,
                "computed_at": now,
            })

    # In-batch dedupe by primary key. Two scheme_name_printed values
    # fuzzy-matching to the same scheme_code (or one Excel listing the
    # same security at two depths) can collide on
    # (scheme_code, security_name, as_of_month); Postgres' ON CONFLICT
    # DO UPDATE can't touch the same row twice in one INSERT. Keep the
    # first occurrence — order is parser output order (stable).
    seen: set[tuple] = set()
    deduped_rows: list[dict] = []
    n_dropped = 0
    for r in matched_rows:
        k = (r["scheme_code"], r["security_name"], r["as_of_month"])
        if k in seen:
            n_dropped += 1
            continue
        seen.add(k)
        deduped_rows.append(r)
    if n_dropped:
        log.info(
            "holdings.dedupe",
            amc=amc_slug, n_dropped=n_dropped,
            n_kept=len(deduped_rows),
        )
    matched_rows = deduped_rows

    n_written = (
        w.upsert_holdings(pl.DataFrame(matched_rows)) if matched_rows else 0
    )
    log.info(
        "holdings.run.done",
        amc=amc_slug, ym=ym,
        n_schemes_discovered=len(urls),
        n_schemes_parsed=n_excels_parsed,
        n_records=n_records_total,
        rows_written=n_written,
        unmatched_unique_schemes=len(unmatched),
    )
    return {
        "amc_slug": amc_slug,
        "ym": ym,
        "n_schemes_discovered": len(urls),
        "n_schemes_parsed": n_excels_parsed,
        "n_records": n_records_total,
        "rows_written": n_written,
        "unmatched_schemes": unmatched,
    }


def run_all(ym: str | None = None) -> dict[str, dict]:
    """Run every registered holdings adapter. Halt-on-first-failure."""
    slugs = registered_adapters()
    if not slugs:
        raise IngestError("No holdings adapters registered.")
    results: dict[str, dict] = {}
    for slug in slugs:
        results[slug] = run_for_amc(slug, ym=ym)
    return results
