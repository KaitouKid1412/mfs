# mfs — Indian Mutual Fund Evaluation Pipeline

Ranks every active Indian mutual fund (Direct Growth plans) within its AMFI equity category
using rolling risk-adjusted metrics over a 5+ year NAV history. Output is a top-N shortlist
per category as Parquet + CSV.

## v1 scope (NAV-only filters)

| Metric | What it measures |
|---|---|
| Rolling 3Y / 5Y returns | Median + 25th-percentile annualized CAGR across overlapping windows |
| Jensen's α (3Y) | Excess return after controlling for benchmark exposure |
| β, R² (3Y) | Benchmark loading and explained-variance fit |
| Sortino (3Y) | Excess return per unit of downside deviation vs T-bill MAR |
| Capture up / down / efficiency | Asymmetry of fund response to benchmark up vs down days |
| Information Ratio (3Y) | Active return per unit of tracking-error |

Active Share, ISIN overlap, AUM drag, PTR, stress-test liquidity, manager tenure → v2.

## Setup (one-time)

```bash
uv sync
```
Resolves and installs all Python dependencies declared in `pyproject.toml` into a project-local
`.venv/`. Pins are captured in `uv.lock`. Run again whenever dependencies change.

## End-to-end pipeline

The flow is **ingest → build → compute → rank**. Each stage writes a Parquet partition that
the next stage reads, so any stage is independently re-runnable.

### 1. Ingest AMFI NAVs

```bash
uv run mfs ingest navs --backfill         # first time, ~5-15 min
uv run mfs ingest navs                    # daily incremental, ~5 sec
```
- `--backfill` fetches the full historical NAV series from AMFI's bulk endpoint
  (`portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx`) in 90-day windows, starting from
  `configs/pipeline.yaml: ingest.amfi_nav.backfill_start`. Stored at
  `data/curated/nav_daily/year=YYYY/data.parquet`. Idempotent — dedupes on
  `(scheme_code, nav_date)` so re-running is safe.
- Without flags, fetches just today's `NAVAll.txt` and appends to the current year's partition.

### 2. Ingest NSE TRI benchmarks

```bash
uv run mfs ingest benchmarks              # ~5-10 min for 16y × 9 equity tickers
```
POSTs to `niftyindices.com/Backpage.aspx/getTotalReturnIndexString` in 360-day chunks
(endpoint's per-request cap), parses the wrapped JSON response, and writes one Parquet
partition per ticker at `data/curated/benchmark_daily/ticker=<slug>/data.parquet`.
Pre-computes daily log returns alongside the close.

**Hybrid/Equity Savings indices** (3 of 12 categories) aren't exposed via this endpoint
— supply them as CSVs at `data/raw/benchmarks/manual/<slug>.csv` with columns
`date,close` (date in `DD-Mon-YYYY` or `YYYY-MM-DD`). The loader auto-detects column
variants like `Total Returns Index`. Required slugs for the hybrid indices:

| Category | Slug |
|---|---|
| Aggressive Hybrid | `nifty_50_hybrid_65_35_tri.csv` |
| Balanced Advantage | `nifty_50_hybrid_50_50_tri.csv` |
| Equity Savings | `nifty_equity_savings_tri.csv` |

### 3. Ingest risk-free rate

```bash
uv run mfs ingest tbill --backfill        # uses a 6.5% flat fallback in v1
```
Writes a daily 91-day T-bill series at `data/curated/risk_free_daily/risk_free.parquet`
with columns `(date, rate_annual, rate_daily)`. Used as MAR in Sortino and as r_f in the
Jensen's α regression.

**v1 caveat**: ships with a constant 6.5% fallback. Rate-level constants don't bias α
or β (both sides of the regression shift by the same constant) but Sortino and absolute
alpha for 2020–2022 windows will be off. Drop a real CSV at
`data/raw/fbil_tbill/manual/tbill.csv` with columns `date, rate_annual_pct` to replace.

### 4. Build the scheme master

```bash
uv run mfs build scheme-master            # ~5 sec, ~14k rows
```
Pulls today's AMFI NAVAll snapshot and derives the canonical scheme dimension:
parses scheme names → `plan_type` (DIRECT/REGULAR) + `option_type` (GROWTH/IDCW),
slugifies AMC names into `amc_code`, maps AMFI category strings to `canonical_category`,
joins `configs/benchmarks.csv` for `benchmark_ticker`, and computes `inception_date` /
`last_seen_date` from the NAV history. Output: `data/curated/scheme_master/scheme_master.parquet`.

### 5. Compute metrics

```bash
uv run mfs compute metrics                # ~5-10 min for ~400 eligible funds
```
For every Direct+Growth scheme with a benchmark mapping, aligns NAV onto the master
trading calendar, then computes every metric in `src/mfs/compute/` (rolling returns,
Jensen's α, Sortino, capture ratios, Information Ratio, R², β) via 3Y/5Y weekly-step
windows. Writes one row per scheme to
`data/metrics/computed_metrics/as_of_date=YYYY-MM-DD/data.parquet`. The partition is
delete-then-write, so re-running for the same `--as-of` is idempotent.

### 6. Rank and write shortlists

```bash
uv run mfs rank --top-n 10                # ~1 sec, writes one file per category
```
Loads the latest computed_metrics partition, applies hard filters (capture efficiency
> 1.15, IR > 0.5, R² in [0.70, 0.90], per-category beta band), z-scores remaining
funds within their `canonical_category`, computes the weighted composite score from
`pipeline.yaml: composite_weights`, and writes the top-N to
`data/output/shortlist/<as_of_date>/<category>.{parquet,csv}`.

Flags:
- `--category "Flexi Cap"` — restrict output to one category
- `--as-of 2026-05-15` — re-rank a historical computed_metrics partition
- `--skip-filters` — debug-only; bypass hard filters (useful when benchmarks are stubbed)

## Inspection & diagnostics

```bash
uv run mfs status                         # list which partitions exist on disk
```
Shows present years for `nav_daily`, present tickers for `benchmark_daily`, whether
`scheme_master` and `risk_free_daily` are built, and which `as_of_date` partitions
exist under `computed_metrics`.

```bash
uv run mfs validate                       # data-quality report
```
Sanity-checks: asserts NIFTY 50 TRI CAGR since 2010 > 12% (guards against accidentally
ingesting Price Return), reports total NAV rows × scheme count.

```bash
uv run pytest                             # ~1 sec, 16 tests
```
Golden math tests for α/β/R²/capture/Sortino/IR + AMFI parser + manual-CSV loader +
rank scoring.

## Orchestrated targets (Makefile)

```bash
make backfill     # ingest navs --backfill + benchmarks + tbill + scheme-master
make daily        # incremental: navs + benchmarks + tbill + scheme-master + compute + rank
make rank         # rank only — fast iteration on weights without recomputing
make qa           # validate
make test         # pytest
```

## Methodology

`/Users/suryavamseeayyagari/.claude/plans/kind-drifting-ullman.md`. Pipeline weights and
thresholds live in `configs/pipeline.yaml`; per-category beta bands in
`configs/category_thresholds.yaml`; canonical category → benchmark mapping in
`configs/benchmarks.csv`.

## Data sources

| Source | Purpose | Endpoint |
|---|---|---|
| AMFI bulk history | All scheme NAVs since 2008 | `portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx` |
| AMFI NAVAll | Daily incremental NAVs | `amfiindia.com/spages/NAVAll.txt` |
| NSE Indices | TRI benchmarks (NOT Yahoo PR) | `niftyindices.com/Backpage.aspx/getTotalReturnIndexString` |
| FBIL / manual | 91-day T-bill yield | manual CSV in v1 |

## v2 (deferred)

AMC portfolio adapters → Active Share, pairwise ISIN overlap pruning at the shortlist
stage, AUM drag, PTR, manager tenure. SEBI stress-test PDFs are v3.
