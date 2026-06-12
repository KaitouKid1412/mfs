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

import statistics
from datetime import date, datetime
from pathlib import Path

import polars as pl

from mfs import paths
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

# B14b: month-over-month PTR unit-flip tripwire bounds. The AMC's incoming
# median PTR is refused only when it shifts BOTH >PTR_FLIP_RATIO-fold AND
# >PTR_FLIP_ABS absolute vs the previous stored month — the conjunction keeps
# tiny-base ratio noise (0.05 → 0.3) and large-base drift (1.0 → 2.0) writable
# while a percent-vs-fraction flip (0.09 → 9.14) is always caught.
PTR_FLIP_RATIO = 5.0
PTR_FLIP_ABS = 0.5


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


def _advisory_statement_check(pdf: Path, ym: str, amc_slug: str) -> dict:
    """ADVISORY statement-date check on the factsheet path (B2) — log-only.

    Scans page 1 of the factsheet for 'as on / Data as on <date>' phrases and
    compares the named month(s) against the requested data month. Factsheet
    front pages vary far too much for a hard gate (unlike the SEBI portfolio
    Excels), so a mismatch is a WARNING plus a verdict in the per-AMC result
    dict for Gate B to surface — never an abort.
    """
    # Local import: holdings._generic owns the shared 'as on' grammar; a
    # module-level import would couple the two packages' import order.
    from mfs.ingest.holdings._generic import statement_months_in_text

    try:
        import pdfplumber

        with pdfplumber.open(pdf) as doc:
            text = (doc.pages[0].extract_text() or "") if doc.pages else ""
    except Exception as e:  # noqa: BLE001 — advisory scan must never crash a run
        log.debug(
            "managers.statement_scan_unreadable",
            amc=amc_slug, path=str(pdf), err=str(e),
        )
        return {"status": "unreadable"}
    months = statement_months_in_text(text)
    if not months:
        return {"status": "absent"}
    if ym in months:
        return {"status": "ok", "found": sorted(months)}
    log.warning(
        "managers.statement_date_mismatch",
        amc=amc_slug, expected=ym, found=sorted(months), path=str(pdf),
    )
    return {"status": "mismatch", "found": sorted(months)}


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
      force: unified --full semantics (B9): re-download AND re-parse AND
        re-write. Deletes this data month's cached factsheet before fetch
        (so a corrected/re-published PDF is actually picked up — every
        ``fetch()`` implementation checks the canonical
        ``paths.factsheet_raw`` location), bypasses the parse-skip, and
        overrides the PTR partition-shrinkage guard (B13) with a loud log.
        Historical months are never touched.
    """
    adapter = get_adapter(amc_slug)
    ym = ym or _default_data_month()
    log.info("managers.run.start", amc=amc_slug, ym=ym)
    as_of_month = date(int(ym.split("-")[0]), int(ym.split("-")[1]), 1)

    # Step 1: fetch (or use override). force (--full) evicts the month's
    # cached artifact first (B9): fetch() returns a cached file by mere
    # existence, so a corrected/re-published factsheet is unreachable
    # without deletion. Scoped to ym only — historical months stay cached.
    if pdf_path is None:
        if force:
            cached = paths.factsheet_raw(amc_slug, ym)
            if cached.exists():
                cached.unlink()
                log.info(
                    "managers.force_refetch",
                    amc=amc_slug, ym=ym, path=str(cached),
                )
        pdf = adapter.fetch(ym)
    else:
        pdf = Path(pdf_path)
        if not pdf.exists():
            raise IngestError(f"Local PDF not found: {pdf}")

    # Step 1.5: advisory statement-date scan (B2) — log-only verdict carried
    # in the result dict; never gates the run.
    stmt_check = _advisory_statement_check(pdf, ym, amc_slug)

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
            "statement_date_check": stmt_check,
        }

    # Step 3: parse each optional signal. Adapters that don't override return ().
    holding_records = list(adapter.parse_holdings(pdf, ym))
    log.info("holdings.parsed", amc=amc_slug, n_records=len(holding_records))
    ptr_records = list(adapter.parse_ptr(pdf, ym))
    log.info("ptr.parsed", amc=amc_slug, n_records=len(ptr_records))

    # Step 4: resolve names to scheme_codes. Cross-name collisions (two
    # printed names → one scheme_code) are resolved by match score inside the
    # resolvers; parse order never decides data ownership. The per-portfolio
    # sanity gate (B3) needs canonical_category per code, so build that map
    # from the scheme_master frame already loaded above.
    cat_by_code: dict[str, str | None] = {}
    for r in sm.iter_rows(named=True):
        cat_by_code[r["scheme_code"]] = r.get("canonical_category")
    gate_skipped: dict[str, list[str]] = {
        "weight_sum_breach": [], "too_few_holdings": [],
    }
    matched_holdings = _resolve_holdings(
        holding_records, candidates, amc_slug, ym, match_threshold,
        cat_by_code=cat_by_code, gate_skipped=gate_skipped,
    )
    matched_ptr = _resolve_ptr(ptr_records, candidates, amc_slug, ym, match_threshold)

    # Step 4.5: dedupe by PK to avoid Postgres CardinalityViolation. After the
    # score-based collision resolution above this only fires when ONE printed
    # name legitimately repeats in the parse output (e.g. the same security at
    # two table depths) — identical-key repeats, not cross-scheme collisions.
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
        incoming_by_month: dict[date, set[str]] = {}
        ptrs_by_month: dict[date, list[float]] = {}
        for r in matched_ptr:
            incoming_by_month.setdefault(r["as_of_month"], set()).add(
                r["scheme_code"]
            )
            ptrs_by_month.setdefault(r["as_of_month"], []).append(
                float(r["ptr"])
            )
        ptr_months = sorted(incoming_by_month)
        with connect() as conn:
            # PTR unit-flip tripwire (B14b): a percent-vs-fraction convention
            # flip in a re-published factsheet (or a layout change picking the
            # adjacent column) writes ~100x-wrong PTR that the per-record
            # bounds (B14a, schemas.py) can't catch on their own. Compare
            # this batch's median against the AMC's previous stored month
            # (read-only): a shift that is BOTH >PTR_FLIP_RATIO-fold AND
            # >PTR_FLIP_ABS absolute is a convention flip, not turnover drift
            # → refuse the AMC. No prior month → nothing to compare, proceed.
            for m in ptr_months:
                row = conn.execute(
                    "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY ptr) "
                    "FROM portfolio_turnover_monthly "
                    "WHERE source_amc = %s AND as_of_month = ("
                    "SELECT MAX(as_of_month) FROM portfolio_turnover_monthly "
                    "WHERE source_amc = %s AND as_of_month < %s)",
                    (amc_slug, amc_slug, m),
                ).fetchone()
                prior_median = (
                    float(row[0])
                    if row is not None and row[0] is not None else None
                )
                if prior_median is None or prior_median <= 0:
                    continue
                incoming_median = statistics.median(ptrs_by_month[m])
                ratio = incoming_median / prior_median
                fold = max(ratio, 1.0 / ratio)
                if (
                    fold > PTR_FLIP_RATIO
                    and abs(incoming_median - prior_median) > PTR_FLIP_ABS
                ):
                    raise IngestError(
                        f"{amc_slug}: PTR unit-flip tripwire for {m} — "
                        f"incoming median {incoming_median:.4g} vs prior-month "
                        f"median {prior_median:.4g} ({fold:.1f}x shift). This "
                        f"looks like a percent-vs-fraction convention flip, "
                        f"not turnover drift; refusing this AMC's PTR batch. "
                        f"If the PRIOR month is the mis-scaled one, delete "
                        f"those rows and re-run."
                    )
            # Partition-shrinkage guard (B13): the per-month DELETE below
            # would silently drop a scheme whose PTR parsed last run but not
            # this run. Refuse before ANY month is deleted (IngestError →
            # rollback, DB untouched); ``force`` (--full) replaces loudly.
            for m in ptr_months:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT scheme_code) "
                    "FROM portfolio_turnover_monthly "
                    "WHERE as_of_month = %s AND source_amc = %s",
                    (m, amc_slug),
                ).fetchone()
                existing = int(row[0]) if row else 0
                if len(incoming_by_month[m]) < existing:
                    if not force:
                        raise IngestError(
                            f"{amc_slug}: refusing PTR partition shrinkage "
                            f"for {m} — incoming "
                            f"{len(incoming_by_month[m])} distinct schemes "
                            f"< {existing} already in DB. Re-run with "
                            f"--full to replace anyway."
                        )
                    log.warning(
                        "managers.ptr_partition_shrinkage_forced",
                        amc=amc_slug, month=str(m),
                        incoming=len(incoming_by_month[m]), existing=existing,
                    )
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
        statement_date_check=stmt_check.get("status"),
        n_weight_sum_skipped=len(gate_skipped["weight_sum_breach"]),
        n_too_few_holdings_skipped=len(gate_skipped["too_few_holdings"]),
    )
    return {
        "amc_slug": amc_slug,
        "ym": ym,
        "rows_written_holdings": n_holdings,
        "rows_written_ptr": n_ptr,
        "candidate_count": len(candidates),
        "statement_date_check": stmt_check,
        "weight_sum_skipped_schemes": gate_skipped["weight_sum_breach"],
        "too_few_holdings_skipped_schemes": gate_skipped["too_few_holdings"],
    }


def _winners_by_code(
    matches: dict[str, MatchResult],
    amc_slug: str,
    signal: str,
) -> set[str]:
    """Resolve two-printed-names → one-scheme_code collisions by match score.

    Mirrors the holdings orchestrator's rule (holdings/_run.py): when two
    similarly-named printed schemes fuzzy-match the SAME scheme_code, keep
    ONLY the highest-scoring printed name — otherwise both signals get
    written to one scheme_code (e.g. two funds' portfolios summing to ~200%,
    or one fund's PTR decided by parse order). The loser is dropped entirely
    (its real scheme just isn't in scheme_master) and logged loudly.
    """
    best_for_code: dict[str, tuple[float, str]] = {}
    dropped: list[str] = []
    for printed, mr in matches.items():
        if mr.matched_scheme_code is None:
            continue
        prev = best_for_code.get(mr.matched_scheme_code)
        if prev is None or mr.score > prev[0]:
            if prev is not None:
                dropped.append(prev[1])
            best_for_code[mr.matched_scheme_code] = (mr.score, printed)
        else:
            dropped.append(printed)
    if dropped:
        log.warning(
            "managers.match_collision_dropped",
            amc=amc_slug, signal=signal,
            n_dropped=len(dropped), names=dropped[:10],
        )
    return {printed for _, printed in best_for_code.values()}


def _resolve_holdings(
    records: list,
    candidates: dict[str, dict],
    amc_slug: str,
    ym: str,
    match_threshold: int = DEFAULT_THRESHOLD,
    *,
    cat_by_code: dict[str, str | None] | None = None,
    gate_skipped: dict[str, list[str]] | None = None,
) -> list[dict]:
    """Resolve printed scheme names → scheme_codes for holdings. Rows with
    no ISIN are dropped (per the Phase 2.2 locked decision).

    ``cat_by_code`` (scheme_code → canonical_category) enables the
    per-portfolio weight-sum + min-holdings sanity gate (B3) — same gate,
    same bounds as the holdings-Excel path. A failing scheme's ENTIRE
    printed portfolio is dropped; ``gate_skipped`` (verdict → printed names)
    collects the skips for the caller's run summary.
    """
    if not records:
        return []
    from mfs.schemas import ParsedHoldingRecord  # local import to avoid cycle
    now = datetime.utcnow()
    as_of_month = date(int(ym.split("-")[0]), int(ym.split("-")[1]), 1)
    # Pass 1: match every distinct printed name once, then keep only the
    # best-scoring printed name per scheme_code (see _winners_by_code).
    cache: dict[str, MatchResult] = {}
    for rec in records:
        if not isinstance(rec, ParsedHoldingRecord) or not rec.isin:
            continue
        if rec.scheme_name_printed not in cache:
            cache[rec.scheme_name_printed] = match_one(
                rec.scheme_name_printed, candidates, threshold=match_threshold,
            )
    winners = _winners_by_code(cache, amc_slug, signal="holdings")
    out: list[dict] = []
    rows_by_printed: dict[str, list[dict]] = {}
    for rec in records:
        if not isinstance(rec, ParsedHoldingRecord):
            continue
        if not rec.isin:
            continue  # skip ISIN-less rows
        if rec.scheme_name_printed not in winners:
            continue  # unmatched, or a lower-scoring duplicate for its code
        mr = cache[rec.scheme_name_printed]
        row = {
            "scheme_code": mr.matched_scheme_code,
            "isin": rec.isin,
            "as_of_month": as_of_month,
            "weight_pct": rec.weight_pct,
            "security_name": rec.security_name,
            "instrument_type": rec.instrument_type,
            "source_amc": amc_slug,
            "computed_at": now,
        }
        out.append(row)
        rows_by_printed.setdefault(rec.scheme_name_printed, []).append(row)

    # Per-portfolio sanity gate (B3) — see holdings/_run.check_portfolio_sanity
    # for the bounds and their live-DB calibration rationale.
    if cat_by_code:
        from mfs.ingest.holdings._run import check_portfolio_sanity

        bad_printed: set[str] = set()
        for printed, rows in rows_by_printed.items():
            mr = cache[printed]
            verdict = check_portfolio_sanity(
                scheme_code=mr.matched_scheme_code,
                printed_name=printed,
                master_name=mr.matched_scheme_name,
                category=cat_by_code.get(mr.matched_scheme_code),
                total_weight=sum(float(r["weight_pct"]) for r in rows),
                n_rows=len(rows),
                amc_slug=amc_slug,
            )
            if verdict != "ok":
                bad_printed.add(printed)
                if gate_skipped is not None:
                    gate_skipped.setdefault(verdict, []).append(printed)
        if bad_printed:
            bad_rows = {id(r) for p in bad_printed for r in rows_by_printed[p]}
            out = [r for r in out if id(r) not in bad_rows]
    return out


def _resolve_ptr(
    records: list,
    candidates: dict[str, dict],
    amc_slug: str,
    ym: str,
    match_threshold: int = DEFAULT_THRESHOLD,
) -> list[dict]:
    """Resolve printed scheme names → scheme_codes for PTR records."""
    if not records:
        return []
    from mfs.schemas import ParsedPtrRecord
    now = datetime.utcnow()
    as_of_default = date(int(ym.split("-")[0]), int(ym.split("-")[1]), 1)
    # Pass 1: match every distinct printed name once, then keep only the
    # best-scoring printed name per scheme_code (see _winners_by_code).
    cache: dict[str, MatchResult] = {}
    for rec in records:
        if not isinstance(rec, ParsedPtrRecord):
            continue
        if rec.scheme_name_printed not in cache:
            cache[rec.scheme_name_printed] = match_one(
                rec.scheme_name_printed, candidates, threshold=match_threshold,
            )
    winners = _winners_by_code(cache, amc_slug, signal="ptr")
    out: list[dict] = []
    for rec in records:
        if not isinstance(rec, ParsedPtrRecord):
            continue
        if rec.scheme_name_printed not in winners:
            continue  # unmatched, or a lower-scoring duplicate for its code
        mr = cache[rec.scheme_name_printed]
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

    ``force=True`` (a ``--full`` pipeline run) re-downloads the current data
    month's factsheet for every AMC, re-parses it even if unchanged, and
    re-writes (overriding the partition-shrinkage guard) — see
    ``run_for_amc``. The default skips re-parsing factsheets whose bytes and
    match universe are identical to the last successful ingest. Cost of
    force: ~41 factsheets re-fetched (minutes serially).

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
