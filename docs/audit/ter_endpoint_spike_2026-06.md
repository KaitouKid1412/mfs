# TER ingestion — endpoint spike & decision memo (D8-spike, 2026-06-13)

**Status: spike only — no scraper built, stub NOT deleted.**
**Recommendation: BUILD the real AMFI TER scraper (D8 full scope) after a
~30-minute operator-run network verification of the endpoint below.** The
fallback (delete `amfi_ter.py` + the manual path) applies only if that
verification finds the disclosure JS-walled or otherwise inaccessible.

## Why TER matters (unchanged from the audit)

* TER is the best-evidenced *forward* predictor of fund performance and the
  missing cost side of D7's active-vs-passive framing (`passive_alternative.csv`
  compares gross-of-our-knowledge fund returns to a costless index — a TER
  column closes that gap narratively).
* Scope stays display + stage-2 tiebreaker ONLY (composite delta < 0.005 →
  lower TER wins). NO score weight — weight changes are gated on the D3
  backtest.

## Current state (verified in-tree 2026-06-13)

* `src/mfs/ingest/amfi_ter.py` is a **manual-CSV stub**: reads
  `data/raw/amfi_ter/manual/*.csv` and writes parquet partitions via
  `paths.ter_daily_dataset()`. This **violates the no-manual-entry
  invariant** (memory: "Auto-scrape clean tuples or skip the AMC").
* `data/raw/amfi_ter/` does not exist — the stub has never ingested a row.
* No `scheme_ter_monthly` table exists in `db/schema.sql`; nothing in
  rank/stage2 reads TER. The blast radius of either path (build or delete)
  is therefore small and contained.

## Candidate endpoint (requires network verification — none available here)

AMFI publishes a monthly **"Total Expense Ratio of Mutual Fund Schemes"**
disclosure page with a month/year + AMC(mutual fund) form and historical
months back to ~2019 (SEBI's TER-disclosure circular effective date):

* Page: `https://www.amfiindia.com/ter-of-mf-schemes`
* The form is an ASP/AJAX month-year-AMC selector; the table it renders is
  scheme-name keyed with per-plan TER columns (Regular / Direct), and AMFI
  pages of this family (NAV, AAUM) historically expose the backing data via
  a POST/XHR module endpoint rather than requiring JS execution. The AAUM
  ingester (`ingest/amfi_aum.py`) already proves the pattern: AMFI's
  quarterly AAUM "form" is backed by a machine-readable endpoint we scrape
  cleanly today.

**Operator verification (~30 min, browser devtools):**
1. Open the TER page, pick a month/year/AMC, click submit, capture the XHR
   (expect `POST https://www.amfiindia.com/modules/...` with month/year/AMC
   form fields, returning HTML table or JSON).
2. Replay the captured request with `curl` (no cookies / fresh session) —
   if it returns the table without JS, the endpoint qualifies.
3. Check one historical month (e.g. 2024-01) and one current month to
   confirm the archive depth and that Direct-plan TER is a column, not a
   separate page.
4. Record: exact URL, method, form fields, response shape, and whether the
   scheme rows carry any code (AMFI code rarely present — assume name-only).

## What the build needs (D8 full scope, deferred — sized M/L)

1. **Fetcher**: per (month, AMC) request loop, content-validated (B1
   conventions), cached under `data/raw/amfi_ter/<YYYY-MM>/<amc>.html`.
2. **Parser**: scheme-name → Direct-plan TER (%) tuples. Clean tuples or
   skip the scheme — never null-fill (no-half-data invariant).
3. **Scheme matching**: the table prints names, not AMFI codes — resolve via
   the managers `_scheme_match` path WITH its collision guards (B5: unique
   best match with margin; subset-name poaching rejected).
4. **Storage**: new table `scheme_ter_monthly (scheme_code TEXT, as_of_month
   DATE, ter_direct_pct DOUBLE PRECISION NOT NULL, source TEXT NOT NULL
   DEFAULT 'amfi', computed_at TIMESTAMP NOT NULL, PRIMARY KEY (scheme_code,
   as_of_month))` in `db/schema.sql` (+ `mfs db init` migration).
5. **Gate B ADVISORY contract** in `coverage.py` CONTRACTS (monthly cadence,
   rankable entity set) + registry test update; pipeline stage after
   amfi-aaum (`cli.py` ~1136-1139), `required=False` initially.
6. **Rank wiring**: join latest `ter_direct_pct` into stage2/stage3
   `mf_report` as display column `ter_pct`; stage-2 ordering breaks
   composite ties (delta < 0.005) by lower TER in `rank/stage2.py`.
7. **Tests**: parser fixture against a saved AMFI response excerpt
   (managers-test convention), collision-guard test, tie-break ordering test
   in `tests/test_stage2.py`, coverage-registry test.
8. **Delete the stub** (`ingest_from_manual_csvs`, `paths.ter_daily_dataset`,
   the `mfs ingest ter` CLI wiring at `cli.py:122` area) in the same PR —
   the manual path must not survive the real one landing.

Acceptance for the build (from the spec): >= 80% of the 665-fund universe
covered for the latest disclosed month; stage3 mf_report shows `ter_pct` for
>= 80% of picks.

## Fallback (pre-agreed)

If step 2 of the verification fails (endpoint requires JS execution /
session tokens that can't be replayed): **DELETE** `amfi_ter.py`, the CLI
command, and `paths.ter_daily_dataset` — never keep the manual path. Grep
acceptance: `grep -r amfi_ter src/` returns nothing.

## Why not built in this batch

This batch is sandboxed without network access; building a scraper against
an *assumed* request shape would ship unverifiable code (the exact failure
mode B1/B8 exist to prevent). The decision memo + verification script above
make the build a clean, single-session task once the endpoint is confirmed.
