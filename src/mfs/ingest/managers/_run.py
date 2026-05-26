"""Top-level orchestration for the factsheet-based ingest stage.

For each registered AMC, this fetches the monthly factsheet PDF once and
extracts three signals from it: portfolio holdings (no ISIN — Phase 3.C
provides the ISIN-tagged Excel path), PTR (one number per scheme), and AUM
(one number per scheme). Manager-tenure extraction was removed when the
user opted to verify manager tenure manually for Stage 2 survivors.

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

    # Step 1: fetch (or use override).
    if pdf_path is None:
        pdf = adapter.fetch(ym)
    else:
        pdf = Path(pdf_path)
        if not pdf.exists():
            raise IngestError(f"Local PDF not found: {pdf}")

    # Step 2: parse each optional signal. Adapters that don't override return ().
    holding_records = list(adapter.parse_holdings(pdf, ym))
    log.info("holdings.parsed", amc=amc_slug, n_records=len(holding_records))
    ptr_records = list(adapter.parse_ptr(pdf, ym))
    log.info("ptr.parsed", amc=amc_slug, n_records=len(ptr_records))
    aum_records = list(adapter.parse_aum(pdf, ym))
    log.info("aum.parsed", amc=amc_slug, n_records=len(aum_records))

    # Step 3: build candidate index from scheme_master and fuzzy-match. The
    # same index is reused across all record types.
    sm = q.scheme_master()
    candidates = build_candidate_index(sm, amc_slug)
    if not candidates:
        raise IngestError(
            f"{amc_slug}: scheme_master has no DIRECT+GROWTH schemes for "
            f"amc_code={amc_slug!r}. Run `mfs build scheme-master` first or "
            "verify the adapter's amc_slug matches the scheme_master code."
        )

    # Step 4: resolve names to scheme_codes.
    matched_holdings = _resolve_holdings(holding_records, candidates, amc_slug, ym)
    matched_ptr = _resolve_ptr(ptr_records, candidates, amc_slug, ym)
    matched_aum = _resolve_aum(aum_records, candidates, amc_slug, ym)

    # Step 4.5: dedupe by PK to avoid Postgres CardinalityViolation.
    matched_holdings = _dedupe_by_keys(
        matched_holdings, ("scheme_code", "security_name", "as_of_month"),
    )
    matched_ptr = _dedupe_by_keys(matched_ptr, ("scheme_code", "as_of_month"))
    matched_aum = _dedupe_by_keys(matched_aum, ("scheme_code", "as_of_month"))

    # Step 5: write each table.
    n_holdings = w.upsert_holdings(pl.DataFrame(matched_holdings)) if matched_holdings else 0
    n_ptr = w.upsert_portfolio_turnover(pl.DataFrame(matched_ptr)) if matched_ptr else 0
    n_aum = w.upsert_scheme_aum(pl.DataFrame(matched_aum)) if matched_aum else 0

    log.info(
        "managers.run.done",
        amc=amc_slug, ym=ym,
        rows_written_holdings=n_holdings,
        rows_written_ptr=n_ptr,
        rows_written_aum=n_aum,
    )
    return {
        "amc_slug": amc_slug,
        "ym": ym,
        "rows_written_holdings": n_holdings,
        "rows_written_ptr": n_ptr,
        "rows_written_aum": n_aum,
        "candidate_count": len(candidates),
    }


def _resolve_aum(
    records: list,
    candidates: dict[str, dict],
    amc_slug: str,
    ym: str,
) -> list[dict]:
    if not records:
        return []
    from mfs.schemas import ParsedAumRecord
    now = datetime.utcnow()
    as_of_month = date(int(ym.split("-")[0]), int(ym.split("-")[1]), 1)
    cache: dict[str, MatchResult] = {}
    out: list[dict] = []
    for rec in records:
        if not isinstance(rec, ParsedAumRecord):
            continue
        mr = cache.get(rec.scheme_name_printed)
        if mr is None:
            mr = match_one(rec.scheme_name_printed, candidates)
            cache[rec.scheme_name_printed] = mr
        if mr.matched_scheme_code is None:
            continue
        out.append({
            "scheme_code": mr.matched_scheme_code,
            "as_of_month": as_of_month,
            "aum_crore": float(rec.aum_crore),
            "source_amc": amc_slug,
            "computed_at": now,
        })
    return out


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
    as_of_month = date(int(ym.split("-")[0]), int(ym.split("-")[1]), 1)
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
        out.append({
            "scheme_code": mr.matched_scheme_code,
            "as_of_month": as_of_month,
            "ptr": float(rec.ptr),
            "source_amc": amc_slug,
            "computed_at": now,
        })
    return out


def run_all(
    ym: str | None = None,
    match_threshold: int = DEFAULT_THRESHOLD,
) -> dict[str, dict]:
    """Run every registered factsheet adapter. Halt-on-first-failure semantics:
    any adapter raising IngestError aborts the stage (consistent with the
    fail-fast invariant). Returns per-AMC summary dicts.
    """
    slugs = registered_adapters()
    if not slugs:
        raise IngestError("No factsheet adapters registered.")
    results: dict[str, dict] = {}
    for slug in slugs:
        results[slug] = run_for_amc(slug, ym=ym, match_threshold=match_threshold)
    return results
