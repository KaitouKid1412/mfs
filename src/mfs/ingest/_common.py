"""Shared helpers for the per-AMC ingest adapters and orchestrators (F-4).

The ~40 manager (factsheet) adapters were built by copy-pasting a small
skeleton: a month-name table, a publish-month offset helper, a pdfminer
log-silencing line, and a ~25-line ``parse_ptr`` page loop. This module is
the single home for those pieces. New adapters should import from here;
existing adapters are migrated opportunistically (hdfc, canara_robeco and
iti are the proof migrations — they carry the committed CI fixture pins in
``tests/test_managers_fixture_extracts.py``).

SimplePtrSpec (FUTURE — docstring spec only, no implementation yet)
-------------------------------------------------------------------
The remaining ~16 text-regex-only manager adapters share so much structure
that they should eventually collapse into a declarative spec consumed by a
single generic adapter::

    SimplePtrSpec = {
        "url_template":   str,         # format()-able with publish/data month
                                       #   fields, e.g. "https://.../{month_name}-{data_year}.pdf"
        "scheme_name_re": re.Pattern,  # printed-name anchor on a scheme page
                                       #   (group 1 = name); page skipped if no match
        "ptr_re":         re.Pattern,  # PTR value regex (group 1 = number)
        "ptr_unit":       str,         # "fraction" (pass-through) | "percent" (/100)
        "publish_offset": int,         # months between data month and publish month
    }

Adapters whose URL needs live discovery (listing-page scrape, encrypted API)
or whose PTR needs word-position fallbacks keep bespoke code and call
``parse_ptr_pages`` directly.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import pdfplumber

from mfs.config import get_settings
from mfs.errors import IngestError
from mfs.schemas import ParsedPtrRecord
from mfs.utils.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Month tables (previously redefined in ~63 ingest files)
# ---------------------------------------------------------------------------

MONTH_NAMES: tuple[str, ...] = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# Lower-cased full names AND 3-letter abbreviations → month number, for
# parsing month names printed in factsheet banners / listing anchors.
MONTH_NUMBER: dict[str, int] = {
    **{name.lower(): i for i, name in enumerate(MONTH_NAMES, start=1)},
    **{name[:3].lower(): i for i, name in enumerate(MONTH_NAMES, start=1)},
}


def month_name(ym: str) -> str:
    """Data month ``'YYYY-MM'`` → full month name (e.g. ``'April'``)."""
    return MONTH_NAMES[int(ym.split("-")[1]) - 1]


def publish_ym(data_ym: str, offset_months: int = 1) -> str:
    """Shift a ``'YYYY-MM'`` data month by ``offset_months`` (year-safe).

    Most AMCs upload the data-month factsheet one calendar month later, so
    the default offset is +1 (April data → May publish path). Negative
    offsets are allowed. Replaces the private ``_publish_ym`` copies that
    were duplicated across the manager/holdings adapters.
    """
    y, m = map(int, data_ym.split("-"))
    total = y * 12 + (m - 1) + offset_months
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _default_data_month(today: date | None = None) -> str:
    """Return YYYY-MM for the most recent complete month (AMFI publishes by the
    10th of the next month, so 'previous calendar month' is a safe default).

    Single shared implementation for both ingest orchestrators (F-5) — the
    managers and holdings copies were identical.
    """
    d = today or date.today()
    if d.month == 1:
        return f"{d.year - 1:04d}-12"
    return f"{d.year:04d}-{d.month - 1:02d}"


# ---------------------------------------------------------------------------
# pdfminer log silencing (previously one inline line in 41 manager files)
# ---------------------------------------------------------------------------

def silence_pdfminer() -> None:
    """Quiet pdfminer's noisy per-object WARNINGs during pdfplumber parses."""
    logging.getLogger("pdfminer").setLevel(logging.ERROR)


# The pdfminer logger is process-global state, so silencing it at import time
# covers every pdfplumber use in any adapter that (transitively) imports this
# module — including parse paths that don't go through parse_ptr_pages
# (e.g. hdfc.parse_holdings).
silence_pdfminer()


# ---------------------------------------------------------------------------
# Canonical factsheet PTR page loop
# ---------------------------------------------------------------------------

def parse_ptr_pages(
    pdf_path: Path,
    *,
    scheme_name_fn: Callable[[str], str | None],
    ptr_extract_fn: Callable[[str], float | None],
    amc_slug: str,
    skip_page_re: re.Pattern[str] | None = None,
    page_fallback_fn: Callable[[Any, str], float | None] | None = None,
) -> Iterator[ParsedPtrRecord]:
    """Yield ``ParsedPtrRecord`` per scheme page of a factsheet PDF.

    The canonical loop that was copy-pasted across ~40 manager adapters:
    open with pdfplumber → per page ``extract_text() or ''`` →
    ``scheme_name_fn(text)`` (page skipped when ``None`` — non-scheme page)
    → optional ``skip_page_re`` sentinel skip (e.g. a "PTR not provided,
    scheme < 1 year old" footnote) → ``ptr_extract_fn(text)`` (skipped when
    ``None``) → NaN / non-positive guard → yield.

    UNIT CONVENTION (this is the central place that kills the recurring
    percent-vs-fraction bug): ``ptr_extract_fn`` MUST return the PTR as a
    FRACTION (0.17 == 17% turnover). Adapters whose factsheet prints a
    percent ("Equity Turnover 9.14%") must divide by 100 INSIDE their
    extract fn; "times"-convention factsheets pass through unchanged.

    ``page_fallback_fn(page, text)`` is the documented escape hatch for
    adapters whose ``extract_text`` output column-bleeds the PTR line
    (hdfc's word-position fallback): it is consulted only when
    ``ptr_extract_fn`` returns ``None``, and must also return a fraction.
    """
    silence_pdfminer()
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            scheme = scheme_name_fn(text)
            if not scheme:
                continue
            if skip_page_re is not None and skip_page_re.search(text):
                continue
            ptr_value = ptr_extract_fn(text)
            if ptr_value is None and page_fallback_fn is not None:
                ptr_value = page_fallback_fn(page, text)
            if ptr_value is None:
                continue
            # Fail-fast: drop NaN / non-positive values.
            if ptr_value != ptr_value or ptr_value <= 0:
                continue
            yield ParsedPtrRecord(
                scheme_name_printed=scheme,
                ptr=ptr_value,
                source_amc=amc_slug,
            )


# ---------------------------------------------------------------------------
# Shared orchestrator core (F-5): dedupe + per-AMC fault isolation
# ---------------------------------------------------------------------------

def _dedupe_by_keys(
    rows: list[dict], key_cols: tuple[str, ...], **log_ctx: Any
) -> list[dict]:
    """Drop later rows whose key tuple already appeared earlier in `rows`.

    Postgres' ``ON CONFLICT DO UPDATE`` cannot affect the same destination
    row twice in a single INSERT, so any in-batch PK collision raises
    ``CardinalityViolation``. Adapters sometimes produce these when two
    slightly different printed scheme names fuzzy-match to the same
    ``scheme_code`` for the same security/month — we keep the first
    occurrence rather than silently merging values. ``log_ctx`` (e.g.
    ``amc=<slug>``) is attached to the structlog event.
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
        log.info(
            "ingest.dedupe", n_dropped=n_drops, keys=list(key_cols), **log_ctx
        )
    return out


def run_all_isolated(
    family: str,
    slugs: list[str],
    run_one: Callable[[str], dict],
    *,
    ym: str | None = None,
    error_zero_fields: tuple[str, ...] = (),
    adapter_kind: str | None = None,
) -> dict[str, dict]:
    """Run ``run_one(slug)`` for every AMC with per-AMC fault isolation.

    The shared core of ``managers.run_all`` / ``holdings.run_all`` (F-5):
    holdings/PTR are advisory signals, so one AMC's failure (a 404, a parse
    error, a layout change) must NOT crash the pipeline — it is caught,
    recorded as that AMC's error row, and the run continues. This also
    contains an uncaught ``httpx.HTTPStatusError`` (e.g. a 4xx, which is
    neither a TransientHttpError nor a PipelineError, so it would otherwise
    escape the pipeline's ``_stage`` catch and abort the whole process).

    Fail-fast is preserved for SYSTEMIC failure: if *every* adapter fails,
    that is a network/config problem rather than a per-AMC hiccup, so we
    raise IngestError instead of proceeding silently (the systemic error is
    raised only after the pool fully drains, so every AMC's outcome is
    recorded). The Phase-2 coverage gate surfaces the per-AMC gaps.

    Concurrency (C2): AMCs run on a ThreadPool of ``settings.ingest_workers``
    (env ``MFS_INGEST_WORKERS``; 1 = serial debugging mode). This is safe
    because each ``run_for_amc`` opens fresh psycopg connections, its
    DELETE+upsert transaction is keyed by (source_amc, as_of_month) —
    disjoint row sets across threads — and each in-flight task targets one
    distinct AMC host with downloads serial inside the task, so per-host
    concurrency stays 1. The B13 shrinkage guard and B14b PTR tripwire run
    inside ``run_for_amc``, i.e. inside this isolation boundary. Result
    aggregation (the results dict, the ``failed`` list, and the per-AMC
    ``amc_failed`` events) is deterministic: outcomes are emitted in ``slugs``
    order regardless of completion order.

    Args:
      family: log-event prefix — ``'managers'`` or ``'holdings'`` — chosen to
        preserve the exact pre-extraction event names
        (``managers.amc_failed`` / ``holdings.amc_failed`` and
        ``<family>.run_all.partial``).
      slugs: registered AMC slugs (deterministic, sorted by the registry).
      run_one: callable invoking the family's ``run_for_amc`` for one slug.
      ym: data month recorded on error rows (mirrors the success rows).
      error_zero_fields: zeroed row-count keys for the family's error shape
        (managers: ``rows_written_holdings``/``rows_written_ptr``;
        holdings: ``rows_written``).
      adapter_kind: human label in the systemic-failure message
        (``'factsheet'`` / ``'holdings'``); defaults to ``family``.
    """
    kind = adapter_kind or family
    if not slugs:
        return {}
    n_workers = min(max(1, int(get_settings().ingest_workers)), len(slugs))

    # Phase 1: collect every AMC's outcome (result dict or exception).
    outcomes: dict[str, dict | Exception] = {}
    if n_workers == 1:
        for slug in slugs:
            try:
                outcomes[slug] = run_one(slug)
            except Exception as e:  # noqa: BLE001 — isolate one AMC; never crash the stage
                outcomes[slug] = e
    else:
        with ThreadPoolExecutor(
            max_workers=n_workers, thread_name_prefix=f"{family}-ingest"
        ) as pool:
            futures = {slug: pool.submit(run_one, slug) for slug in slugs}
            for slug, fut in futures.items():
                try:
                    outcomes[slug] = fut.result()
                except Exception as e:  # noqa: BLE001 — isolate one AMC; never crash the stage
                    outcomes[slug] = e

    # Phase 2: deterministic aggregation in slugs order.
    results: dict[str, dict] = {}
    failed: list[str] = []
    for slug in slugs:
        out = outcomes[slug]
        if isinstance(out, Exception):
            failed.append(slug)
            row = {
                "amc_slug": slug, "ym": ym,
                "error": str(out), "error_type": type(out).__name__,
            }
            row.update({f: 0 for f in error_zero_fields})
            results[slug] = row
            log.error(f"{family}.amc_failed", amc=slug,
                      err=str(out), err_type=type(out).__name__)
        else:
            results[slug] = out
    if failed:
        log.warning(f"{family}.run_all.partial",
                    n_failed=len(failed), n_total=len(slugs), failed=failed)
    if slugs and len(failed) == len(slugs):
        raise IngestError(
            f"All {len(slugs)} {kind} adapters failed — systemic network/config "
            f"issue, not per-AMC. Refusing to proceed silently. "
            f"First error: {results[slugs[0]].get('error')}"
        )
    return results
