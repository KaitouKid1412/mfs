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
from mfs.errors import IngestError, StatementDateMismatchError
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

    # Resolve every printed name → (scheme_code, score) FIRST. When two
    # similarly-named sibling funds (e.g. "Nifty 50 Index" vs "Nifty Next 50
    # Index") both fuzzy-match the SAME scheme_code, keep ONLY the
    # highest-scoring printed name — otherwise both portfolios get written to
    # one scheme_code and its weights sum to ~200%. A losing duplicate is NOT
    # treated as unmatched (its real scheme just wasn't in scheme_master).
    best_for_code: dict[str, tuple[float, str]] = {}
    name_to_code: dict[str, str] = {}
    collisions: list[str] = []
    for scheme_name_printed in urls:
        mr = match_one(scheme_name_printed, candidates, threshold=match_threshold)
        if mr.matched_scheme_code is None:
            unmatched.append(scheme_name_printed)
            continue
        name_to_code[scheme_name_printed] = mr.matched_scheme_code
        prev = best_for_code.get(mr.matched_scheme_code)
        if prev is None or mr.score > prev[0]:
            if prev is not None:
                collisions.append(prev[1])
            best_for_code[mr.matched_scheme_code] = (mr.score, scheme_name_printed)
        else:
            collisions.append(scheme_name_printed)
    winners = {printed for _, printed in best_for_code.values()}
    if collisions:
        log.info(
            "holdings.match_collision_dropped",
            amc=amc_slug, n_dropped=len(collisions), names=collisions[:10],
        )

    for scheme_name_printed, url in urls.items():
        if scheme_name_printed not in winners:
            continue  # unmatched, or a lower-scoring duplicate for its code
        scheme_code = name_to_code[scheme_name_printed]
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
        except StatementDateMismatchError as e:
            # Wrong-month artifact (e.g. the endpoint served last month's
            # file for this month's id). Evict it from the month-keyed cache
            # — fetch_excel's exists() check would otherwise pin the wrong
            # file forever — and re-raise so the whole AMC aborts BEFORE its
            # DB transaction (fail-fast: absence over wrongness). run_all's
            # per-AMC isolation keeps the other AMCs running.
            excel_path.unlink(missing_ok=True)
            log.error(
                "holdings.month_mismatch",
                amc=amc_slug, scheme=scheme_name_printed,
                expected=e.expected_ym, found=sorted(e.found_yms),
                path=str(excel_path),
            )
            raise
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
                "scheme_code": scheme_code,
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

    # Idempotency: a successful run of an AMC is authoritative for that AMC's
    # rows in this month. Delete ALL existing (source_amc, as_of_month) rows
    # before inserting the fresh batch — this evicts stale rows the PK-upsert
    # can't (e.g. a security dropped from the portfolio, or a prior contaminated
    # run that merged two funds' holdings onto one orphaned scheme_code). Only
    # runs here AFTER discovery+parse succeed; a 0-URL discovery raises earlier,
    # so a failed fetch can never wipe good data.
    if matched_rows:
        from mfs.db.connection import connect
        # DELETE + upsert in ONE transaction so a kill between them can't leave
        # this (source_amc, month) partition empty for the next run.
        with connect() as conn:
            conn.execute(
                "DELETE FROM holdings_monthly "
                "WHERE as_of_month = %s AND source_amc = %s",
                (as_of_month, amc_slug),
            )
            n_written = w.upsert_holdings(pl.DataFrame(matched_rows), conn=conn)
    else:
        n_written = 0
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
    """Run every registered holdings adapter with per-AMC fault isolation.

    Holdings are an advisory signal: one AMC's failure (a 404 in disclosure
    discovery, a changed page layout, a parse error) is caught, recorded, and
    the run continues — it must NOT crash the pipeline. An uncaught
    ``httpx.HTTPStatusError`` from ``discover_scheme_urls`` is the exact 4xx that
    would otherwise escape the pipeline's ``_stage`` catch and abort the process.
    If EVERY adapter fails we raise IngestError, since that is systemic rather
    than per-AMC.
    """
    slugs = registered_adapters()
    if not slugs:
        raise IngestError("No holdings adapters registered.")
    results: dict[str, dict] = {}
    failed: list[str] = []
    for slug in slugs:
        try:
            results[slug] = run_for_amc(slug, ym=ym)
        except Exception as e:  # noqa: BLE001 — isolate one AMC; never crash the stage
            failed.append(slug)
            results[slug] = {
                "amc_slug": slug, "ym": ym,
                "error": str(e), "error_type": type(e).__name__,
                "rows_written": 0,
            }
            log.error("holdings.amc_failed", amc=slug,
                      err=str(e), err_type=type(e).__name__)
    if failed:
        log.warning("holdings.run_all.partial",
                    n_failed=len(failed), n_total=len(slugs), failed=failed)
    if len(failed) == len(slugs):
        raise IngestError(
            f"All {len(slugs)} holdings adapters failed — systemic network/config "
            f"issue, not per-AMC. Refusing to proceed silently. "
            f"First error: {results[slugs[0]].get('error')}"
        )
    return results
