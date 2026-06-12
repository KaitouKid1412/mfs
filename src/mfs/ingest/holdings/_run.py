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

import re
from datetime import date, datetime
from pathlib import Path

import polars as pl

from mfs import paths
from mfs.db import queries as q
from mfs.db import writers as w
from mfs.errors import IngestError, StatementDateMismatchError
from mfs.ingest import _parse_cache
from mfs.ingest._common import _dedupe_by_keys, _default_data_month, run_all_isolated
from mfs.ingest._scheme_match import (
    DEFAULT_THRESHOLD,
    build_candidate_index,
    match_one,
)
from mfs.ingest.holdings._generic import artifact_statement_months
from mfs.ingest.holdings._registry import get_adapter, registered_adapters
from mfs.utils.logging import get_logger

log = get_logger(__name__)


# --- Per-portfolio sanity gate (B3) ----------------------------------------
#
# Bounds are EMPIRICAL, not the 95-105 a clean spec would suggest. ISIN-less
# cash / TREPS / derivative rows are dropped at parse, so a complete
# portfolio's ISIN-bearing weight sum lands well under 100 for cash- and
# derivative-heavy categories. Live-DB calibration (read-only, 2026-06-12,
# 1,172 rankable scheme-months): sums span 47.9 (Thematic) to 108.8 (PSU);
# 318 of 1,172 sit in [60, 95) and 851 in [95, 105]; 0 below 40 and 0 above
# 115. A 95-105 gate would discard ~27% of good months; [40, 115] discards
# only true mis-scaling (the audit's 120-186% contaminated months) while the
# [40,60) / (105,115] shoulders WARN so drift toward the hard bounds is
# visible before it costs coverage.
WEIGHT_SUM_HARD_MIN = 40.0
WEIGHT_SUM_HARD_MAX = 115.0
WEIGHT_SUM_WARN_LOW = 60.0  # [40, 60) writes, but loudly
WEIGHT_SUM_WARN_HIGH = 105.0  # (105, 115] writes, but loudly

# Generic net for the Motilal-class contamination: the poached 152651 April
# row-set was exactly 4 ETF rows written to an active equity fund. Every
# rankable canonical_category is equity-oriented (incl. the hybrid trio —
# live min holdings count per rankable scheme-month is 12), so the floor
# applies to all of them. FoF-flavoured MASTER names are exempt: a genuine
# FoF legitimately holds a handful of funds, and judging by the MATCHED
# scheme's name (not the printed one) is what catches a FoF printed name
# poaching a non-FoF scheme_code.
MIN_EQUITY_HOLDINGS = 5
_FOF_NAME_RE = re.compile(r"(?i)fof|fund\s+of\s+fund")


def check_portfolio_sanity(
    *,
    scheme_code: str,
    printed_name: str,
    master_name: str | None,
    category: str | None,
    total_weight: float,
    n_rows: int,
    amc_slug: str,
) -> str:
    """Per-portfolio weight-sum + min-holdings gate at ingest (B3).

    Returns ``"ok"`` (write), ``"weight_sum_breach"`` or
    ``"too_few_holdings"`` (caller must SKIP the scheme's rows — per the
    no-half-data invariant a mis-scaled or contaminated portfolio is worse
    than a missing one). Applies only to rankable schemes
    (``category`` is not None); shared by the holdings-Excel and the
    managers/factsheet ingest paths.
    """
    if category is None:
        return "ok"  # not rankable — ETFs/debt legitimately sum anywhere
    if not (WEIGHT_SUM_HARD_MIN <= total_weight <= WEIGHT_SUM_HARD_MAX):
        log.error(
            "holdings.weight_sum_breach",
            amc=amc_slug, scheme=printed_name, scheme_code=scheme_code,
            category=category, weight_sum=round(total_weight, 2),
            bounds=[WEIGHT_SUM_HARD_MIN, WEIGHT_SUM_HARD_MAX],
        )
        return "weight_sum_breach"
    if total_weight < WEIGHT_SUM_WARN_LOW or total_weight > WEIGHT_SUM_WARN_HIGH:
        log.warning(
            "holdings.weight_sum_suspect",
            amc=amc_slug, scheme=printed_name, scheme_code=scheme_code,
            category=category, weight_sum=round(total_weight, 2),
        )
    if n_rows < MIN_EQUITY_HOLDINGS and not _FOF_NAME_RE.search(
        master_name or printed_name
    ):
        log.error(
            "holdings.too_few_holdings",
            amc=amc_slug, scheme=printed_name, scheme_code=scheme_code,
            category=category, n_rows=n_rows, floor=MIN_EQUITY_HOLDINGS,
        )
        return "too_few_holdings"
    return "ok"


def _statement_months_for(
    adapter, excel_path: Path, cache: dict[Path, set[str] | None],
) -> set[str] | None:
    """Memoized central banner scan for one artifact (B2).

    Consolidated workbooks (tata/icici/uti style) are parsed once per scheme
    but scanned once per file. An adapter whose banner is nonstandard can
    override extraction by defining ``artifact_statement_months(path)``;
    none needs to today (tata's numeric 'as on 30-04-2026' banners are
    handled by the shared regex), but the hook is the documented extension
    point. A scan failure means 'cannot check' (None), never a crash.
    """
    if excel_path not in cache:
        override = getattr(adapter, "artifact_statement_months", None)
        try:
            cache[excel_path] = (
                override(excel_path) if override is not None
                else artifact_statement_months(excel_path)
            )
        except Exception as e:  # noqa: BLE001 — advisory scan must not crash
            log.debug(
                "holdings.statement_scan_failed",
                amc=adapter.amc_slug, path=str(excel_path), err=str(e),
            )
            cache[excel_path] = None
    return cache[excel_path]


def run_for_amc(
    amc_slug: str,
    ym: str | None = None,
    match_threshold: int = DEFAULT_THRESHOLD,
    force: bool = False,
) -> dict:
    """Run one AMC's holdings adapter end-to-end.

    ``force=True`` (a ``--full`` pipeline run) carries the unified force
    semantics (B9): re-download AND re-parse AND re-write. It deletes this
    data month's cached per-scheme Excels before ``fetch_excel`` (which
    otherwise returns a cached file by mere existence, making a corrected/
    re-published artifact unreachable), and overrides the
    partition-shrinkage guard (B13): a fresh parse yielding fewer distinct
    schemes than the DB already holds for (source_amc, month) is normally
    refused so a partial re-run can't silently delete previously-good
    schemes; force replaces the partition anyway, loudly. Deletion is
    scoped to ``ym`` only — historical months are never re-fetched.

    Default runs are incremental (C5): when the fetched artifact set and the
    match universe are byte-identical to the last successful ingest and the
    DB already holds the rows, the parse loop AND the per-month
    DELETE+reinsert are skipped (``skipped=True`` in the result) — discovery
    and the cached fetches still run, so late-published schemes are detected.
    ``force`` bypasses the skip (unified --full semantics).
    """
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

    # canonical_category + master name per scheme_code, for the per-portfolio
    # sanity gate (B3). iter_rows(named=True).get() tolerates minimal test
    # frames; the production q.scheme_master() always carries both columns.
    cat_by_code: dict[str, str | None] = {}
    master_name_by_code: dict[str, str | None] = {}
    for r in sm.iter_rows(named=True):
        cat_by_code[r["scheme_code"]] = r.get("canonical_category")
        master_name_by_code[r["scheme_code"]] = r.get("scheme_name")

    matched_rows: list[dict] = []
    unmatched: list[str] = []
    n_excels_parsed = 0
    n_records_total = 0
    gate_skipped: dict[str, list[str]] = {
        "weight_sum_breach": [], "too_few_holdings": [],
    }
    statement_months_cache: dict[Path, set[str] | None] = {}

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

    # Fetch loop (C5): download every winner's Excel BEFORE parsing — fetch is
    # disk-cached unconditionally (holdings/_generic.py), so on an unchanged
    # month this is pure cache reads. Downloading only matched schemes saves
    # bandwidth on ETFs/FoFs that aren't ranked.
    fetched: list[tuple[str, Path]] = []
    for scheme_name_printed, url in urls.items():
        if scheme_name_printed not in winners:
            continue  # unmatched, or a lower-scoring duplicate for its code
        excel_filename = f"{scheme_name_printed}.xlsx"
        if force:
            # B9: --full re-downloads this data month's artifacts — evict the
            # canonical cached Excel so fetch_excel's exists() check can't
            # serve stale bytes. unlink is a no-op for adapters whose bespoke
            # fetch_excel caches elsewhere.
            paths.holdings_excel_raw(amc_slug, ym, excel_filename).unlink(
                missing_ok=True
            )
        try:
            excel_path = adapter.fetch_excel(url, excel_filename, ym)
        except Exception as e:  # noqa: BLE001
            log.error(
                "holdings.fetch_failed",
                amc=amc_slug, scheme=scheme_name_printed, url=url, err=str(e),
            )
            continue
        fetched.append((scheme_name_printed, excel_path))

    # Incremental parse-skip (C5, behavioral parity with the managers path):
    # if the artifact SET (every winner's printed name + Excel bytes) AND the
    # match universe are identical to the last successful ingest, and the DB
    # already holds those rows, re-parsing + the DELETE+reinsert of this
    # (source_amc, month) partition is a provable no-op — skip both. The win
    # is the churn (the ~96k-row DELETE window), not the parse CPU. `force`
    # (--full) always re-parses; discovery HTTP above is NEVER skipped, so
    # late-published schemes within a month still surface (their entry changes
    # the artifact-set fingerprint and triggers a full re-parse).
    universe_fp = _parse_cache.fingerprint(
        f"{name}={info['scheme_code']}" for name, info in candidates.items()
    )
    artifact_set_fp = _parse_cache.fingerprint(
        f"{printed}:{_parse_cache.sha256_file(path)}" for printed, path in fetched
    )
    marker_dir = paths.holdings_excel_raw(amc_slug, ym, "x").parent
    if (
        not force
        and fetched
        and _parse_cache.should_skip_parse_dir(
            marker_dir, artifact_set_fp, universe_fp,
            db_has_rows=q.has_holdings_rows(amc_slug, as_of_month),
        )
    ):
        log.info("holdings.run.skip_unchanged", amc=amc_slug, ym=ym)
        return {
            "amc_slug": amc_slug,
            "ym": ym,
            "n_schemes_discovered": len(urls),
            "n_schemes_parsed": 0,
            "n_records": 0,
            "rows_written": 0,
            "skipped": True,
            "unmatched_schemes": unmatched,
            "weight_sum_skipped_schemes": [],
            "too_few_holdings_skipped_schemes": [],
        }

    for scheme_name_printed, excel_path in fetched:
        scheme_code = name_to_code[scheme_name_printed]
        n_excels_parsed += 1
        try:
            # Central statement-date screen (B2): EVERY adapter — including
            # the bespoke fixed-offset parsers that never call
            # parse_sebi_excel — is checked against the artifact's printed
            # 'AS ON' month before its rows can be parsed in. Validate-when-
            # present: an artifact with no parseable banner (or an unreadable
            # container) proceeds — most AMCs print it, and absence alone
            # must not nuke coverage.
            found_months = _statement_months_for(
                adapter, excel_path, statement_months_cache,
            )
            if found_months and ym not in found_months:
                raise StatementDateMismatchError(excel_path, ym, found_months)
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
        if not records:
            continue  # zero-record parse: not a weight problem (B1 evicts)
        # Per-portfolio sanity gate (B3): a rankable scheme whose ISIN-bearing
        # weights are mis-scaled, or an active equity fund carrying a
        # contamination-shaped sliver of rows, is skipped entirely — its prior
        # DB partition rows stay (the DELETE below is per-AMC and only runs
        # when matched_rows survive; the shrinkage guard then refuses to lose
        # the skipped scheme without --full).
        verdict = check_portfolio_sanity(
            scheme_code=scheme_code,
            printed_name=scheme_name_printed,
            master_name=master_name_by_code.get(scheme_code),
            category=cat_by_code.get(scheme_code),
            total_weight=sum(float(r.weight_pct) for r in records),
            n_rows=len(records),
            amc_slug=amc_slug,
        )
        if verdict != "ok":
            gate_skipped[verdict].append(scheme_name_printed)
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
    matched_rows = _dedupe_by_keys(
        matched_rows, ("scheme_code", "security_name", "as_of_month"),
        amc=amc_slug,
    )

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
        incoming_schemes = {r["scheme_code"] for r in matched_rows}
        with connect() as conn:
            # Partition-shrinkage guard (B13): a scheme that parsed last run
            # but failed this run would otherwise have its prior-good rows
            # silently deleted by the partition DELETE. Refuse (IngestError →
            # transaction rolls back, DB untouched, run_all records the
            # per-AMC failure); ``force`` (--full) replaces anyway, loudly.
            row = conn.execute(
                "SELECT COUNT(DISTINCT scheme_code) FROM holdings_monthly "
                "WHERE as_of_month = %s AND source_amc = %s",
                (as_of_month, amc_slug),
            ).fetchone()
            existing_schemes = int(row[0]) if row else 0
            if len(incoming_schemes) < existing_schemes:
                if not force:
                    raise IngestError(
                        f"{amc_slug}: refusing partition shrinkage for "
                        f"{ym} — incoming {len(incoming_schemes)} distinct "
                        f"schemes < {existing_schemes} already in DB. A "
                        f"scheme that ingested before failed this run; "
                        f"re-run with --full to replace anyway."
                    )
                log.warning(
                    "holdings.partition_shrinkage_forced",
                    amc=amc_slug, ym=ym,
                    incoming=len(incoming_schemes), existing=existing_schemes,
                )
            conn.execute(
                "DELETE FROM holdings_monthly "
                "WHERE as_of_month = %s AND source_amc = %s",
                (as_of_month, amc_slug),
            )
            n_written = w.upsert_holdings(pl.DataFrame(matched_rows), conn=conn)
    else:
        n_written = 0
    # Record this successful ingest so a byte-identical re-run (same artifact
    # set + same match universe) can skip parse + DELETE/reinsert next time
    # (C5). Written only AFTER the rows are committed — a crash before here
    # leaves no marker, so the next run re-parses (managers-path parity).
    if n_written:
        _parse_cache.write_dir_marker(
            marker_dir, artifact_set_fp, universe_fp,
            {"ym": ym, "n_rows": n_written, "n_schemes": len(incoming_schemes)},
        )
    log.info(
        "holdings.run.done",
        amc=amc_slug, ym=ym,
        n_schemes_discovered=len(urls),
        n_schemes_parsed=n_excels_parsed,
        n_records=n_records_total,
        rows_written=n_written,
        unmatched_unique_schemes=len(unmatched),
        n_weight_sum_skipped=len(gate_skipped["weight_sum_breach"]),
        n_too_few_holdings_skipped=len(gate_skipped["too_few_holdings"]),
    )
    return {
        "amc_slug": amc_slug,
        "ym": ym,
        "n_schemes_discovered": len(urls),
        "n_schemes_parsed": n_excels_parsed,
        "n_records": n_records_total,
        "rows_written": n_written,
        "unmatched_schemes": unmatched,
        "weight_sum_skipped_schemes": gate_skipped["weight_sum_breach"],
        "too_few_holdings_skipped_schemes": gate_skipped["too_few_holdings"],
    }


def run_all(ym: str | None = None, force: bool = False) -> dict[str, dict]:
    """Run every registered holdings adapter with per-AMC fault isolation.

    ``force`` (wired to the pipeline's ``--full`` flag) is forwarded to each
    AMC's run: re-download this data month's Excels, re-parse, and re-write
    overriding the partition-shrinkage guard (see ``run_for_amc``). Cost of
    force: ~640 holdings Excels re-fetched (~minutes serially; hours if an
    AMC's month probe is pathological — kotak's negative cache, B11, bounds
    that).

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
    return run_all_isolated(
        "holdings",
        slugs,
        lambda slug: run_for_amc(slug, ym=ym, force=force),
        ym=ym,
        error_zero_fields=("rows_written",),
    )
