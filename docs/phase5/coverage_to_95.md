# Phase 5 — Holdings / Constituents / PTR coverage push

**Goal (2026-05-30):** raise coverage of portfolio **holdings**, benchmark
**constituent weights**, and **PTR** to >95% of the rankable universe, and
document any metric that can't reach 95% with the explicit reason.

**Universe (denominator):** `scheme_master` rows that are `is_active AND
plan_type='DIRECT' AND option_type='GROWTH' AND canonical_category IS NOT
NULL` = **664 funds** ("raw"). We also report an **addressable** universe
(**631**) that removes rows which can never carry a domestic Indian
holdings/PTR signal: legacy **Bonus Options** and **Segregated Portfolios**
(share-class duplicates the rank stage already dedupes) and **overseas /
FoF / commodity** funds (no Indian equity book).

> **Update 2026-06-06:** closed-ended & interval schemes are now excluded from
> the universe (they had leaked in via the "ELSS"/"Tax Saver" name rules —
> e.g. SBI/UTI/Sundaram/Bank of India "Long Term Advantage" series). This
> dropped the raw universe **682 → 664** (and addressable **649 → 631**).
> Re-measured coverage on the 664 universe: **Holdings 634/664 = 95.5%**,
> **PTR 577/664 = 86.9%**, **Constituents 21/22 tickers = 95.5%**. The
> 2026-05-31 table below is the original measurement on the then-682 universe.

Measure coverage anytime with `uv run python tools/coverage_report.py`.

---

## Results (data month 2026-04; measured 2026-05-31)

| Metric | Start | Raw (of 682) | Addressable (of 649) | Notes |
|---|---|---|---|---|
| **Holdings** | 7.6% | **94.6%** (645) | **97.5%** (633) | ✅ >95% addressable |
| **Constituent weights** | 22.7% (5/22) | **95.5%** (21/22 tickers) | — | ✅ >95% by ticker |
| **PTR** | 72.1% | **82.4%** (562) | 84.7% (550) | capped by quant + structural blockers |

- **Holdings** clears 95% on the addressable universe; the raw 94.6% gap is the
  irreducible set in the table below (dead closed-ended funds, 360 ONE
  misclassification, a handful of overseas FoFs).
- **Constituents** reaches 21/22 benchmark tickers; the sole miss is
  **NIFTY100 ESG** (no exact passive tracker). Fund-weighted this is ~99% (the
  ESG benchmark backs 9 of 664 funds).
- **PTR cannot reach 95%** on the raw universe — **quant alone is 24 funds
  (3.5%)** and structurally omits PTR, plus ~3% of brand-new-AMC / dead /
  hybrid-omitted / download-walled funds. Excluding quant, PTR is **87.8%**;
  the residual is documented below.

Also fixed a data-quality bug exposed during the build: sibling-fund name
collisions (e.g. "Mid Cap" vs "Large & Mid Cap") that fuzzy-matched two funds
onto one `scheme_code`, merging portfolios and inflating ~36 ranked funds'
equity weight-sums to 120–240%. The orchestrator now keeps only the
highest-scoring printed name per `scheme_code` and does a delete-then-insert
per `(source_amc, as_of_month)`, so a run is idempotent. Post-fix: **0 ranked
funds** have a per-month equity weight-sum > 105%.

---

## What was built

### 1. Generic SEBI-format holdings parser — `src/mfs/ingest/holdings/_generic.py`
SEBI mandates every AMC publish a monthly portfolio Excel with an ISIN
column and a `% to NAV` weight column, but **column positions and the weight
unit (percent vs fraction) vary by AMC**. `parse_sebi_excel` auto-detects the
header row, the ISIN / name / weight columns, and the weight unit (a complete
portfolio sums to ~100 percent or ~1.0 fraction), so one parser serves the
long tail. Validated to byte-match the three pre-existing bespoke adapters:
**HDFC 92/92, SBI 87/87, Nippon 25/25**. `GenericHoldingsAdapter` lets a new
AMC adapter be just a `discover_scheme_urls` method.

### 2. ~45 new per-AMC holdings adapters
There is **no uniform AMC-direct or AMFI portfolio source** — AMFI's
portfolio-disclosure page is a React SPA whose `mf=` links are NAV RSS feeds,
not portfolios. Discovery is irreducibly per-AMC: static-HTML `.xlsx` links,
JSON/AJAX CMS APIs (Axis, ICICI, Mirae, PGIM, Bandhan, Motilal, …), or a
single consolidated workbook (Nippon, ABSL). Each adapter reverse-engineers
its AMC's source and reuses the shared parser.

### 3. ~12 new factsheet PTR adapters
For AMCs that previously had no factsheet adapter. PTR is normalized to a
fraction (1.27 = 127%); each adapter documents whether the factsheet prints
percent or fraction.

### 4. Constituent-weight derivation — `tools/derive_constituents.py`
NSE/niftyindices publishes index *membership* but **no machine-readable
constituent weights**. We derive weights from a passive index tracker's
holdings (a tracker's portfolio ≈ the index). The tool auto-selects the
best-ingested tracker per index with an index-size sanity check, derives the
three synthetic hybrid benchmarks from the **NIFTY 50 equity sleeve** (the
standard equity benchmark for a hybrid's equity book), and falls back to an
exact-index **ETF's** monthly portfolio for sector indices that have no
Direct+Growth index fund.

### 5. Scheme-match fix — `_scheme_match.canonicalize`
Strips `(erstwhile / formerly X)` rename parentheticals. AMFI keeps the old
name as a parenthetical on renamed schemes (e.g. "ICICI Prudential Large Cap
Fund (erstwhile Bluechip Fund)"); the extra tokens lengthened the canonical
key so the Levenshtein tie-break picked a wrong shorter sibling ("Large &
Mid Cap Fund"). Stripping them fixes the mis-match.

---

## Residual blockers (why some funds can't reach the source)

| Bucket | Metric affected | Reason |
|---|---|---|
| ~~**quant Mutual Fund** (24 funds)~~ → **RESOLVED** (annual report) | PTR | quant omits PTR from its factsheet, but SEBI mandates it as a per-scheme footnote in the **abridged annual report** (Circ. MFD/CIR/14/18337/2002). `QuantAdapter` now parses that report (see "quant PTR" note below). Recovers ~19 quant schemes. |
| **Brand-new AMCs** (the_wealth_company, unifi, abakkus, jio_blackrock; ~12 funds) | PTR | Funds are <1 yr old with no completed FY; PTR is a trailing-12-month metric that is "not computed" until a fund completes one year — so neither factsheet nor annual report carries it yet. |
| **Dead closed-ended funds** (UTI Long Term Advantage Series III–VI; NAV frozen 2021) | Holdings + PTR | Matured closed-ended schemes still flagged `is_active`; not in any current disclosure. |
| **360 ONE funds mis-assigned** to `uti`/`whiteoak_capital` amc_codes (~4) | Holdings + PTR | An upstream `scheme_master` amc_code-derivation quirk: 360 ONE (ex-IIFL) is a distinct AMC; its funds aren't in UTI/WhiteOak disclosures. Needs a `scheme_master` fix + a 360 ONE adapter. |
| **NIFTY100 ESG TRI** (9 funds) | Constituents | No exact passive tracker — only "Nifty 100 ESG **Sector Leaders**", a distinct NSE index. niftyindices publishes no machine-readable weights for the plain index. |
| **Download-walled factsheets** (trust, jio_blackrock) | PTR | Factsheet PDFs front a WAF / auth-gated CDN; holdings Excels were reachable, factsheets were not, without a headless browser. |
| **Axis April portfolio** | Holdings | Axis hadn't published the 2026-04 monthly portfolio at run time (publish lag); covered using the latest available month (2026-03). |

---

## quant PTR — sourced from the abridged annual report

quant deliberately omits PTR from its monthly factsheet (legal — SEBI mandates
PTR only in the half-yearly portfolio and the annual report, not the
factsheet). `src/mfs/ingest/managers/quant.py` therefore parses the per-scheme
"Portfolio turnover ratio" footnote from quant's **abridged scheme-wise annual
report** (`/Admin/disclouser/Annual-Report__quant-Mutual-Fund_Financial-Year-<FY>.pdf`).
The report's "Perspective Historical Per Unit Statistics" table is one column
per scheme (current FY + previous FY); we read the clean current-FY value
(printed in "times" = our fraction convention) and stamp `as_of_month` at the
report's FY-end (read from the table header), via the new optional
`ParsedPtrRecord.as_of_month`. `fetch()` probes FY URLs newest→oldest and uses
the latest available.

**Freshness caveat.** The report is annual and published ~6 months after FY-end,
so quant's PTR is older than other AMCs' monthly factsheet PTR (as of mid-2026
the latest pattern-discoverable report is FY2023-24 → stamped 2024-03; FY2024-25
sits behind the site's WebForms postback at a non-pattern URL). This is
acceptable: PTR feeds a **soft penalty** (`rank/score.py`), the compute layer
takes the latest PTR per scheme regardless of age, and the PTR freshness gate is
disabled (`freshness.max_ptr_lag_days: null`). **If PTR freshness is ever
enabled, set the threshold ≥ ~450 days or exempt `source_amc='quant'`** so the
annual figure isn't rejected as stale. A fresher upgrade path is the half-yearly
portfolio statement (carries the same footnote, semi-annual).

## Hybrid benchmark methodology — equity sleeve only (by design)

The three hybrid benchmarks (`nifty_50_hybrid_50_50_tri`,
`nifty_50_hybrid_65_35_tri`, `nifty_equity_savings_tri`) are modeled by their
**NIFTY 50 equity sleeve** only. This is deliberate, not a coverage gap:
Active Share (the sole consumer of benchmark constituents) is an
equity-portfolio measure — it compares the fund's equity book against the
equity benchmark, both renormalized to 100%. The debt sleeve is excluded
because (a) the NIFTY Composite Debt Index publishes no machine-readable
per-bond weights and no tracker fund exists to mine, so it is only knowable as
a single aggregate weight; and (b) injecting an aggregate debt row would
*spuriously inflate* every hybrid's Active Share, since the fund's own debt
holdings are already dropped by the equity-only filter and so would not cancel.
The benchmark's fixed equity/debt split (e.g. 65:35) is recorded as metadata in
the `composite_recipe` column of `configs/benchmarks.csv` for transparency.

---

## How to extend / re-run

- Holdings: `uv run mfs ingest holdings --amc <slug> --ym 2026-04` (or `--list`).
- PTR: `uv run mfs ingest managers --amc <slug> --ym 2026-04`.
- Constituents: `uv run python tools/derive_constituents.py --ym 2026-04` then
  `uv run mfs ingest constituents`.
- Coverage: `uv run python tools/coverage_report.py`.
