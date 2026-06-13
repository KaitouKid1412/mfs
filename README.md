# mfs — Indian Mutual Fund Evaluation Pipeline

Ranks every active, open-ended Indian mutual fund (Direct + Growth plans)
within its canonical SEBI category using rolling risk-adjusted NAV metrics
plus portfolio-disclosure signals (PTR, style drift, AUM impact cost,
overlap). Postgres-backed, fail-fast, with per-run provenance. Output is a
staged shortlist per category plus a plain-language report.

Accuracy note: this README describes the system as it is, including its
known weaknesses (see [Known limitations](#known-limitations)). The full
audit it answers to lives at `docs/audit/2026-06-10_pipeline_audit.md`.

## Project invariants

These override convenience everywhere in the code:

1. **Fail-fast ingestion** — the pipeline halts on stale data or scrape
   failures; it never proceeds with partial inputs.
2. **No manual data entry, no nullable strict fields** — auto-scrape clean
   tuples or skip the AMC entirely. Half-data is worse than no data.
3. **AUM is AMFI-AAUM-only** — `scheme_aum_monthly` CHECK-constrains
   `source_amc='amfi_aaum'`; the factsheet-AUM path was deleted. Do not add
   `parse_aum` to adapters.
4. **Incremental by default, `--full` to force** — one flag meaning
   re-download AND re-parse AND re-write.

## Architecture

```
ingest adapters ──> coverage gates ──> Postgres ──> compute ──> rank-deep ──> artifacts
 (AMFI NAV, NSE      (Gate A blocking,  (schema:     (phase 1     (stage 1/2/3)  (CSV/parquet,
  TRI, RBI T-bill,    Gate B advisory,   src/mfs/db/  NAV metrics,                manifest.json,
  ~46 holdings +      freshness gates)   schema.sql)  phase 2                     REPORT.md,
  ~29 factsheet                                       disclosure                  rank_history)
  adapters, NSE                                       metrics)
  bhavcopy, AMFI AAUM)
```

Everything stateful lives in Postgres (`mfs db init` creates/migrates the
schema idempotently): `nav_daily` (~30M rows), `benchmark_daily` (TRI),
`risk_free_daily`, `scheme_master` (+ `scheme_master_history`),
`holdings_monthly`, `portfolio_turnover_monthly`, `scheme_aum_monthly`,
`index_constituents_monthly`, `stock_adv_daily`, `computed_metrics`,
`rank_history`. Raw downloads (factsheet PDFs, holdings Excels, bhavcopies)
are cached under `data/raw/` — much of it unrecoverable upstream, hence the
[backup policy](#ops). Parquet appears only in output artifacts; the
parquet-era data path is gone.

## The pipeline (`mfs pipeline`)

`mfs pipeline [--as-of YYYY-MM-DD] [--full] [--skip-phase2]
[--allow-fallback]` runs, under a Postgres advisory lock (one pipeline at a
time):

1. **ingest navs** (incremental) — AMFI daily NAVs; zero/negative NAVs
   rejected at ingest (DB CHECK backs it).
2. **ingest benchmarks** — NSE TRI series per `configs/benchmarks.csv`
   (incremental tail re-fetch; `--full` re-pulls history), then
   **synthesize hybrid TRIs** for hybrid categories from equity TRI +
   91-day T-bill components (see limitations — this biases hybrid alpha).
3. **ingest tbill** — RBI/FBIL 91-day T-bill auctions → daily risk-free
   series. `--allow-fallback` substitutes a synthetic 6.5% flat series and
   is for debugging only.
4. **build scheme-master** — today's AMFI snapshot → plan/option parsing,
   canonical categories, benchmark mapping. Diff-sync: departed schemes are
   kept with `is_active=false` + `departed_at`, never deleted (survivorship
   containment), and a post-sync snapshot lands in `scheme_master_history`.
   A **category-relabel guard** halts the build if a canonical category
   that currently holds ≥5 active schemes would drop to 0 matches (an
   AMFI/SEBI relabel must not silently evaporate a category).
5. **Coverage Gate A (BLOCKING)** — NAV / benchmarks / risk-free /
   scheme_master contracts: emptiness, staleness, history depth, entity
   coverage, interior NAV-calendar gaps. Any breach halts before the
   expensive Phase-2 ingest.
6. **Phase-2 ingest** (skipped by `--skip-phase2`): AMFI quarterly AAUM;
   factsheet PDFs per AMC (PTR); per-scheme monthly portfolio Excels
   (holdings with ISINs); NSE bhavcopy (stock ADV). Per-AMC failures are
   isolated and reported; all-AMCs-failed is systemic and halts.
7. **derive constituents** — index constituent weights are derived
   in-pipeline from index-tracker fund holdings for the latest month (D9),
   then the CSV ingest upserts them (plus any operator backfills).
8. **Coverage Gate B (ADVISORY)** — holdings / PTR / AAUM / constituents /
   stock-ADV per-fund coverage: gaps are reported loudly and the affected
   funds are flagged/penalized downstream, but the run continues.
9. **freshness check (BLOCKING)** — whole-source staleness thresholds from
   `configs/pipeline.yaml` (`freshness:`): NAV/benchmarks ≤5 business days,
   T-bill ≤14 days, holdings/PTR/constituents ≤75 days, stock-ADV ≤10 days,
   AAUM ≤150 days. The global MAX(date) of a table going dark for more than
   one publication cycle halts the run; per-fund gaps stay advisory in
   Gate B.
10. **compute phase1** — NAV metrics for every eligible scheme.
11. **rank-deep** — the three-stage ranker (below), which runs Phase-2
    compute itself, restricted to the Stage-1 candidate pool.

The coverage summary is printed on every run — success, advisory gaps, or a
Gate A halt. The pipeline halts at the first required stage that fails; no
partial or stale data ever reaches compute or ranking.

**When the pipeline halts**, that is the system working as designed. The
two halts that need operator judgment rather than a re-run:
- *Category-relabel guard* (`PipelineError` naming the category + the
  unmatched raw AMFI strings): extend `CATEGORY_RULES` in
  `src/mfs/master/scheme_master.py` for the relabel (or confirm a genuine
  category retirement) and re-run `mfs build scheme-master`.
- *Freshness/Gate A breach*: fix the ingestion source; do not bypass the
  gate.

## Methodology

### Phase 1 metrics (NAV-only, per scheme vs mapped benchmark TRI)

Rolling 3y/5y windows, weekly step, one trailing-epoch convention across all
metrics: rolling return distribution (median + p25 CAGR), Jensen's alpha
(log-basis regression vs benchmark, T-bill risk-free), beta, R², Sortino
(T-bill MAR), up/down capture + capture efficiency, information ratio, plus
display-only max-drawdown depth/recovery columns.

**Signed alpha (locked D1):** `alpha_3y_annualized` is the median over ALL
rolling windows — negative alpha is stored and ranked as negative; there is
no t-stat censoring. `alpha_confidence` (share of windows with |t| ≥ 1) is a
display column, never a filter.

### The three-stage ranker (`mfs rank-deep`)

- **Stage 1** — full universe: relaxed hard filters (drop only
  catastrophically broken funds: capture efficiency < 0.5, IR < −1, R²/beta
  bands, stale NAV, insufficient history), then the **core-metric gate
  (locked D2)**: funds missing any core Stage-1 metric are hard-dropped into
  a visible `stage1/excluded.csv` with a contract-vocabulary
  `exclusion_reason` — nulls never score as category-average. Legacy
  bonus-option duplicates are deduped before z-scoring. Z-scores within
  category, composite from `composite_weights_stage1` (alpha 0.25, 3y/5y
  return medians 0.15 each, p25s 0.10 each, Sortino/IR/capture 0.10/0.10/0.05).
- **Stage 2** — top-20 per category: Phase-2 compute (active share vs
  derived constituents, style drift, PTR, AUM impact cost in
  days-to-liquidate vs stock ADV) runs on just this pool. Stage-1's
  full-universe z-scores are carried (never re-z-scored in the small pool);
  Phase-2 metrics are z-scored within the pool. **Missing disclosures
  (locked D3)** are soft-neutral: weight renormalization + a calibrated
  fixed penalty (≈ median observed PTR penalty) + a `partial_disclosure`
  flag — missing scores like average-bad, never better than disclosed-bad.
  Soft penalties: PTR ramp above 150% turnover (arbitrage-mechanics
  categories exempt), log-scale AUM-impact ramp, style-drift negative
  weight.
- **Stage 3** — pairwise holdings overlap on survivors. **Locked D4: keep
  both funds and flag the breach** (overlap % + counterpart) — no
  cross-category drops. Breaches land in `stage3/overlap_breaches.csv`;
  a full `overlap_matrix.csv` supports subset buyers.

Context columns ride along every output row: per-category benchmark-fit
confidence (median R² < 0.80 ⇒ alpha is low-confidence in that category),
`benchmark_is_synthetic` for hybrid categories, and the passive-alternative
verdict (`passive_alternative.csv`: benchmark TRI vs category median fund,
with the pick's margin over the investable index).

### Validation

`tools/backtest_ic.py` retro-backtests the Stage-1 composite quarterly from
2016 (Spearman IC vs forward category-relative returns, weight sensitivity,
pre-registered verdict thresholds). **Survivorship-biased by construction**
(see limitations) — every IC it reports is an upper bound.
`tools/cross_validate.py` + `docs/ops/spot_check.md` reconcile our numbers
against external sources after every recompute.

## Output artifacts

`data/output/shortlist/<as_of>/`:

| artifact | what it is |
|---|---|
| `stage1/<category>.csv` (+`.parquet`), `stage1/mf_report.csv` | full Stage-1 ranking per category; top-5 consolidated |
| `stage1/excluded.csv` | D2 hard-drops with `exclusion_reason` (`INSUFFICIENT_HISTORY \| MISSING_CORE_METRIC:<name> \| STALE_NAV \| FILTER:<name>`) |
| `stage2/…` | pool re-rank, coverage report, vestigial `dropped.csv` (structurally empty post-D3) |
| `stage3/…` | final picks with overlap flags, `overlap_breaches.csv`, `overlap_pairs.csv`, `overlap_matrix.csv` |
| `passive_alternative.csv` | per-category index-vs-funds verdict (D7) |
| `manifest.json` | run provenance: git SHA + dirty flag, config SHA-256, `pipeline_version`, package version, per-table DB row counts, stage survivor counts |
| `REPORT.md` | plain-language investor report rendered by `mfs report` over the run dir (report tooling is being actively extended) |

Point-in-time history persists in Postgres: `rank_history` (every stage
outcome per run, including exclusions with reasons) and
`scheme_master_history` (universe snapshots). `mfs shortlist diff` gives
run-to-run ENTERED/EXITED/RANK-MOVED monitoring from the artifacts alone.

## CLI map

| command | purpose |
|---|---|
| `mfs pipeline` | end-to-end run (stages above) |
| `mfs ingest navs / benchmarks / tbill / amfi-aum / bhavcopy / constituents / managers / holdings` | individual ingest stages |
| `mfs ingest ter` | manual-CSV TER path — deprecated (violates the no-manual-entry invariant); slated for removal/replacement pending the TER endpoint decision (`docs/audit/ter_endpoint_spike_2026-06.md`) |
| `mfs build scheme-master` | rebuild the scheme dimension (diff-sync + relabel guard) |
| `mfs compute phase1 / phase2 / metrics` | metric computation into `computed_metrics` |
| `mfs rank` | Stage 1 only |
| `mfs rank-deep` | Stage 1 + 2 + 3 (+ Phase-2 compute on the pool) |
| `mfs report` | render `REPORT.md` for a shortlist run dir |
| `mfs shortlist diff` | run-to-run diff, pure file comparison |
| `mfs status` / `mfs db status` | row counts + date bounds per table |
| `mfs db init` | create DB / apply schema (idempotent) |
| `mfs db migrate` / `mfs db verify` | legacy parquet→Postgres one-shot tooling (retirement pending confirmation the parquet store is dead) |
| `mfs db coverage` / `mfs missing-data` / `mfs validate` | diagnostics: trading-day coverage, gap inventory, TRI sanity |
| `mfs audit-scheme` | one-off metric computation for a (scheme, benchmark) pair |

Note: standalone commands (`mfs rank-deep`, `mfs compute …`) do NOT take the
pipeline advisory lock; transactional writes bound the damage, but don't run
them concurrently with `mfs pipeline`.

## Development

```bash
uv sync                                  # install (uv.lock pinned)
uv run pytest -q                         # full suite (needs data/raw for adapter value-pins)
uv run pytest -m 'not local_data' -q     # the CI subset — no data/raw, no Postgres, no network
uv run mypy                              # typed baseline: compute + rank (advisory until 0 errors)
uv run ruff check src tests              # advisory; backlog being ratcheted down
```

Tests pinned to gitignored `data/raw` artifacts are auto-marked
`local_data` (tests/conftest.py); committed PDF page-extracts keep the real
parse paths covered in CI. GitHub Actions (`.github/workflows/ci.yml`) runs
ruff + mypy (advisory), the `not local_data` suite (blocking), a pinned
deselected-count tripwire (a new local-only test fails the build until
consciously pinned), and pip-audit (blocking).

Config single sources of truth: `configs/pipeline.yaml` (weights, filters,
freshness thresholds, soft penalties, `pipeline_version` — required, no code
default), `configs/category_thresholds.yaml`, `configs/benchmarks.csv`.

## Ops

- **Backups**: `scripts/backup.sh` (pg_dump -Fc with 7-daily/4-weekly
  rotation + restic-or-tar of `data/raw`) and the monthly
  `scripts/backup.sh restore-check` drill — runbook with cron/launchd
  recipes at `docs/ops/backups.md`. Scheduling is deliberately an operator
  decision.
- **External spot-check**: `docs/ops/spot_check.md` — mandatory after every
  recompute, before a shortlist is publishable; includes the recurring
  Kotak holdings parity check.
- **Scraping provenance**: `docs/ops/scraping_provenance.md` — every
  non-public access path (extracted keys, browser mimicry, test hosts,
  server-action ids) with rotation blast radius, ToS judgment, and an
  accept/replace/drop decision awaiting user sign-off.

## Known limitations

- **Survivorship before 2026-06**: `scheme_master_history` /
  `rank_history` only exist from June 2026; funds that died/merged earlier
  are absent from NAV history entirely. Backtest ICs are upper bounds; the
  diff-sync containment only protects the future.
- **Synthetic hybrid benchmarks**: hybrid-category TRIs compound a 91-day
  T-bill debt sleeve, systematically easier to beat than the real composite
  debt indices (~35–140bp/yr one-sided at 35–70% debt weight). Hybrid
  alpha/beat-rates are overstated; every affected row carries
  `benchmark_is_synthetic=true`.
- **TER is not a signal yet**: no clean automated source survived the
  endpoint spike (`docs/audit/ter_endpoint_spike_2026-06.md`); the manual
  path violates the no-manual-entry invariant and is deprecated. Expense
  drag is currently unmodeled.
- **active_share is dormant** until ≥3 matched (holdings, constituents)
  months accrue (~2026-07); rank-deep prints an activation banner. Until
  then Stage 2 renormalizes around it.
- **Holdings adapter fixture coverage is 3/46**: most adapters are guarded
  by runtime gates (weight-sum, statement-date, shrinkage) rather than
  committed fixtures — a silent parse regression is unlikely but not
  impossible.
- **Kotak holdings provenance**: served from a `prodtest` host with no SLA
  (see `docs/ops/scraping_provenance.md`); mitigated by a recurring manual
  parity check.
- **Static tax figures** in the report (STCG/LTCG/ELSS lock-in) are baked
  as of FY2025-26 and go stale with the next Finance Act.
- Full audit + remediation state: `docs/audit/2026-06-10_pipeline_audit.md`,
  `docs/phase6/`.
