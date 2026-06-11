"""Top-level orchestration for the factsheet-based ingest stage.

For each registered AMC, this fetches the monthly factsheet PDF once and
extracts two signals from it: portfolio holdings (no ISIN — Phase 3.C
provides the ISIN-tagged Excel path) and PTR (one number per scheme).
Manager-tenure extraction was removed when the user opted to verify manager
tenure manually for Stage 2 survivors. AUM is sourced exclusively from
AMFI's quarterly AAUM endpoint (see ``mfs.ingest.amfi_aum``).

The package path is kept at ``mfs.ingest.managers`` for backward
compatibility with existing imports.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl

from mfs.db import queries as q
from mfs.db import writers as w
from mfs.errors import IngestError
from mfs.ingest import _parse_cache
from mfs.ingest.managers._registry import get_adapter, registered_adapters
from mfs.ingest.managers._scheme_match import (
    DEFAULT_THRESHOLD,
    MatchResult,
    build_candidate_index,
    match_one,
)
from mfs.utils.logging import get_logger

log = get_logger(__name__)


def _dedupe_by_keys(rows: list[dict], key_cols: tuple[str, ...]) -> list[dict]:
    """Drop later rows whose key tuple already appeared earlier in `rows`.

    Postgres' ``ON CONFLICT DO UPDATE`` cannot affect the same destination
    row twice in a single INSERT, so any in-batch PK collision raises
    ``CardinalityViolation``. Adapters sometimes produce these when two
    slightly different printed scheme names fuzzy-match to the same
    ``scheme_code`` for the same security/month — we keep the first
    occurrence rather than silently merging values.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    n_drops = 0
    for r in rows:
        k = tuple(r.get(c) for c in key_cols)
        if k in seen:
            n_drops += 1
            continue
        seen.add(k)
        out.append(r)
    if n_drops:
        log.info("ingest.dedupe", n_dropped=n_drops, keys=list(key_cols))
    return out


def _default_data_month(today: date | None = None) -> str:
    """Return YYYY-MM for the most recent complete month (AMFI publishes by the
    10th of the next month, so 'previous calendar month' is a safe default).
    """
    d = today or date.today()
    if d.month == 1:
        return f"{d.year - 1:04d}-12"
    return f"{d.year:04d}-{d.month - 1:02d}"


def run_for_amc(
    amc_slug: str,
    ym: str | None = None,
    pdf_path: Path | None = None,
    match_threshold: int = DEFAULT_THRESHOLD,
    force: bool = False,
) -> dict:
    """Run one AMC's factsheet adapter end-to-end.

    Fetches the monthly factsheet PDF (cached) and extracts holdings, PTR
    and AUM. Each signal is fuzzy-matched against scheme_master and upserted
    into its respective table.

    Args:
      amc_slug: registered AMC slug (e.g. 'hdfc'). Must resolve to a
        scheme_master amc_code via ``_scheme_match`` aliasing.
      ym: data month YYYY-MM. Defaults to last calendar month.
      pdf_path: optional override — skip fetch and parse this local file.
                Useful for re-running against an archived PDF or for tests.
      match_threshold: rapidfuzz score floor for accepting a scheme-name match.
    """
    adapter = get_adapter(amc_slug)
    ym = ym or _default_data_month()
    log.info("managers.run.start", amc=amc_slug, ym=ym)
    as_of_month = date(int(ym.split("-")[0]), int(ym.split("-")[1]), 1)

    # Step 1: fetch (or use override).
    if pdf_path is None:
        pdf = adapter.fetch(ym)
    else:
        pdf = Path(pdf_path)
        if not pdf.exists():
            raise IngestError(f"Local PDF not found: {pdf}")

    # Step 2: build candidate index from scheme_master (drives all matching).
    # Built before parsing so it can also fingerprint the match universe for the
    # parse-skip check below.
    sm = q.scheme_master()
    candidates = build_candidate_index(sm, amc_slug)
    if not candidates:
        raise IngestError(
            f"{amc_slug}: scheme_master has no DIRECT+GROWTH schemes for "
            f"amc_code={amc_slug!r}. Run `mfs build scheme-master` first or "
            "verify the adapter's amc_slug matches the scheme_master code."
        )

    # Step 2.5: incremental parse-skip. If the factsheet bytes AND the match
    # universe are byte-identical to the last successful ingest, and the DB
    # already holds those rows, re-parsing + DELETE-reinsert is a provable no-op
    # — skip the expensive pdfplumber parse. `force` (a --full run) always
    # re-parses; an explicit pdf_path override never skips (caller wants it
    # parsed). The universe fingerprint closes the scheme_master-coupling hole:
    # if matching would re-route, the fingerprint differs and we re-parse.
    universe_fp = _parse_cache.fingerprint(
        f"{name}={info['scheme_code']}" for name, info in candidates.items()
    )
    if (
        not force and pdf_path is None
        and _parse_cache.should_skip_parse(
            pdf, universe_fp,
            db_has_rows=q.has_factsheet_rows(amc_slug, as_of_month),
        )
    ):
        log.info("managers.run.skip_unchanged", amc=amc_slug, ym=ym)
        return {
            "amc_slug": amc_slug, "ym": ym,
            "rows_written_holdings": 0, "rows_written_ptr": 0,
            "candidate_count": len(candidates), "skipped": True,
        }

    # Step 3: parse each optional signal. Adapters that don't override return ().
    holding_records = list(adapter.parse_holdings(pdf, ym))
    log.info("holdings.parsed", amc=amc_slug, n_records=len(holding_records))
    ptr_records = list(adapter.parse_ptr(pdf, ym))
    log.info("ptr.parsed", amc=amc_slug, n_records=len(ptr_records))

    # Step 4: resolve names to scheme_codes.
    matched_holdings = _resolve_holdings(holding_records, candidates, amc_slug, ym)
    matched_ptr = _resolve_ptr(ptr_records, candidates, amc_slug, ym)

    # Step 4.5: dedupe by PK to avoid Postgres CardinalityViolation.
    matched_holdings = _dedupe_by_keys(
        matched_holdings, ("scheme_code", "security_name", "as_of_month"),
    )
    matched_ptr = _dedupe_by_keys(matched_ptr, ("scheme_code", "as_of_month"))

    # Step 4.6 / Step 5: write each table. Holdings (factsheet path) is a plain
    # idempotent upsert. PTR is authoritative per (source_amc, as_of_month): we
    # DELETE the existing rows then upsert the fresh batch — in ONE transaction,
    # so a kill between the delete and the insert can't leave this AMC's PTR for
    # the month wiped. The delete also evicts stale rows the PK-upsert can't
    # (e.g. a prior mis-match that assigned a PTR to the wrong sibling
    # scheme_code). Guarded by ``if matched_ptr`` so an empty parse never wipes
    # good data.
    n_holdings = w.upsert_holdings(pl.DataFrame(matched_holdings)) if matched_holdings else 0
    if matched_ptr:
        from mfs.db.connection import connect
        ptr_months = sorted({r["as_of_month"] for r in matched_ptr})
        with connect() as conn:
            for m in ptr_months:
                conn.execute(
                    "DELETE FROM portfolio_turnover_monthly "
                    "WHERE as_of_month = %s AND source_amc = %s",
                    (m, amc_slug),
                )
            n_ptr = w.upsert_portfolio_turnover(pl.DataFrame(matched_ptr), conn=conn)
    else:
        n_ptr = 0

    # Step 5.5: record this successful ingest so a byte-identical re-run (same
    # factsheet + same match universe) can skip the re-parse next time. Written
    # only after the rows are committed; a crash before here leaves no marker, so
    # the next run re-parses. Skipped for the pdf_path override path.
    if pdf_path is None and (n_holdings or n_ptr):
        _parse_cache.write_marker(
            pdf, universe_fp,
            {"ym": ym, "n_holdings": n_holdings, "n_ptr": n_ptr},
        )

    log.info(
        "managers.run.done",
        amc=amc_slug, ym=ym,
        rows_written_holdings=n_holdings,
        rows_written_ptr=n_ptr,
    )
    return {
        "amc_slug": amc_slug,
        "ym": ym,
        "rows_written_holdings": n_holdings,
        "rows_written_ptr": n_ptr,
        "candidate_count": len(candidates),
    }


def _resolve_holdings(
    records: list,
    candidates: dict[str, dict],
    amc_slug: str,
    ym: str,
) -> list[dict]:
    """Resolve printed scheme names → scheme_codes for holdings. Rows with
    no ISIN are dropped (per the Phase 2.2 locked decision)."""
    if not records:
        return []
    from mfs.schemas import ParsedHoldingRecord  # local import to avoid cycle
    now = datetime.utcnow()
    as_of_month = date(int(ym.split("-")[0]), int(ym.split("-")[1]), 1)
    cache: dict[str, MatchResult] = {}
    out: list[dict] = []
    for rec in records:
        if not isinstance(rec, ParsedHoldingRecord):
            continue
        if not rec.isin:
            continue  # skip ISIN-less rows
        mr = cache.get(rec.scheme_name_printed)
        if mr is None:
            mr = match_one(rec.scheme_name_printed, candidates)
            cache[rec.scheme_name_printed] = mr
        if mr.matched_scheme_code is None:
            continue
        out.append({
            "scheme_code": mr.matched_scheme_code,
            "isin": rec.isin,
            "as_of_month": as_of_month,
            "weight_pct": rec.weight_pct,
            "security_name": rec.security_name,
            "instrument_type": rec.instrument_type,
            "source_amc": amc_slug,
            "computed_at": now,
        })
    return out


def _resolve_ptr(
    records: list,
    candidates: dict[str, dict],
    amc_slug: str,
    ym: str,
) -> list[dict]:
    """Resolve printed scheme names → scheme_codes for PTR records."""
    if not records:
        return []
    from mfs.schemas import ParsedPtrRecord
    now = datetime.utcnow()
    as_of_default = date(int(ym.split("-")[0]), int(ym.split("-")[1]), 1)
    cache: dict[str, MatchResult] = {}
    out: list[dict] = []
    for rec in records:
        if not isinstance(rec, ParsedPtrRecord):
            continue
        mr = cache.get(rec.scheme_name_printed)
        if mr is None:
            mr = match_one(rec.scheme_name_printed, candidates)
            cache[rec.scheme_name_printed] = mr
        if mr.matched_scheme_code is None:
            continue
        # Adapters sourcing PTR from a less-frequent document (e.g. quant's
        # abridged annual report) stamp the record's true period-end; everything
        # else falls back to the run's data month.
        out.append({
            "scheme_code": mr.matched_scheme_code,
            "as_of_month": rec.as_of_month or as_of_default,
            "ptr": float(rec.ptr),
            "source_amc": amc_slug,
            "computed_at": now,
        })
    return out


def run_all(
    ym: str | None = None,
    match_threshold: int = DEFAULT_THRESHOLD,
    force: bool = False,
) -> dict[str, dict]:
    """Run every registered factsheet adapter with per-AMC fault isolation.

    ``force=True`` (a ``--full`` pipeline run) re-parses every factsheet even if
    unchanged; the default skips re-parsing factsheets whose bytes and match
    universe are identical to the last successful ingest (see ``run_for_amc``).

    Holdings/PTR are advisory signals, so one AMC's failure (a 404 on the
    factsheet URL, a parse error, a layout change) must NOT crash the pipeline —
    it is caught, recorded as that AMC's error, and the run continues. This also
    contains an uncaught ``httpx.HTTPStatusError`` (e.g. a 4xx, which is neither
    a TransientHttpError nor a PipelineError, so it would otherwise escape the
    pipeline's ``_stage`` catch and abort the whole process).

    Fail-fast is preserved for SYSTEMIC failure: if *every* adapter fails, that
    is a network/config problem rather than a per-AMC hiccup, so we raise
    IngestError instead of proceeding silently. The Phase-2 coverage gate
    surfaces the per-AMC gaps recorded here.
    """
    slugs = registered_adapters()
    if not slugs:
        raise IngestError("No factsheet adapters registered.")
    results: dict[str, dict] = {}
    failed: list[str] = []
    for slug in slugs:
        try:
            results[slug] = run_for_amc(
                slug, ym=ym, match_threshold=match_threshold, force=force,
            )
        except Exception as e:  # noqa: BLE001 — isolate one AMC; never crash the stage
            failed.append(slug)
            results[slug] = {
                "amc_slug": slug, "ym": ym,
                "error": str(e), "error_type": type(e).__name__,
                "rows_written_holdings": 0, "rows_written_ptr": 0,
            }
            log.error("managers.amc_failed", amc=slug,
                      err=str(e), err_type=type(e).__name__)
    if failed:
        log.warning("managers.run_all.partial",
                    n_failed=len(failed), n_total=len(slugs), failed=failed)
    if len(failed) == len(slugs):
        raise IngestError(
            f"All {len(slugs)} factsheet adapters failed — systemic network/config "
            f"issue, not per-AMC. Refusing to proceed silently. "
            f"First error: {results[slugs[0]].get('error')}"
        )
    return results
