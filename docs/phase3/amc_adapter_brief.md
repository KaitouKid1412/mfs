# AMC factsheet adapter — build brief

Anyone (sub-agent or human) building a new AMC adapter for Stage 2 PTR/AUM
coverage should read this end-to-end before touching code. It is the single
source of truth for the contract; deviations are bugs.

## What an adapter is

One Python module per AMC at `src/mfs/ingest/managers/<slug>.py`. It is a
subclass of `mfs.ingest.managers._base.ManagerAdapter` decorated with
`@register_adapter`. It pulls the AMC's monthly combined factsheet PDF
and yields three streams:

- `parse_ptr(pdf, ym)` → `ParsedPtrRecord(scheme_name_printed, ptr, source_amc)`
- `parse_aum(pdf, ym)` → `ParsedAumRecord(scheme_name_printed, aum_crore, source_amc)`
- `parse_holdings(pdf, ym)` → `ParsedHoldingRecord(scheme_name_printed, security_name, weight_pct, isin?, instrument_type, source_amc)`

The orchestrator (`mfs.ingest.managers._run.run_for_amc`) handles fuzzy
matching of printed scheme names to `scheme_master` and DB upsert. Adapters
only need to extract clean (name, value) tuples — do not match
`scheme_code` yourself.

## Reference adapters

Two existing adapters cover the two main layout families. Read both before
designing your regex:

- `src/mfs/ingest/managers/hdfc.py` — text-extract first, then regex.
  HDFC's PTR is `Equity Turnover 9.14%` (percent → divide by 100 to get
  fraction); AUM is `As on April 30, 2026 ₹100,479.23Cr.` Holdings use
  pdfplumber word positions in a two-column layout.

- `src/mfs/ingest/managers/sbi.py` — word-position extraction in a
  per-page layout. PTR is `Equity Turnover : 0.31` (already a fraction);
  AUM is `AAUM for the Month ... ₹ 52,854.99 Crores`. Scheme name comes
  from the page footer, not the header.

Use whichever pattern fits the target AMC's PDF.

## Slug → scheme_master amc_code

Adapter `amc_slug` does NOT have to match `scheme_master.amc_code`. Short
slugs are preferred. When they differ, add an entry to the alias map in
`src/mfs/ingest/managers/_scheme_match.py` under
`_ADAPTER_SLUG_TO_SCHEME_MASTER_AMC_CODE`. Current canonical mapping:

| adapter `amc_slug` | `scheme_master.amc_code` | alias needed? |
| --- | --- | --- |
| kotak | kotak_mahindra | YES |
| uti | uti | no |
| mirae | mirae_asset | already there |
| franklin | franklin_templeton | YES |
| axis | axis | no |
| bandhan | bandhan | no |
| dsp | dsp | no |
| tata | tata | no |
| edelweiss | edelweiss | no |
| hsbc | hsbc | no |
| baroda_bnp | baroda_bnp_paribas | YES |
| invesco | invesco | no |
| motilal_oswal | motilal_oswal | no |
| sundaram | sundaram | no |
| groww | groww | no |

If the adapter you're building needs a YES, ADD the alias to the dict in
`_scheme_match.py` as part of the same change.

## Data month

Calibrate against **2026-04** (data month). The default
`_default_data_month()` in `_run.py` produces this. Published filenames
on AMC sites typically reflect the PUBLISH month, which is one month later
(2026-05). HDFC's URL builder shows the pattern: `_publish_ym()` shifts
data_ym forward by one.

## Step-by-step

1. **Find the PDF**: read `data/raw/factsheets/<slug>/2026-04.pdf` if it
   already exists. If not, discover the canonical URL via WebFetch /
   WebSearch (typical pattern: search "site:<amc-domain> factsheet april 2026 pdf")
   and download. Save to that exact path. URLs change YoY but the slug-stable
   `build_url(ym)` must produce next month's URL too — encode the pattern,
   don't hardcode one date.

2. **Inspect the PDF**: use a one-off script to dump page text and find
   how PTR and AUM are printed. Use `pdfplumber`:
   ```python
   import pdfplumber
   with pdfplumber.open("data/raw/factsheets/<slug>/2026-04.pdf") as pdf:
       for i, page in enumerate(pdf.pages):
           text = page.extract_text() or ""
           if "Turnover" in text or "AUM" in text:
               print(f"--- page {i+1} ---")
               print(text[:2000])
   ```
   Look for: how PTR is labeled (Equity Turnover / Portfolio Turnover / PTR);
   percent vs fraction; how AUM is labeled (Month End AUM / AAUM); units
   (`Crore` / `Cr.` / lakhs). Find an unambiguous scheme-name anchor for
   each page (header / footer / banner).

3. **Write the adapter**. Skeleton:
   ```python
   from __future__ import annotations
   import re
   from pathlib import Path
   from typing import Iterable
   import pdfplumber
   from mfs.ingest.managers._base import ManagerAdapter
   from mfs.ingest.managers._registry import register_adapter
   from mfs.schemas import ParsedAumRecord, ParsedHoldingRecord, ParsedPtrRecord

   @register_adapter
   class FooAdapter(ManagerAdapter):
       amc_slug = "foo"
       source_label = "Foo Mutual Fund"

       def build_url(self, ym: str) -> str:
           y, m = map(int, ym.split("-"))
           ... # encode the AMC's URL convention

       def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
           ...

       def parse_aum(self, pdf_path: Path, ym: str) -> Iterable[ParsedAumRecord]:
           ...

       def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
           ...
   ```

4. **PTR normalization**: storage is fraction (1.27 = 127%). If the AMC
   prints percent, divide by 100 in the parser. If they print fraction
   (0.31), pass through. Be explicit about which in a one-line comment
   above the regex — it is the most common error.

5. **AUM normalization**: storage is INR Crore. If the AMC prints in
   lakhs, divide by 100. Prefer **Month-End AUM** over AAUM (average) when
   both are present, since the metric set treats `aum_crore` as a point-
   in-time value.

6. **Register the adapter**: add `from mfs.ingest.managers import <slug>
   # noqa: F401` to `src/mfs/ingest/managers/__init__.py`.

7. **Write the test**: `tests/test_managers_<slug>.py`. Calibrate
   against the same 2026-04 PDF. Minimum coverage:
   - `parse_ptr` yields at least one record for a known scheme.
   - `parse_aum` yields at least one record with `aum_crore > 0` for a known scheme.
   - `build_url(ym)` produces the actual URL you downloaded from (string
     equality OK; don't HTTP-test).
   The test should NOT hit the network — read the cached PDF.

8. **Run end-to-end**:
   ```bash
   uv run mfs ingest managers --amc <slug> --ym 2026-04
   ```
   Confirm log shows ptr.parsed n_records>0 and aum.parsed n_records>0,
   and rows_written_ptr / rows_written_aum > 0.

## Fail-fast invariants (from CLAUDE memory)

- **No half-data**: an adapter that yields PTR with NaN, AUM=0, or a
  scheme-name that lost its identity (e.g. "Fund of Fund Schemes")
  should drop the row entirely. Half data is worse than no data.
- **No manual entry**: if the PDF layout defeats your parser, REPORT
  back rather than handwriting values. Mark the AMC as DEFERRED.
- **Halt on scrape failure**: if the PDF URL 404s, raise
  `mfs.errors.IngestError` with a clear message. Don't silently skip.

## Output expected back from a sub-agent

- The adapter file
- Updated `__init__.py` (and `_scheme_match.py` alias if needed)
- The test file
- A short report:
  - Cached PDF path used
  - URL pattern used (and whether it was guessed vs WebFetched)
  - PTR regex / approach in one line
  - AUM regex / approach in one line
  - Number of PTR rows extracted from 2026-04 (printed at the end of the run)
  - Number of AUM rows extracted from 2026-04
  - Anything weird about the layout future-you should know
