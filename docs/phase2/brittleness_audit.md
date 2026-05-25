# Phase 2 Brittleness Audit

Captured **2026-05-22**, before Phase 2.0 started. Revisit after Phase 2.0 ships
to validate assumptions and decide whether any Tier-1 risks need re-scoping
before Phase 2.1.

## How to use this doc

- Each item lists the failure mode, expected cadence of breakage, and what's
  planned to absorb it.
- After Phase 2.0: re-read this file. If any assumption (e.g. "AMFI Excel
  layouts mostly stable") has already been falsified in practice, revise the
  plan for 2.1/2.2/2.3 before writing scraper code.
- Tier numbers are maintenance-burden tiers (1 = highest), not priority order.

---

## TIER 1 — Highest burden, expect frequent breakage

### 1. Per-AMC factsheet PDF scrapers (manager tenure + PTR + scheme-level AUM)

- **Surface**: ~45 AMCs x monthly PDFs (50-200 pages each). 90+ tables to extract.
- **Why brittle**:
  - Every AMC has a different PDF layout; some change 2-3x per year.
  - Several AMCs publish **image-only PDFs** -> needs OCR (Tesseract), unreliable on Indian names and small fonts.
  - Manager name parsing: honorifics ("Mr./Ms./Dr."), middle initials, joint-managed funds (2-3 names with separate dates), "Lead Manager" not always labeled.
  - "Managing since" dates use >=4 formats (`"01-Jan-2020"`, `"January 2020"`, `"1.1.2020"`, `"5 years"`).
  - Manager changes often disclosed only in footnotes or addenda.
  - Co-management transitions (30/60 day handoff) are ambiguous.
  - PTR sometimes published as a single number, sometimes as Purchases/Sales/Avg AUM rows.
- **Failure mode**: Silent (wrong field, wrong scheme, wrong manager).
- **Cadence**: 1-3 AMC parsers break per month.
- **Risk to ranking**: Bad manager tenure / PTR pollutes soft-penalty term -> moves shortlist.
- **Mitigation planned**:
  - Per-AMC test fixtures with last-month PDF snapshot.
  - Schema validation on extracted rows (date ranges, value ranges, name regex).
  - `factsheet_extraction_quality` flag per scheme -> downgrade contribution to zero if confidence is low (don't penalize, just don't reward).

### 2. AMFI Monthly Portfolio Disclosure Excel parsers (~45 AMC adapters)

- **Surface**: One per AMC, each with sheet-name / column-header quirks.
- **Why brittle**:
  - Header drift: `"% to Nav"` / `"% to NAV"` / `"% of Net Assets"` / `"Weightage (%)"`.
  - Layout drift: workbook-per-AMC vs file-per-scheme vs single consolidated PDF.
  - **ISIN missing** in 5-15% of rows (debt, REITs, futures, foreign equity) -> fuzzy security-name matching.
  - Equity derivatives, cash equivalents, REIT/InvIT line items pollute Active Share denominator.
  - "Market Value" unit drift: Lakhs vs Crores vs Rupees, sometimes mixed in one sheet.
  - AMC mergers (Indiabulls -> Groww, Principal -> Sundaram) break file naming.
- **Failure mode**: Mostly loud (parse errors), occasionally silent (wrong units).
- **Cadence**: 1-2 AMC parsers break per month; long tail (small AMCs) is the worst.
- **Mitigation planned**:
  - Per-AMC adapter classes with a registry.
  - Golden-file tests per AMC.
  - Unit-detection heuristic (median row value x 10^5 vs 10^7 vs 10^9).

### 3. AMFI consolidated stress-test PDF scraper

- **Surface**: One PDF, quarterly, since March 2024 — short track record (~7 releases).
- **Why brittle**:
  - Format already changed twice in 2024-2025 (column reorder, footnote moves).
  - PDF table extraction fragile on multi-page tables with merged headers.
  - SEBI may change the disclosure mandate.
  - Scheme-name fuzzy match to `scheme_code` required — names don't match `amfi_category` strings exactly.
- **Failure mode**: Loud (table extraction errors) or silent (wrong column).
- **Cadence**: Quarterly review minimum. Expect breakage every other release for the first year.
- **Mitigation planned**:
  - Snapshot-test the last published PDF.
  - Integration test compares extracted values to a fixture file in repo.

---

## TIER 2 — Medium burden, periodic breakage

### 4. NSE Index Constituents endpoint

- **Why brittle**:
  - NSE anti-bot tightened 2023-2025. Constituent CSV URLs sometimes 403 silently.
  - Requires browser-like headers, session cookies, sometimes a referer chain. May need Playwright for some sector indices.
  - URL pattern has changed ~3x since 2020 (`nseindia.com` -> `archives.nseindia.com` -> `niftyindices.com`).
  - Aggressive polling -> 24h IP ban.
  - Synthetic/hybrid TRIs in `configs/benchmarks.csv` have **no constituent file** -> manual CSV.
  - Index reconstitutions semi-annual; "previous" constituent file disappears -> can't backfill old months.
- **Failure mode**: Loud (403/429), occasionally silent (stale 200 OK).
- **Cadence**: 1-2 break-fixes per year on URL/auth; ongoing rate-limit tuning.
- **Mitigation planned**:
  - Per-ticker manual-CSV fallback: `data/raw/index_constituents/manual/{ticker}/{ym}.csv`.
  - `niftyindices.com` as secondary source.
  - Snapshot last-good response; reuse with freshness flag if fetch fails.

### 5. AMC anti-bot measures (factsheet PDF + portfolio Excel downloads)

- **Why brittle**:
  - HDFC, ICICI, Nippon use Cloudflare; aggressive scraping -> IP ban.
  - Quant, Edelweiss intermittently require form POST + captcha.
  - Several AMCs gate factsheets behind email registration.
  - PDF URLs sometimes signed with short-lived tokens — direct-link scraping breaks weekly.
- **Failure mode**: Loud (403/captcha) or silent (HTML error page with 200).
- **Cadence**: 2-5 fixes per year per AMC.
- **Mitigation planned**:
  - User-Agent + Accept header rotation.
  - 2-4s jitter between requests.
  - Playwright fallback for worst offenders.
  - Honor robots.txt and AMC ToS; stop if any AMC asks us to.

### 6. NSE Bhavcopy daily fetch (stock ADV)

- **Why brittle (but lower than the rest)**:
  - Endpoint changed late 2024 to "BhavCopyEQ" CSV under new CDN path. Stable since.
  - Most recent date can lag 30-90 min after close on volatile days.
  - **Symbol -> ISIN mapping** required (bhavcopy uses NSE symbol). Symbols change on M&A, splits, delistings -> weekly refresh.
  - Holiday calendar mismatch with `master_calendar` can cause spurious freshness failures.
- **Failure mode**: Loud (404 on URL change), silent (symbol drift).
- **Cadence**: Endpoint change ~once per year; symbol-mapping refresh weekly.
- **Mitigation planned**:
  - Weekly symbol-mapping refresh job.
  - NSE holiday calendar synced.

---

## TIER 3 — Lower burden, worth flagging

### 7. AMC mergers, scheme mergers, scheme renames

- Indiabulls -> Groww, Principal -> Sundaram, IDFC -> Bandhan, IDBI -> LIC: each changes scheme codes, ISINs, AMC slugs.
- Scheme mergers chain `scheme_code` lineage across the event — current `scheme_master` doesn't model this.
- Phase 2 metrics needing history (Active Share 1Y median, manager tenure across mergers) need a **scheme lineage graph**.
- **Cadence**: 2-4 events per year industry-wide.
- **Mitigation planned**: `scheme_lineage` table mapping (old_scheme_code -> new_scheme_code, event_date, event_type). Manual entry as events occur.

### 8. Sector / category reclassification

- SEBI / AMFI occasionally change category names or merge categories.
- Active Share needs the right benchmark — fund flipping from "Thematic" to "Sectoral - Manufacturing" mid-year changes its benchmark.
- **Cadence**: 1-2x per year.
- **Mitigation planned**: existing `CATEGORY_RULES` / `SECTOR_RULES` absorb most; "benchmark history" column eventually (Phase 3).

### 9. PDF extraction tooling stack

- `pdfplumber` reliable but slow; `camelot` needs Java + Ghostscript (deployment pain); `tabula` JVM-based; `unstructured` heavy.
- OCR (`pytesseract`) has its own dependency tree (Tesseract binary, language packs).
- **Cadence**: occasional tool upgrades, dependency conflicts in `uv.lock`.
- **Mitigation planned**: `pdfplumber` first, `camelot` only for tables it can't handle, OCR deferred until needed.

### 10. Holdings-disclosure cadence and lag

- AMFI mandate: publish by 10th of next month. **Compliance patchy** — some AMCs publish on the 20th.
- Current `freshness.py` model expects bounded staleness; need ~45 day tolerance for holdings or pipeline fail-fasts monthly.
- **Cadence**: zero maintenance, just a design constraint.
- **Mitigation planned**: `max_holdings_lag_days = 45`.

---

## TIER 4 — Existing brittleness in the pipeline (pre-Phase 2)

Pre-existing; shares the maintenance pool but not caused by Phase 2.

- **RBI press-release HTML scraping** (`ingest/fbil_tbill.py`) — format-fragile, retries baked in.
- **AMFI NAVAll daily flat-file** — pipe-delimited, interleaved headers, has changed once historically.
- **AMFI category strings** drift occasionally — `CATEGORY_RULES` in `scheme_master.py` needs hand-updates.
- **Manual hybrid-benchmark CSVs** — maintained by hand today.

---

## Cross-cutting mitigations baked into all of Phase 2

1. **Quality flags propagate to ranking**. Any scheme with a low-confidence Active Share / PTR / tenure value gets that contribution zeroed and the weight redistributed. Broken parser quietly downgrades the scheme rather than poisoning it.
2. **Snapshot tests per AMC** in `tests/fixtures/factsheets/{amc}/` with expected extracted values. Regressions caught in CI, not prod.
3. **Loud freshness fail-fast** for every new source (per the pipeline fail-fast invariant). A partially-scraped run halts the pipeline rather than producing a half-broken ranking.
4. **Per-AMC coverage report** at the end of each run: "Active Share computed for X/Y schemes. Missing AMCs: ...". Coverage degradation visible.

---

## Realistic ongoing maintenance load

After Phase 2 ships: **~2-6 engineer-hours/week** to keep scrapers green. PDF scrapers (Tier 1.1 and 1.3) dominate the cost.

---

## Off-ramps available if Tier-1 burden becomes too high

These were offered before Phase 2.0 started; revisit if the Tier-1 picture worsens.

- **A. Manual-CSV escape hatch as a backstop, not primary.** Scrapers from day one, but every scraper has a `data/raw/.../manual/` override directory. When a scraper breaks, drop a CSV in.
- **B. Top-15 AMCs (~90% AUM) for factsheet scrapers, full coverage for portfolio holdings only.** Manager tenure / PTR null for long tail, weight redistributed. Halves Tier-1 burden.

---

## Revisit checklist (use this after Phase 2.0)

- [ ] Did any Tier 1 assumption already fail before scraper work started?
- [ ] Is the maintenance budget (~2-6 hrs/week) still acceptable, or do we want off-ramp A or B?
- [ ] Are there any new brittle pieces discovered during 2.0 that should be added here?
- [ ] Did the `factsheet_extraction_quality` flag design (planned in mitigation for Tier 1.1) survive contact with the actual data?

---

## Findings from real adapter work — accumulating as we ship

Each subsection captures empirical lessons from a specific adapter. Future
adapters should check this section before assuming their AMC behaves the same.

### PR 2.1.B — HDFC adapter (calibrated 2026-05-24 against April 2026 factsheet)

**Coverage**: 45 / 48 scheme pages parsed cleanly (93.75%).

**Layout learnings**:
- HDFC factsheets are text-extractable (no OCR needed for this AMC).
- The per-scheme FUND MANAGER section is usually a pdfplumber-extractable
  table with header row exactly `['Name', 'Since', 'Total Exp']`.
- Date format is consistently `'MonthName DD, YYYY'`. pdfplumber may insert
  newlines mid-cell from column-wrapping — normalize whitespace before parsing.
- HDFC **never labels** a "Lead" or "Primary" manager (0 / 48 pages). For
  this AMC the picker always falls through to the "earliest start date" rule.
  This may be the norm for Indian AMCs rather than the exception — keep
  watching across other adapters.

**Edge cases that needed code**:
1. **Header-only table** (HDFC Large Cap Fund, p11): pdfplumber's table
   detector found `'Name | Since | Total Exp'` but no data rows. The manager
   row was actually on the page but in an unrecognized layout. **Mitigation
   shipped**: word-position fallback in `_extract_manager_rows_by_words` that
   reads `extract_words()` and reconstructs rows from x/y coordinates of the
   Name and Since columns. Ignore the Total Exp column (we don't need it).
2. **Column bleed** (HDFC Multi-Asset Allocation Fund, p49): pdfplumber
   merged 2 manager rows into one table cell, with the trailing year of the
   second date truncated (wrapped to a continuation page). Detecting via
   "count of full `Month DD, YYYY` matches" failed because the truncated date
   had no year. **Mitigation shipped**: count month-name occurrences in the
   Since cell — `>1` → log and skip.
3. **Continuation pages** (Multi-Asset Allocation spans p49-51): HDFC marks
   continuation pages with `'....Contd from previous page'` and does NOT
   re-print the FUND MANAGER table. The parser correctly skips them. Other
   AMCs may re-print on continuation; watch for duplicate records.

**Known unresolved gap**:
- **HDFC Housing Opportunities Fund** (p33): sectoral equity fund, IS in the
  ranked universe, but neither standard nor word-position extraction yielded
  manager records. Cause not yet investigated. Investigate if this scheme's
  null tenure starts moving the ranking output materially.

**Validated assumptions**:
- The brittleness audit's planned snapshot-test-per-AMC pattern works
  cleanly: `tests/test_ingest_hdfc.py` covers known layout outliers with
  named regression checks. Future format drift breaks specific named tests.
- Fuzzy-match threshold of 85 (rapidfuzz token_set_ratio) accepts every
  HDFC printed scheme name → AMFI scheme_name combination we saw. No
  false matches observed.

**Open questions to answer with the next adapter (ICICI Pru)**:
- Do other AMCs label a "Lead" manager? If so, the picker's `is_lead`
  branch starts mattering.
- Do other AMCs use "Mr./Ms./Dr." honorifics inline with manager names?
  HDFC doesn't; we haven't tested honorific stripping yet.
- Do other AMCs use abbreviated month names ("Apr 30, 2025")? HDFC uses
  full names only.
- Do continuation pages re-print the FUND MANAGER table elsewhere?

### PR 2.1.C — ICICI Pru deferred, SBI shipped (calibrated 2026-05-24)

**ICICI Pru — deferred** (resolved by skipping this AMC for now):
- icicipruamc.com is a JS-rendered React SPA. The factsheet PDF URL is not
  in the initial HTML. The `/docs/default-source/factsheet/factsheet.pdf`
  path returns a 307 to `archive.icicipruamc.com`, **which does not resolve
  via DNS**. The JS bundle does not expose an API endpoint we could grep
  for. To unblock, we would need a headless browser (Playwright) or a
  user-supplied PDF. Per the user's 2026-05-24 decision: skip for Phase
  2.1, revisit during the post-2.3 brittleness pass.

**SBI Mutual Fund — coverage 38 schemes** including all equity flagships:
- URL discovered via Wayback Machine CDX search:
  `https://www.sbimf.com/docs/default-source/scheme-factsheets/all-sbimf-schemes-factsheet-{month_lower}-{year}.pdf`
  (no `sfvrsn=` needed; Sitecore resolves latest version).
- **Layout completely different from HDFC**:
  - Manager block is text-flow in a left column, NOT a pdfplumber table.
    Required word-position extraction with x-coordinate gating.
  - Scheme name lives at the **page footer** (y > 600), not the top.
    Top of each page shows the SEBI category banner ("EQUITY-LARGE CAP").
  - **Two date formats coexist** in the same document:
    `(w.e.f. Apr 2024)` and `Jan-2022`. No day in either; default to day=1.
  - "Mr./Ms./Dr." honorifics standard — strip before saving.
  - **`use_text_flow=True` is essential** when calling `extract_words()`:
    Banking & FS and PSU pages have shattered word groups under the default
    settings (e.g. "Managing Since:" comes out as individual characters).
    Text-flow mode reconstructs the correct words.
  - Column boundary much tighter than HDFC (x < 195 for SBI vs x < 220 for
    HDFC). Loose boundaries leak portfolio holdings into the name strings.
  - **Names sometimes truncated in "Managing Since:" rows** (column too
    narrow for full surname). Architecture: read names from the
    "Fund Manager:" header line (which has full names), dates from the
    "Managing Since:" rows, then zip by ordinal position.
  - **Footnote markers** (`#`, `*`) attached to manager names — strip from
    token ends before regex-validating.
  - "Total Experience:" header sometimes drops its colon → accept both forms.
- **Confirmed: SBI never labels a "Lead" manager** either. Combined with
  HDFC: lead-labeling appears to be the exception, not the norm, in Indian
  AMCs. The picker's `is_lead` branch is dead code so far.

**Known unresolved equity gaps (SBI)**:
- SBI Equity Hybrid Fund (p41), SBI Equity Savings Fund (p45),
  SBI Balanced Advantage Fund (p47, p73), SBI Multi Asset Allocation Fund
  (p43, p97). These are hybrid/balanced funds — different page layouts.
  Acceptable for v1 of the adapter; revisit during brittleness pass.

**Validated patterns**:
- Per-AMC integration tests (named regression checks for specific
  layout outliers) caught everything I expected them to. The pattern is
  worth replicating for every new adapter.
- Module-scoped pytest fixture for parsing the PDF once kept the suite
  fast (~75s with 2 AMC integrations vs ~150s without caching).

**Open questions for PR 2.1.D (next AMC — likely Nippon or Aditya Birla)**:
- Will the AMC publish at a stable PDF URL like HDFC + SBI, or require
  SPA scraping like ICICI Pru?
- Are there AMCs that label "Lead" — making the `is_lead` branch finally
  matter?
- Are there AMCs whose factsheets are image-only PDFs (forcing OCR)?

### PR 2.1.D — Nippon India shipped (calibrated 2026-05-24)

**Coverage**: 118 records across 96 distinct printed schemes — broadest universe
yet. Nippon publishes a combined factsheet for both their equity schemes AND
all their ETFs/FoFs, which is why the count is higher.

**URL pattern** (clean static link, no Wayback hunt needed):
  `https://mf.nipponindiaim.com/InvestorServices/FactSheetsDocuments/Nippon-FS-{MMM-uppercase}-{YYYY}.pdf`
  e.g. `Nippon-FS-APR-2026.pdf`. Older months (>~3 months back) migrate to
  `/InvestorServices/FactSheets/` with title-case month — not modeled,
  since we always target the latest.

**Layout — the cleanest of the three AMCs so far**:
- Each scheme page starts with the canonical scheme name on line 1:
  `"Nippon India <X> Fund"`. No header banner gymnastics needed.
- Manager block under a `"Fund Manager(s)"` header (note: parenthetical 's').
  Per-manager lines have a single, parseable structure:
  `<Name> [(Assistant Fund Manager) | (Co - Fund Manager)]? (Managing Since <Mon> <Year>)`.
- Date format: full or abbreviated month name + year, no day. Default day=1.
- No honorifics. Names are plain.
- Right-column portfolio bleeds into text flow → word-position extraction
  confined to x < 200 still required.
- `use_text_flow=True` matters here too (same finding as SBI). Without it,
  multi-manager lines wrap inconsistently.

**Big new finding: Nippon DOES label co-managers.**
- The first manager listed (no role label) is the lead.
- Subsequent managers are tagged `(Assistant Fund Manager)` or
  `(Co - Fund Manager)` (with literal spaces around the hyphen).
- This is the first AMC where `is_lead` is non-trivial. The picker's lead
  branch is finally exercised. Out of 21 multi-manager Nippon schemes
  observed, all have exactly one lead.
- HDFC + SBI never label a lead. Nippon labels every multi-manager case.
  So the "lead-if-labeled-else-earliest" rule actually matters for some
  AMCs, not all. The previous adapters' picker behavior was a no-op fall-
  through; now the lead branch carries weight on a real AMC.

**pdfplumber quirk**: this PDF emits a flood of
`"Could not get FontBBox from font descriptor"` warnings — hundreds per page.
They don't affect text extraction. Suppressed in the adapter via
`logging.getLogger("pdfminer").setLevel(logging.ERROR)` so the CLI output
stays readable.

**Validated assumptions**:
- The picker rule "lead-if-labeled-else-earliest" finally produces a
  different answer than naive "earliest" — for Nippon Large Cap Fund, the
  rule picks Sailesh Raj Bhan (lead, 2007-08) over Bhavik Dave (assistant,
  2024-08). Without the lead flag the picker would still pick Sailesh
  because he's earliest, but on Nippon Flexi Cap Fund the lead is Meenakshi
  Dawar (2023-01) while Dhrumil Shah is the assistant since 2021-08 — i.e.
  the assistant has the EARLIER start date. The lead flag is what makes
  the right answer here.

**No known equity gaps for Nippon** in the April 2026 PDF — all flagship
schemes parsed cleanly with the regex-based line approach.

**Open questions for PR 2.1.E (Aditya Birla Sun Life)**:
- Aditya Birla closes the top-5 AUM list. URL discoverability still TBD.
- Will Aditya Birla also label leads (like Nippon) or not (like HDFC/SBI)?
- Will we see a fourth different date format?

### PR 2.1.E — Aditya Birla Sun Life shipped (calibrated 2026-05-24)

**Coverage**: 120 records across 74 distinct schemes — strong, includes both
equity AND debt+hybrid universes.

**URL pattern** (live but historically unstable):
  `https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/factsheets/{publish-YYYY}/absl-factsheet_{publish-month-lower}-{publish-YYYY}.pdf`
- ABSL names files by **publish month**, not data month (different from
  HDFC/Nippon which name by data month). The May 2026 publication carries
  April 2026 data — `build_url` converts data-month to publish-month
  internally.
- URL pattern was different in 2025: `abslmf_empower-<month>-<year>` with
  inconsistent separator usage (`_` vs `-`, full vs short year, `_v1`
  suffix on some). The rename to `absl-factsheet_<month>-<year>` happened
  in early 2026.
- **Old months get rotated off the server**: only the latest 1-2 months
  remain accessible at the stable URL. April 2026 was removed when May
  2026 published. Cannot backfill more than ~2 months without Wayback.

**Layout**:
- Scheme name on page line 2 (after a "Equity Funds"/"Hybrid Funds" banner).
- Manager block is text-flow in a narrow left column (x < ~175). Right
  column starts at x ≥ ~194 — **cleanest column separation of all four AMCs**
  shipped so far (~60-point gap).
- **Each manager has their OWN "Fund Manager" header** — unlike Nippon's
  single "Fund Manager(s)" with a list:
      Fund Manager -Mr. <Name>
      Managing the Fund Since: <Month> <DD>, <YYYY>
      Experience in Managing the Fund: <N.N> Years
- Date format: "Month DD, YYYY" with day (matches HDFC, differs from
  SBI/Nippon which omit the day).
- "Mr./Ms." honorifics standard (like SBI).
- **No lead/co labels** — falls back to "earliest" rule (matches HDFC, SBI;
  Nippon remains the only top-5 AMC that labels leads).
- pdfminer FontBBox warnings present here too — same logger suppression as
  Nippon.

**Two specific quirks that required parser work**:
1. **Two-line scheme names**. Longer names wrap across two physical lines:
       Aditya Birla Sun Life Balanced
       Advantage Fund April 2026
   The scheme-name regex must try 1-line, 2-line, and 3-line concatenations
   of the page's first lines. Affected schemes (caught by named regression
   test): Balanced Advantage Fund, ELSS Tax Saver Fund, Equity Savings Fund,
   Equity Hybrid '95 Fund, Dividend Yield Fund.
2. **Unicode quotation marks**. "Equity Hybrid '95 Fund" uses U+2018 (left
   single quote), not a regular apostrophe. The scheme-name regex character
   class must include `‘’"`.

**Validated patterns**:
- Per-manager "name line + date line" pairing by ordinal works reliably on
  ABSL because each manager gets their own complete block. No truncation
  problems like SBI's Managing Since rows.
- The 60-pt column gap means `_LEFT_COL_MAX_X = 175` cleanly excludes
  right-column bleed without tuning.

**Top-5 AUM coverage at end of Phase 2.1.E**:
| AMC | Status | Schemes |
|---|---|---|
| HDFC | ✓ 2.1.B | 45 (94%) |
| ICICI Pru | ✗ Deferred — SPA blocker | — |
| SBI | ✓ 2.1.C | 38 equity flagships |
| Nippon India | ✓ 2.1.D | 96 (with lead detection) |
| Aditya Birla | ✓ 2.1.E | 74 |
**4 of top-5 shipped. ICICI Pru deferred — revisited and shipped in 2.1.F.**

**Cross-cutting learnings from four AMCs**:
- Every AMC has materially different URL conventions, file layouts, date
  formats, and scheme-name presentations. Nothing is portable adapter-to-
  adapter beyond the framework (registry + fuzzy matcher + ParsedManagerRecord).
- `use_text_flow=True` is essential on every AMC except HDFC (which uses
  proper tables). Default text extraction shatters words on most AMCs.
- Lead labeling is the exception (1 of 4 = Nippon), not the norm. The
  picker's lead branch matters only for Nippon so far.
- pdfminer FontBBox warnings are common; suppress at the logger level in
  every adapter that opens an affected PDF.
- `data/raw/factsheets/{amc}/{ym}.pdf` is treated as the data month even
  when the AMC names files by publish month. `build_url` does the conversion.

### PR 2.1.F — ICICI Pru shipped (deferral reversed 2026-05-24)

**Coverage**: 116 records across 59 distinct schemes.

**Unblocked by**: Wayback Machine CDX search (the same technique that worked
for SBI). The live homepage's React SPA still hides the link, but the
underlying static CDN path `/blob/downloads/Files/Historic Factsheets/{FY}/`
is stable. The lesson: when an AMC's website is a JS-rendered SPA, always
try Wayback's CDX before declaring the URL unreachable.

**URL pattern (with a quirk)**:
  `https://www.icicipruamc.com/blob/downloads/Files/Historic%20Factsheets/{FY}/Complete%20Factsheet%20{Month}%20{Year}.pdf`
- The FY directory is Indian fiscal year (Apr-Mar): FY 2025-26 = `2025-2026`.
- **But April 2026 data lives in the `2025-2026` directory**, not the
  expected `2026-2027`. ICICI Pru keeps the new FY's early months in the
  prior FY folder until they cut over (timing unclear). The adapter's
  `fetch()` tries both candidate FY directories and uses whichever returns
  200, so this is self-healing across the transition.

**Layout — fundamentally different from every other AMC we've seen**:
- The manager block is **narrative text**, not a table or labeled section:
    "The scheme is currently managed by X, Y and Z. Mr. X has been
     managing this fund since Feb 2026. Total Schemes managed by..."
- **Two interchangeable verb phrasings** in the same PDF:
    1. `<Name> has been managing this fund since <Date>`
    2. `<Name> currently manages the scheme since <Date>`
  Some pages use one, some use the other. Regex handles both.
- Date format: `<Month> <Year>` or `<Month>, <Year>` (comma optional). No
  day. Abbreviated or full month names both occur.
- Honorifics ("Mr./Ms.") are **inconsistently applied** — some pages have
  them, some don't (e.g. "Priyanka Khandelwal" with no prefix). Regex makes
  them optional.
- Apostrophes in names ("Sharmila D'silva") preserved via character-class
  allowance.
- No lead/co labels — picker falls to "earliest" (matches HDFC, SBI, ABSL).
- pdfminer column-bleed sometimes **prepends a single category-banner word**
  ("Concentrated", "Diversified") to the captured name when the previous
  line in the left column is a one-word style banner. Post-processing in
  `_clean_manager_name` strips known-banner first tokens.

**Edge cases handled by named regression tests**:
1. ICICI Prudential Smallcap Fund — 4 co-managers including "Sakshat Goel"
   (the page prepends "Concentrated" to his name before post-processing).
2. ICICI Prudential Large & Mid Cap Fund — uses the `currently manages the
   scheme since` phrasing (regression for the second verb branch).
3. Apostrophe in "Sharmila D'silva" — regression for the character class.

**Top-5 AUM coverage now COMPLETE**:
| AMC | PR | Schemes | Notable |
|---|---|---|---|
| HDFC | 2.1.B | 45 | Tabular extraction |
| ICICI Pru | **2.1.F** | **59** | **Narrative text + Wayback URL discovery** |
| SBI | 2.1.C | 38 | Text-flow + name truncation |
| Nippon India | 2.1.D | 96 | Lead labeling (only AMC) |
| Aditya Birla | 2.1.E | 74 | Two-line names + Unicode quotes |

**Total Phase 2.1 universe: ~312 schemes with manager data across 5 AMCs.**

**Cross-cutting learnings update (5 AMCs)**:
- Wayback CDX is the right move when a live page hides URLs behind JS.
- Lead labeling: 1 of 5 (Nippon only). Confirmed as the exception.
- Verb phrasings vary not just across AMCs but **within a single AMC's
  document**. Multiple regex branches are sometimes necessary.
- Inconsistent honorific use within an AMC (ICICI Pru) means we should
  always make Mr./Ms./Dr. optional rather than required.

---

## Phase 2.2 findings (holdings + Active Share + PTR)

### Critical pivot — factsheet PDFs DON'T have ISINs

The original Phase 2 plan assumed AMC factsheets would include ISINs in
their portfolio listings, allowing direct ISIN→ISIN matching for Active
Share computation. **This assumption was wrong.** All 5 AMCs print only
`Company Name / Industry / Weight%` in their factsheet portfolios. ISINs
appear only in separate per-scheme monthly portfolio Excel files (HDFC
publishes those; we haven't validated for the other AMCs).

**Schema refactor (mid-Phase 2.2.C)**: `holdings_monthly.isin` was made
nullable; the primary key changed to `(scheme_code, security_name,
as_of_month)`. Active Share now matches on **normalized security_name**
(lowercase + strip Ltd./Limited/punctuation) — both holdings and
constituent CSVs use the same normalization, so they interoperate
correctly.

### PR 2.2.C–G — per-AMC holdings extraction summary

| AMC | PR | Schemes parsed | Total holdings | Avg coverage | Layout quirk |
|---|---|---|---|---|---|
| HDFC | 2.2.C | 32 | 1,448 | ~75% equity weight | 2-column, multi-line name wraps |
| ICICI Pru | 2.2.D | 38 | 1,062 | ~85% equity weight | Sector groupings; duplicated top holdings; column bleed |
| SBI | 2.2.E | 20 | 800 | ~92% equity weight | 2-column, sector banners cleanly identifiable |
| Nippon | 2.2.F | 53 | 1,347 | ~75% equity weight | Cleanest 2-column layout |
| **ABSL** | 2.2.G | **0** | **0** | **n/a** | **Factsheet only shows sector breakdown — no individual holdings.** ABSL schemes get null active_share; pro-rata redistribution kicks in. |

### Active Share is dominated by per-AMC parsing brittleness

Two distinct shapes of parsing problems emerged:

1. **Multi-line name wraps (HDFC, ICICI Pru)**. When a stock name like
   "SBI Life Insurance Company Ltd." overflows the column width, parts
   spread across 2-3 y-bands. Required an "accumulate name words until
   you hit a weight column entry" algorithm in HDFC; ICICI Pru still has
   some mangled rows that get filtered by the multiple-"Ltd." sanity check.

2. **Column bleeding (ICICI Pru, all narrative-style AMCs)**. Two-column
   portfolio layouts where a sector header in one column appears at the
   same y-band as a stock in the other column. The text-extraction
   interleaves them. Mitigated with strict x-range column boundaries and
   a stock-suffix filter (require `Ltd|Limited|Inc|Corp` at end of name).

**Quality control**: each adapter applies three sanity filters:
- Stock-suffix presence required (drops sector aggregates)
- Reject names with `>1` "Ltd." occurrence (drops merged-name garbage)
- Reject names `>7` words (drops obvious wraps)

The DB primary key `(scheme_code, security_name, as_of_month)` provides
final dedup at write time.

### NSE Index Constituents — pivoted to manual CSV only

NSE/niftyindices.com publishes constituent **membership** at
`/IndexConstituent/ind_<name>list.csv` but **no weights**. Without weights,
Active Share can't be computed against the benchmark side. Per the
brittleness audit's locked decision, PR 2.2.B made manual-CSV the
**primary** ingestion path — `data/raw/index_constituents/manual/<ticker_slug>/<YYYY-MM>.csv`.
No live-fetch fallback shipped; users provide monthly weight CSVs.

### PTR (PR 2.2.H) — surprisingly clean

PTR is one number per scheme, printed with a clearly labeled prefix in
all 5 AMC factsheets. A single per-AMC regex extracts it:

| AMC | Pattern | Storage format |
|---|---|---|
| HDFC | `Equity Turnover N.NN%` | Divide by 100 → fraction |
| ICICI Pru | `Equity - N.NN times` | Direct fraction |
| SBI | `Equity Turnover : N.NN` | Direct fraction |
| Nippon | `Portfolio Turnover (Times) N.NN` | Direct fraction |
| ABSL | `Portfolio Turnover N.NN` | Direct fraction |

180 PTR records across 176 distinct schemes from the April 2026 data
month. Soft penalty wired into `score.py`: linear ramp from 0 at PTR=1.50
to -0.10 at PTR=3.00, capped thereafter.

### Phase 2.2 maintenance load

Per-AMC holdings extractors are noticeably more brittle than the manager
extractors (which proved highly stable). The wrap/bleed issues will
recur as AMC factsheet layouts drift. Expect **1-2 holdings adapters to
break per month**, vs roughly the same cadence for managers — i.e.
holdings approximately doubles the per-AMC maintenance burden.

The clean Excel/portal alternatives (HDFC publishes per-scheme XLSX
files; AMFI has a centralized monthly portfolio portal) would massively
reduce this burden. **Strong recommendation for the post-2.3 brittleness
pass: replace factsheet-based holdings extraction with one of those
sources.**

---

## Phase 2.3 findings (AUM Impact Cost + stress test)

### NSE bhavcopy (PR 2.3.B) — surprisingly stable

URL pattern `https://archives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv`
returned 200 + clean CSV on the first try. 14 columns, 2,141 equity rows
per day. NSE EQUITY_L.csv provides a complete `SYMBOL→ISIN` mapping
(2,364 entries) and is similarly stable.

Non-trading-day handling is built in: a 404 from the bhavcopy URL is
treated as "no data, skip" rather than a fatal error, so the daily walk
gracefully skips weekends and Indian holidays.

**Open**: bhavcopy CSV is the *legacy* endpoint. NSE introduced a newer
`BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip` format in late 2024 (the
brittleness audit's Tier 2.6 entry). We're on the legacy path because it
worked; if NSE deprecates it we'll need to switch (the new format would
require ZIP unpacking + slightly different column layout).

### AUM extraction (PR 2.3.C) — one regex per AMC, very clean

| AMC | Pattern | Sample |
|---|---|---|
| HDFC | `As on <date> ₹<num>Cr.` | HDFC Flexi Cap → ₹100,479.23 Cr |
| ICICI Pru | `Closing AUM as on <date> : Rs. <num> crores` | Large Cap → ₹75,650.43 Cr |
| SBI | `AAUM for the Month of <month>\n` then `<num> Crores` | Large Cap → ₹52,854.99 Cr |
| Nippon | `Month End: ₹ <num> Cr` | Large Cap → ₹46,520.53 Cr |
| ABSL | `Month End AUM <num>` | Large Cap → ₹28,969.90 Cr |

302 AUM records across ~296 distinct schemes — broader coverage than
holdings because AUM is on every scheme page (whereas holdings need the
explicit portfolio listing some schemes don't print).

### AUM Impact Cost compute (PR 2.3.A) — sensitive to ISIN coverage

The math is straightforward: `days_to_exit = (weight × AUM) / ADV`,
take max of top-10 highest. The dominant data quality issue:

- Holdings tables have **null ISIN for most rows** (factsheet PDFs don't
  print ISINs — see Phase 2.2 notes).
- bhavcopy keys on ISIN.
- So Active Share works (name-matching), but AUM Impact's ISIN-based join
  matches only the subset of holdings whose security_name happens to map
  via the index constituents table.

In practice, this means **AUM Impact Cost is populated only for schemes
where most holdings overlap with our benchmark constituent CSVs** —
which we control. For HDFC Flexi Cap (Nifty 500 TRI benchmark, ~75% of
holdings would be in the benchmark) we'd expect ~75% of holdings to have
ADV; below the min-3-holdings floor for some smaller funds.

**Strong dependency to flag**: AUM Impact for ABSL schemes will be null
(no holdings extracted, see Phase 2.2.G). The composite_weight slot for
this metric remains unallocated and absorbs no weight in score.py.

### SEBI stress test (PR 2.3.E) — manual-CSV only

SEBI mandates AMCs publish "days to liquidate 25%/50% of portfolio" for
Small Cap and Mid Cap funds quarterly since March 2024. There's no
single AMFI-hosted consolidated PDF — **each AMC publishes their own**
to their website (45 AMCs × 4 quarters = 180+ PDFs to scrape with
inconsistent formats).

Per the off-ramp A pattern, PR 2.3.E ships as **manual-CSV-only**:
users provide `data/raw/stress_test/manual/<YYYY-MM-DD>.csv` with
`(scheme_code, days_to_liquidate_50, days_to_liquidate_25, source)`.
The hard filter (PR 2.3.F) applies ONLY when this data is present;
schemes in Mid Cap / Small Cap with no stress-test data are kept.

### Stress test hard filter (PR 2.3.F) — Mid Cap + Small Cap only

Per the locked decision: drop schemes where
`stress_test_days_50pct > 15` AND `canonical_category ∈ {Mid Cap, Small
Cap}`. Other categories (Large Cap, Flexi Cap, sectoral, etc.) are
exempt regardless of the disclosed value — the SEBI mandate doesn't
apply, so we treat any value there as informational.

### Phase 2.3 maintenance load

Notably lower than Phase 2.2:
- bhavcopy is one CSV with stable URL (vs. 45 AMC PDFs).
- AUM is one number per scheme with clean per-AMC regex (vs. multi-line
  name+date manager parsing).
- Stress test is manual entry — zero maintenance until we automate.

Expect ~30 minutes per quarter to refresh stress test CSVs by hand,
plus the existing factsheet adapter maintenance (now also extracting AUM
alongside managers/holdings/PTR from the same PDF).

---

## Phase 2.4 discovery findings (long-tail AMCs)

### PR 2.4.A — Kotak Mahindra MF (partial — blocked)

**URL pattern discovered** via Wayback CDX:
`https://www.kotakmf.com/factsheet/<Month>_<Year>/Kotak%20MF%20Factsheet%20<Month>%20<Year>.pdf`

April 2026: 19MB PDF, 188 pages. Downloaded cleanly.

**Layout finding — manager start date NOT present in factsheet**:
Kotak's scheme pages print `Fund Manager*: Mr. Rohit Tandon` but no
`Managing since: <date>` field anywhere on the scheme page. A back-of-PDF
annexure (pages 180-188) has per-manager BIO pages with career history
dates (e.g. "since 2014", "October 2007") but these are about the
manager's career, not per-scheme tenure.

**Impact**: Without per-scheme manager start dates, we can't compute
`manager_tenure_years` for Kotak schemes. The soft penalty wouldn't
trigger; manager_name could be captured but the schema requires a
non-null `manager_start_date`.

**Implication for the rest of Phase 2.4**: Long-tail AMCs likely vary
in whether they print per-scheme manager start dates. Top-5 (HDFC,
ICICI Pru, SBI, Nippon, ABSL) all did. Kotak doesn't. Each new AMC
needs PDF inspection to confirm before parser work — there's no
short-cut.

### Long-tail scope reality

Discovery alone (URL + download + layout inspection) takes 20-30 min per
AMC. Writing the parser, calibrating, and testing adds another 30-60 min.
**Realistic budget: ~60-90 min per AMC** even with established patterns.

10 AMCs = 10-15 hours of focused work. Not achievable in one autonomous
session.

**Three potential resolutions**, ordered by likely value:

1. **Do the brittleness pass first** (as originally planned). The
   manual-override fallback layer would let us capture *whatever* fields
   each AMC publishes (name only, name+date, full block) via per-AMC
   manual CSV when the auto-scraper falls short. This sidesteps the
   "Kotak has no start date" problem because we'd use manual entry for
   that signal specifically.

2. **Schema change**: make `manager_start_date` nullable. Kotak-style
   AMCs would emit name-only records. The soft penalty already handles
   null tenure gracefully (zero contribution). Cost: ~1 hour schema
   work + propagation.

3. **Cherry-pick AMCs that have start dates**: requires per-AMC PDF
   inspection (which is the expensive step). Hard to budget without
   doing the work.

### Recommended pivot (2026-05-24)

Given the scope reality, the **brittleness pass should come BEFORE
Phase 2.4** (reverting to your original ordering). Building the
manual-override layer first lets us ship long-tail adapters in batches
of 5-10 AMCs per session, with the auto-scraper handling what it can
and manual CSVs filling gaps. Each new AMC then becomes incremental
rather than a fresh per-PDF investigation.

### Constraints clarified 2026-05-24 (post-survey)

After the survey work, the user made two further decisions that reshape
long-tail strategy:
- **No manual data entry / manual-override layer.** Anywhere data is
  missing, we don't fill it by hand.
- **No nullable strict fields.** Don't soften the schema to admit
  partial data; skip the AMC entirely.

Net effect: an AMC is "ingestable" only if its factsheet publishes the
full `(scheme, manager_name, manager_start_date)` tuple cleanly enough
to scrape. AMCs that publish only names go in the "skip" pile.

### Long-tail survey results (April 2026 factsheets)

| AMC | URL discovered | Download | Manager start dates? | Verdict |
|---|---|---|---|---|
| Kotak Mahindra | ✓ | ✓ (19MB) | ✗ — name only, no per-scheme dates | **SKIP** |
| UTI MF | ✗ | — | — | Discovery needed |
| Axis MF | Per-scheme PDFs only (not consolidated) | — | — | Different model |
| DSP MF | Hash-based URLs | — | — | Discovery needed (URL hash changes per file) |
| Mirae Asset | ✓ | ✓ (8MB) | **✓** — `Mr. X (since Month DD, YYYY)` | Eligible (but multi-scheme-per-page layout adds parser complexity) |
| Tata MF | ✓ | ✗ 403 | — | Anti-bot blocked |
| Edelweiss | ✓ | ✗ 403 | — | Anti-bot blocked |
| Quant MF | ✓ (2025 only) | ✗ 404 for Apr 2026 | — | URL pattern may have changed |
| PPFAS | ✓ (2021 paths) | ✗ 404 for Apr 2026 | — | URL pattern may have changed; single fund only |
| Bandhan | ✗ | — | — | Discovery needed |

**Net: 1 of 9 long-tail AMCs survives the "publishes per-scheme manager
start dates AND we can access the file" criterion.** Mirae is shippable
but needs a multi-scheme-per-page parser pattern that's distinct from
the top-5 adapters.

**Strategic implication**: With the no-manual / no-nullable constraints,
the long-tail value is small. Coverage gain from going beyond top-5
plus Mirae is materially capped by these AMCs' disclosure practices.

---

## Phase 3.C findings (per-AMC monthly portfolio Excels with ISINs)

### Critical correction — there is NO centralized AMFI portfolio portal

The Phase 3 close-the-gap plan as originally written suggested migrating
holdings to `portal.amfiindia.com/spages/MFPortfolio.aspx`. That URL
returns 404 — **AMFI does not centralize portfolio data**. SEBI mandates
each AMC publish its own monthly portfolio disclosure (since Feb 2021)
but the disclosures live on AMC websites, not on an AMFI aggregator.

The plan was therefore pivoted on 2026-05-24 to one-adapter-per-AMC,
sourcing each AMC's own monthly portfolio Excels. The factsheet-PDF
holdings path remains in place as the fallback for AMCs whose
portfolio-Excel adapter isn't shipped yet.

### HDFC monthly portfolio Excels (Phase 3.C first adapter)

- **URL pattern**: `https://files.hdfcfund.com/s3fs-public/{YYYY-MM}/`
  `Monthly%20<scheme>%20-%20<DD>%20<Month>%20<YYYY>.xlsx`. Publish month =
  data month + 1 (same convention as ABSL/Mirae/SBI factsheets).
- **Discovery**: scrape `https://www.hdfcfund.com/statutory-disclosure/`
  `portfolio/monthly-portfolio` once per month; the rendered HTML carries
  ~109 `.xlsx` links per month for every active scheme.
- **Sheet layout** (validated against Flexi Cap, April 2026):
  - Sheet 0 = equity holdings ('HDFCEQ' / 'HDFCEQ-LC' / etc).
  - Sheet 1 = derivatives ('DerivativeHDFCEQ'). Currently ignored — Active
    Share is computed on cash equity and derivatives use a different
    column layout.
  - Row 5 is the header: `ISIN | Coupon (%) | Name Of the Instrument |`
    `Industry+ /Rating | Quantity | Market/ Fair Value (Rs. in Lacs.) |`
    `% to NAV | Yield | ~YTC`.
  - Section banners ('EQUITY & EQUITY RELATED', 'Equity', 'Debt
    Instruments', 'REIT', 'Net Current Assets') appear with the banner
    label in the ISIN column. The adapter walks rows and tracks the
    most-recent section banner to tag instrument_type.
- **Output quality on Flexi Cap snapshot**:
  - 67 holdings rows extracted.
  - Total equity weight: 95.63% (compare Phase 2.2's ~75% from factsheet).
  - ZERO rows with missing ISIN (the whole point of the migration).
  - Mixed instrument types observed: Equity / Debt / REIT/InvIT.

### Brittleness expectations for portfolio-Excel adapters

- **URL pattern drift**: each AMC's CDN convention differs and may change.
  HDFC's pattern is on Drupal s3fs-public; mid-tier AMCs sometimes use
  signed URLs that rotate weekly.
- **Sheet name drift**: HDFC uses scheme-specific sheet names like
  'HDFCEQ' / 'HDFCMC' / 'HDFCLC'. The adapter just uses sheet index 0; if
  a future scheme is published with the equity sheet at index 1, this
  fails loudly.
- **Header row position**: Phase 3.C's adapter assumes the column header
  at row 5 (1-indexed) and that subsequent rows are data + section banners.
  A 1-row banner addition (e.g. SEBI mandates a new disclosure note above
  the header) would shift the header row and yield zero rows. Add header
  row autodetection if this becomes an issue.
- **Unit drift**: Market values are quoted in lakhs in HDFC's template.
  Other AMCs may quote in crores or rupees. The adapter currently uses
  only the `% to NAV` column (unit-free) — switch to value-based math
  only after explicit per-AMC unit-detection logic.
- **Section banner taxonomy**: `_classify_section` is regex-light. As we
  add adapters for ICICI/SBI/Nippon, the section labels may differ
  (e.g. ICICI uses "Equity Shares" vs HDFC's "Equity"). Expect each
  adapter to either contribute back labels or override the classifier.

### SBI MF monthly portfolio Excels (Phase 3.C second adapter)

- **Discovery model**: SBI's `/portfolios` page is a Sitefinity SPA. The
  HTML rendered by the browser is sourced from a POST AJAX endpoint
  (`POST /ajaxcall/CMS/GetSchemePortfolioSheets` with a JSON body of
  `{"FundId":0,"PSYear":"2026","PSMonth":"April","PSFrequency":"Monthly"}`).
  Returns a `<tbody>` HTML fragment with one row per scheme. The adapter
  posts directly rather than loading the SPA.
- **URL pattern**: `https://www.sbimf.com/docs/default-source/`
  `scheme-portfolios/<scheme-slug>-monthly-portfolio---<month-lower>-<YYYY>.xlsx`.
  No publish-month folder (unlike HDFC), but there's an `?sfvrsn=<hex>_2`
  cache-buster suffix that the adapter strips.
- **Aggregate file**: SBI also publishes
  `all-schemes-monthly-portfolio---as-on-30th-april-2026.xlsx` as a
  single workbook covering all schemes. The adapter rejects it at
  filename-regex level so it's not double-ingested.
- **Sheet layout**: single sheet, named after the scheme's internal code
  (e.g. 'SBLUECHIP' for Large Cap). Header at row 6: cols are
  C=name, D=ISIN, E=Rating/Industry, F=Quantity, G=Market value (Lakhs),
  H=% to AUM. **Note ISIN sits in col D for SBI vs col B for HDFC** —
  the parsers do not share a layout constant.
- **Section banners**: 'EQUITY & EQUITY RELATED', 'a) Listed/awaiting
  listing on Stock Exchanges' (sub-banner — does NOT change instrument
  type), 'Equity Shares', 'DEBT INSTRUMENTS', 'Money Market Instruments',
  'TREPS', 'REITs & InvITs', 'Net Receivables/(Payables)'.
- **HTML entity encoding**: SBI's anchor labels carry `&amp;` for
  ampersands; the adapter `html.unescape`s before parsing.
- **Validation snapshot**: SBI Large Cap Fund April 2026 → 51 holdings,
  98.0% total weight, ISINs all valid, only Equity + Debt sections present.

### Nippon India monthly portfolio Excels (Phase 3.C third adapter)

- **Discovery model**: ONE consolidated workbook covers all ~109 NIMF
  schemes — distinct from HDFC/SBI where each scheme has its own file.
  Sheet 0 is named `Index` and maps 2-character sheet codes to scheme
  names; sheets 1..N are the per-scheme tables.
- **URL pattern**: `https://mf.nipponindiaim.com/InvestorServices/`
  `FactsheetsDocuments/NIMF-MONTHLY-PORTFOLIO-<DD>-<Month>-<YY>.xls`
  with `DD`=month-end day, `Month`=mixed full/abbreviated month name,
  `YY`=2-digit year. Legacy filenames used underscores (`NIMF_MONTHLY_…`)
  — the regex accepts both.
- **`.xls` extension trap**: the body is OOXML zip, not the legacy
  Excel binary format. openpyxl rejects the `.xls` filename suffix; the
  adapter caches the download with a `.xlsx` extension before opening.
- **Unit trap (critical)**: Nippon stores `% to NAV` as a **fraction**
  (0.0924 = 9.24%), not as a percent. The HDFC/SBI parsers expect
  percent. The Nippon adapter multiplies by 100 and a regression test
  pins this against HDFC Bank's weight in Large Cap.
- **Sheet layout** (per scheme): row 3 header — col B=ISIN, C=Name,
  D=Industry/Rating, E=Quantity, F=Market Value (Lacs), G=% to NAV,
  H=Yield. Same ISIN column as HDFC (col B), unlike SBI (col D).
- **Section banners**: 'Equity & Equity related', '(a) Listed / awaiting
  listing on Stock Exchanges' (sub-banner), '(b) UNLISTED', 'Money
  Market Instruments', 'Certificate of Deposit', 'Triparty Repo/ Reverse
  Repo Instrument', 'Debt Instruments', 'Non Convertible Debentures',
  'Government Securities', 'Zero Coupon Bonds', 'Preference Shares',
  'REIT', 'InvIT', 'OTHERS', 'Net Current Assets', 'GRAND TOTAL'.
- **Segregated portfolios**: a few legacy schemes (Aggressive Hybrid,
  Credit Risk) append a second mini-table for Yes Bank "Segregated
  Portfolio 2" after the first GRAND TOTAL. The parser hard-stops at
  GRAND TOTAL to avoid double-counting.
- **Case-inconsistent scheme names**: the Index sheet mixes uppercase
  ("NIPPON INDIA LARGE CAP FUND") and titlecase. Lookup is case-insensitive.
- **No Cloudflare block observed** — both pages and Excel downloaded
  cleanly on the first attempt with the project's default Chrome UA.
- **Validation snapshot**: Nippon India Small Cap Fund (flagship) April
  2026 → 250 holdings, 97.4% total weight, all ISINs valid, sections
  observed: Equity, Debt, REIT/InvIT.
