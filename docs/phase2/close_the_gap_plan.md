# Close-the-Gap Plan — Phase 3

**Goal**: bring all 7 Phase-2 metrics to production-ready state across the
ranked mutual-fund universe.

This document is the execution plan after the conversation compacts.
After compaction, read this end-to-end before starting any phase — it
captures the current state, the user's constraints, and the concrete
work items for each remaining gap.

---

## Current state inventory (as of 2026-05-24, end of Phase 2.4)

### What's already shipped

- **Phase 2.0** — Schema + composite-weight refactor. Active Share, Style
  Drift columns reserved with their weights in `composite_weights`.
- **Phase 2.1** — Manager tenure adapters for **6 AMCs**: HDFC, ICICI Pru,
  SBI, Nippon, Aditya Birla, Mirae. ~300 manager records across ~250-300
  distinct schemes. Soft penalty live (max -0.05 below 3y).
- **Phase 2.2** — Holdings + Active Share + PTR. Holdings extracted for
  4 AMCs (ABSL has no portfolio listings in its factsheet). PTR for 5 of 5
  top AMCs. Active Share math wired but populates only when holdings AND
  benchmark constituents both exist. PTR soft penalty live (max -0.10 above
  PTR=1.50).
- **Phase 2.3** — AUM Impact Cost + SEBI stress test. NSE bhavcopy daily
  ingestion working. AUM extraction for 5 top AMCs. Stress test hard
  filter for Mid Cap + Small Cap (>15 days → drop) live. Stress test
  manual-CSV ingestion path exists but no data ingested.
- **Phase 2.4** — Long-tail survey. Kotak skipped (no per-scheme start
  dates). Mirae shipped at ~40% coverage. Other 8 long-tail AMCs blocked.

### Test count baseline: 235 passing

### What's NOT done (the gaps)

| Metric | Math | Coverage | Production-ready? |
|---|---|---|---|
| Style Drift | ✓ | All ranked schemes | **YES** |
| Manager Tenure | ✓ | Top-5 AMCs + Mirae partial | Partial |
| PTR | ✓ | Top-5 AMCs only | Partial |
| Active Share | ✓ | Sparse (depends on holdings + constituent weights) | Degraded |
| AUM Impact Cost | ✓ | Very sparse (needs ISIN match) | Degraded |
| SEBI Stress Test | ✓ | 0 schemes | Dormant |
| **Portfolio Overlap (30% cap)** | ✗ | N/A | **Not started** |

**AUM-weighted AMC coverage: ~66%** (top-5 + Mirae) out of 45 AMCs total.

---

## User constraints to honor (do not violate)

These are load-bearing decisions made during Phase 2.4. They restrict the
solution space:

1. **No systematic manual-override layer.** Don't propose `data/raw/.../manual/*`
   directories as a primary scraping fallback or as infrastructure that
   admits sloppy auto-scrape. Existing manual paths (NSE constituents
   `data/raw/index_constituents/manual/`, stress test
   `data/raw/stress_test/manual/`) are grandfathered — don't add new ones.

2. **No nullable strict fields.** When an AMC doesn't publish a required
   field (e.g. Kotak with no per-scheme manager_start_date), the answer
   is to skip that AMC, not soften the schema.

3. **Pipeline must fail-fast on stale data.** See [[feedback-pipeline-fail-fast]].

4. **One narrow exception (offered by user 2026-05-24)**: for genuinely
   un-scrapable schemes, the user is willing to supply specific data via
   a SINGLE CSV. This is NOT a general fallback layer — it's gap-fill for
   specific identified cases. Phase 3.B builds this path.

---

## Phase 3 work breakdown

Phases are largely independent; pick any order based on impact preference.
**Recommended sequencing** at the bottom.

> **Note**: Phase 3.A (Portfolio Overlap, 30% pairwise cap) was deferred by
> user decision 2026-05-24. The other phases (3.B–3.F) are in scope.
>
> **Progress (2026-05-24)**:
> - **3.F shipped** — `tenure_data_status` column on shortlist output
>   (`auto` / `user_provided` / `not_available`). 5 unit tests in
>   `tests/test_ranking.py`.
> - **3.B shipped** — `mfs ingest managers --user-provided` loads gap-fill
>   CSV at `data/raw/managers/user_provided.csv` with strict validation. 27
>   tests in `tests/test_ingest_managers_user.py`. Test count: 235 → 267.
> - **3.C step 1 shipped** — `mfs ingest holdings --amc hdfc` pulls all
>   ~109 monthly portfolio Excels from HDFC. Total equity weight 95.6%
>   (vs ~75% for factsheet path), every row carries ISIN. 29 tests in
>   `tests/test_ingest_holdings_hdfc.py`. Test count: 267 → 296.
> - **3.C remaining**: ICICI Pru, SBI, Nippon monthly-portfolio adapters
>   (~3-4h each). ABSL excluded (no per-scheme holdings published).
> - **Next**: continue 3.C with ICICI Pru (largest AUM after HDFC).

### Phase 3.B — User-provided gap-fill layer (single CSV)

**Why**: User offered to provide specific data for genuinely un-scrapable
schemes. This is the narrow exception to the "no manual override" rule —
it's gap-fill, not infrastructure.

**Scope**: one CSV per signal type. Loaded as the LAST step in each
ingestion stage, after all auto-scrape adapters run.

**Three CSVs (each optional)**:

1. `data/raw/managers/user_provided.csv` — `scheme_code, manager_name,
   manager_start_date, is_lead` (header row required, complete tuples
   only — no nulls).
2. `data/raw/index_constituents/user_provided.csv` (extends 2.2.B path)
   — already exists as manual CSVs; document that this is the explicit
   user-provided source.
3. `data/raw/stress_test/user_provided.csv` — already exists as 2.3.E path.

**Files to create/modify**:
- `src/mfs/ingest/managers/user_provided.py` (new) — CSV loader, validates
  scheme_code exists in scheme_master, writes to managers table with
  `source_amc='user'`. CLI: `mfs ingest managers --user-provided`.
- `tests/test_ingest_managers_user.py` (new) — load + validation tests.
- Update `.claude/commands/run-pipeline.md` to mention the gap-fill stage.

**Key design decisions**:
- Schema unchanged (strict NOT NULL). User must provide complete tuples
  for managers.
- `source_amc='user'` tags every user-provided row for audit visibility.
- Rejects rows with unknown scheme_codes (loud failure, not silent).
- If both auto AND user-provided rows exist for the same `(scheme,
  manager_name, start_date)`, user-provided wins on conflict.
- CSV is OPTIONAL — pipeline runs fine without it.

**Acceptance criteria**:
- CSV-less run: no-op, no error.
- CSV with valid rows: rows ingested, manager_tenure_years populated for
  those schemes on next compute.
- CSV with invalid scheme_code: row rejected with structured log, run
  continues.
- Schema validation rejects null start_date (consistent with the no-nullable
  principle).

**Effort**: ~3 hours.

---

### Phase 3.C — Per-AMC monthly portfolio Excel adapters (unblocks Active Share + AUM Impact)

> **2026-05-24 pivot**: The original framing assumed a centralized AMFI
> portal at `portal.amfiindia.com/spages/MFPortfolio.aspx`. That page
> returns 404 — AMFI does NOT centralize per-scheme portfolio data. SEBI
> requires each AMC to publish its own monthly portfolio disclosure on its
> own website (per the Feb 2021 circular). The Phase 2.2 brittleness audit
> already documented this: "ISINs appear only in separate per-scheme
> monthly portfolio Excel files (HDFC publishes those; we haven't
> validated for the other AMCs)." See line 528 of brittleness_audit.md.
>
> **New scope**: one adapter per AMC against their own monthly portfolio
> Excel/CSV. These DO carry ISINs (unlike factsheet PDFs). Build the
> abstract base + HDFC first (already validated to publish Excels), then
> ICICI Pru / SBI / Nippon. ABSL stays uncovered (no per-scheme holdings
> in their public artifacts). Effort: ~3-4h per AMC × 4 AMCs ≈ 12-16h.

**Why**: The dominant issue with Active Share and AUM Impact coverage is
that AMC factsheets don't print ISINs. Per-scheme monthly portfolio Excel
files DO include ISINs per holding.

**Scope**: replace (or supplement) factsheet-based holdings extraction with
per-AMC monthly portfolio Excel extraction. One adapter per AMC.

**Files to create/modify**:
- `src/mfs/ingest/holdings/` (new package) — separate from
  `ingest/managers/` because the source differs. Mirrors the registry
  pattern.
- `src/mfs/ingest/holdings/_base.py` — `HoldingsAdapter` ABC with
  `fetch(ym)` + `parse(path, ym)` returning `ParsedHoldingRecord` with
  ISIN (NOT NULL).
- `src/mfs/ingest/holdings/_registry.py` — same pattern as managers.
- `src/mfs/ingest/holdings/hdfc.py` (first concrete adapter), then
  `icici_pru.py`, `sbi.py`, `nippon.py`. ABSL excluded — see audit.
- CLI: `mfs ingest holdings --amc <slug>` or `--all`.
- Update `src/mfs/ingest/managers/_run.py` to NOT extract holdings from
  factsheets — those are now sourced from per-AMC portfolio Excels.

**Discovery work needed before each adapter**:
- Find the AMC's monthly portfolio disclosure page URL (each AMC's site).
- Document the URL pattern (per-month, per-scheme, or aggregated workbook).
- Capture one month of Excel for layout inspection (header rows, column
  names, ISIN column position, unit conventions).

**Key design decisions**:
- Per-scheme monthly portfolio Excels HAVE ISINs → `holdings_monthly.isin
  NOT NULL` for rows written via this stage. (Current schema allows nullable
  ISIN — left over from the factsheet pivot. Tighten via a NOT VALID
  constraint or runtime guard in the adapter rather than schema-wide change,
  so factsheet-sourced rows already on disk aren't invalidated.)
- Per-AMC adapters from the start; no "common AMFI format" base because
  there's no centralized AMFI source.
- Same scheme-name fuzzy matcher (`_scheme_match.py`) to resolve AMC
  scheme names to scheme_codes.

**Acceptance criteria**:
- Holdings rows ingested with ISIN populated.
- Active Share populates for all schemes whose AMC is covered.
- AUM Impact Cost populates more broadly (ISIN match-up rate goes from
  ~10% to ~90%+).
- Old factsheet-based holdings extraction can be removed once AMFI portal
  covers the same AMCs.

**Risk**: AMC anti-bot (Cloudflare on HDFC/ICICI/Nippon per the brittleness
audit). May need UA rotation, jitter, occasional Playwright fallback for
the worst offenders.

**Effort**: ~3-4h per AMC × 4 AMCs (HDFC, ICICI, SBI, Nippon) ≈ 12-16h.
ABSL stays excluded (no per-scheme holdings published).

---

### Phase 3.D — Long-tail AMC adapters (selective, post-AMFI-migration)

**Why**: 34 AMCs (~34% AUM) still uncovered. Strict policy makes most
unscrapable from factsheet, BUT if AMFI portal carries them with clean
ISIN-tagged Excel, that bypasses the per-AMC factsheet problem.

**Scope**: extend Phase 3.C's AMFI portal coverage to all remaining AMCs.
Manager-tenure data may still need per-AMC factsheet work, but holdings
become cheaper.

**Files**:
- Per-AMC adapter additions under `src/mfs/ingest/managers/<slug>.py` for
  manager tenure (each AMC needs URL + layout investigation).
- AMFI portal handles holdings.

**Effort**: ~1-2 hours per AMC for manager-tenure adapter, ~0.5 hours per
AMC for AMFI portal coverage if format is standard. 34 AMCs × ~2 hours =
~70 hours total. Realistically, prioritize next ~10 AMCs (Kotak excluded,
others where survey unblocks).

**Order**: Do AFTER 3.C. Don't repeat 2.4's mistake of doing per-AMC work
before establishing the better data source.

---

### Phase 3.E — Production hardening (auto-scrape only)

**Why**: With ~6-15 AMC adapters in production, the ongoing maintenance
ceiling becomes a real cost. Per the brittleness audit, expect 1-3 adapter
breaks per month at scale.

**Scope**: build out the AUTO-SCRAPE portions of the brittleness pass.
The manual-override layer is OFF the table per user constraint.

- **R0.2 LKG cache** — every successful fetch archived; auto-fallback on
  transient failure.
- **R0.3 central HTTP layer** — UA rotation, jitter, retry, optional
  Playwright path for Cloudflare-protected sources.
- **R0.4 schema validation gate** — pydantic models on every extracted
  record with value-range validators; bad rows quarantined not silently
  dropped.
- **R0.5 extraction quality scoring** — per-row confidence; low-confidence
  values contribute zero to composite_score (already conceptually wired
  via null-tolerance).
- **R0.6 adapter registry** — already exists; just polish.
- **R2.1 scheme_lineage table** — AMC merger / scheme rename tracking
  (manual entry for the lineage table is acceptable here because it's
  structural metadata, not metric data).

**Effort**: ~5-7 days of focused work. Treat as a phase, not a single PR.

---

### Phase 3.F — Transparency: `tenure_data_status` shortlist column

**Why**: Surfacing the un-ingested-AMC asymmetry to the user. Small change
but improves trust.

**Scope**: add `tenure_data_status` (and similar for other metrics) to
shortlist output columns. Values: `auto` / `not_available` / `user_provided`
(if 3.B ships).

**Files**:
- `src/mfs/rank/shortlist.py` — `OUTPUT_COLS` addition + derive logic.
- `tests/test_ranking.py` — assert column populates correctly per row.

**Effort**: ~1 hour.

---

## Recommended sequencing (3.A dropped)

```
3.F (Transparency column) — 1h, ship first as a quick warm-up post-compact
   │
3.B (User-provided gap-fill CSV) — 3h, unblocks long-tail manager coverage
   │
3.C (AMFI portal migration) — 10-15h, unblocks Active Share + AUM Impact
       │
       ├── 3.D (Long-tail adapters) — only after 3.C
       │
       └── 3.E (Production hardening) — after enough adapters that
                                         maintenance becomes painful
```

**Suggested next session**: ship 3.F + 3.B together. 3.F gets a quick
transparency win; 3.B opens the user-CSV gap-fill path you can backfill
into without changing pipeline architecture. Combined effort ~4 hours.

After that, 3.C (AMFI portal) is the highest-leverage next bet because it
unblocks two stuck metrics simultaneously and makes 3.D cheaper.

---

## Out of scope for this plan

- Replacing the existing manual-CSV paths for stress test / constituents.
  Those are grandfathered. Don't propose removing them; just don't add new
  manual paths.
- Multi-stint manager support (a manager rejoining the same scheme). Lost
  when the Phase 2.4 PK refactor was reverted. Add back if/when needed.
- Cross-source validation (Value Research / Morningstar) for data quality.
  Different project entirely.
- Removing Mirae's coverage limitations beyond ~12 schemes. Diminishing
  returns; better to invest in AMFI portal migration.

---

## How to resume after compaction

1. Read this entire file.
2. Read `docs/phase2/brittleness_audit.md` for context on prior decisions
   and per-AMC quirks.
3. Read `MEMORY.md` for user feedback that constrains the solution space:
   - `feedback_pipeline_fail_fast.md`
   - `feedback_no_manual_no_nullable.md`
4. Run `uv run pytest 2>&1 | tail -3` to confirm baseline (should be 235
   tests passing).
5. Confirm the user wants to start with **Phase 3.F** (transparency column) +
   **3.B** (user-provided gap-fill CSV) — that's the recommended next move
   now that 3.A is dropped.
6. Execute the chosen phase per its acceptance criteria.
