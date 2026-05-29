# AUM extraction strip — adapter cleanup brief

> **Status: COMPLETED 2026-05-29.** `parse_aum` / `ParsedAumRecord` and the
> factsheet AUM write path are removed from `src/mfs/ingest/managers/`.
> `scheme_aum_monthly` rows where `source_amc <> 'amfi_aaum'` were
> deleted (1,252 rows) and the table now carries a CHECK constraint
> `source_amc = 'amfi_aaum'`. `latest_scheme_aum()` ORDER BY simplified
> to `as_of_month DESC`. Ranked-fund AUM coverage unchanged at 666/682.
> See `src/mfs/ingest/amfi_aum.py` for the sole AUM ingester. This brief
> is retained as historical record of the cleanup.

## Why

AMFI publishes per-scheme quarterly AAUM at the JSON endpoint
`/api/average-aum-schemewise`. A new ingester at
`src/mfs/ingest/amfi_aum.py` covers ~98% of the ranked universe in a
single GET, replaces ~1,535 LOC of factsheet AUM extraction, and is
authoritative SEBI-reported data. PTR is *not* in AMFI — factsheets
remain the only PTR source — so adapters stay, but with their AUM
extraction removed.

`latest_scheme_aum()` in `mfs.db.queries` already prefers
`source_amc='amfi_aaum'` over adapter rows, so removing adapter AUM
code is a clean cut with no downstream impact.

## What each adapter has that needs removing

A typical adapter has these AUM-specific elements:

1. **Method override**: `def parse_aum(self, pdf_path, ym) -> Iterable[ParsedAumRecord]: ...`
   - The base class default returns `()`, so removing the override is sufficient.

2. **Regex / pattern constants**: e.g. `_AUM_RE`, `_MONTH_END_RE`, `_AAUM_RE`, `_SBI_AUM_RE`.
   - Anything declared at module level whose only consumer is `parse_aum`.

3. **Helper functions**: e.g. `_find_aum_via_words`, `_extract_aum`, `_parse_aum_value`.
   - Only delete if AUM is the sole caller. Word-position helpers shared with
     PTR (e.g. a generic `_find_value_near_anchor`) must stay.

4. **`ParsedAumRecord` import**: drop the import from `mfs.schemas` if
   nothing else in the file references it.

5. **Test cases**: in `tests/test_managers_<slug>.py`, delete tests
   whose name or body contains `parse_aum`, `AUM`, `aum_crore`,
   `_AUM_RE`, etc.

## What NOT to touch

- `parse_ptr` and `parse_holdings` methods, their regex/helpers, and tests.
- The `build_url`, `fetch`, scheme-name detection, page-gating logic.
- The `amc_slug`, `source_label` class attributes.
- Side-effect imports in `src/mfs/ingest/managers/__init__.py`.
- The base class (`_base.py`) — `parse_aum` still exists as a no-op default.
- The schema (`ParsedAumRecord` in `schemas.py`) — kept for any future use
  or for the `_run.py` dispatcher (which can still call `parse_aum` and
  receive zero records).

## How to verify

1. The file still imports cleanly: `python -c "from mfs.ingest.managers import <slug>"`.
2. The adapter test file passes its remaining tests: `uv run pytest tests/test_managers_<slug>.py -q`.
3. An end-to-end run still ingests PTR but writes zero AUM rows:
   ```bash
   uv run mfs ingest managers --amc <slug> --ym 2026-04 \
     --pdf-path data/raw/factsheets/<slug>/2026-04.pdf
   ```
   Expect `rows_written_ptr > 0` and `rows_written_aum = 0`.

## Style guidance

- Don't leave commented-out AUM blocks. Delete cleanly.
- Don't add migration comments ("removed because AMFI replaced this"). The
  commit message + this brief carry that context.
- If a docstring at the top of the file mentions extracting AUM, edit it
  to match the new reality (PTR + holdings only) — but keep it tight.
