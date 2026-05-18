---
description: Run the mfs mutual-fund pipeline end-to-end with narrated stages; halt on stale data or scrape failures rather than producing garbage
allowed-tools: Bash(uv:*), Bash(uv run:*), Bash(uv sync:*), Bash(rm:*), Bash(mkdir:*), Bash(ls:*), Bash(find:*), Bash(cat:*), Bash(head:*), Bash(tail:*), Bash(du:*), Bash(stat:*), Bash(wc:*), Bash(grep:*), Bash(awk:*), Bash(cut:*), Bash(curl:*), Bash(date:*), Bash(make:*), Read, Edit, Write, Glob, Grep, AskUserQuestion
argument-hint: [--clean] [--no-rank-ask]
---

# Run the mfs pipeline end-to-end

You are running the Indian mutual fund evaluation pipeline at `/Users/suryavamseeayyagari/mfs/` end-to-end and narrating every step to the user. Your job is to **execute, observe, and halt cleanly** if any stage produces stale or partial data — the pipeline now enforces freshness and scrape-success invariants in code (`mfs.errors.PipelineError`).

## Two invariants (do not violate)

1. **Every dataset must be fresh.** AMFI NAVs, NSE TRI closes, and the 91-day T-bill series each have a configured max-lag in `configs/pipeline.yaml` under `freshness:`. Before compute or rank runs, the pipeline asserts each is within bounds. If not, it raises `FreshnessError` — **do not bypass this; do not skip ahead to compute.**

2. **Every scrape must succeed within its retry budget.** The HTTP layer retries transient errors (5xx, network). If retries are exhausted for AMFI / NSE / RBI, the ingest stage raises `IngestError`. **Do not silently skip a failed window/ticker/prid** — the right response is: re-run the failing stage, and if it still fails, surface the root cause to the user (server outage? renamed field? broken regex?) and STOP. Never proceed to compute with partial data.

Arguments passed: `$ARGUMENTS`

## How to narrate

Before every command:
- State what stage you're in (e.g., "Stage 3 of 7: NSE benchmark ingestion")
- State what the command will do in one sentence
- State the expected runtime ("~5 min", "instant")

After every command:
- Summarize what changed on disk (rows ingested, partitions written)
- Note anything unexpected — empty output, warnings, slower than expected
- If you fix something, explain **what** broke and **why** the fix works

Keep each narration block tight — 2-4 sentences per stage. Don't restate the obvious.

## Argument handling

- `--clean` → wipe `data/curated/`, `data/metrics/`, `data/output/`, and `data/raw/benchmarks/`. Keep `data/raw/amfi_nav*/` (expensive HTTP cache). Then run a full backfill.
- `--no-rank-ask` → on the rank stage, don't ask the user before adjusting thresholds; just report and continue with defaults.
- (no args) → smart incremental: keep existing NAV history, refresh benchmarks if stale, re-run compute + rank.

## Pre-flight (always run)

1. `cd /Users/suryavamseeayyagari/mfs && pwd` — confirm working dir
2. `uv --version && ls -la pyproject.toml` — confirm uv and project files
3. `uv run mfs status` — inventory of what's on disk before we start
4. State to user: which stages will run, which will be skipped, and why.

If `pyproject.toml` is missing → STOP and tell the user they're in the wrong directory.
If `uv` is missing → STOP and tell the user to install uv first.

## Stage 1: Cleanup

What gets deleted depends on args:

- `--clean`: `rm -rf data/curated data/metrics data/output data/raw/benchmarks`
- default: `rm -rf data/metrics/computed_metrics data/output/shortlist` only (stale derived outputs; preserves NAVs and benchmarks)

Narrate: "Wiping <these> because <reason>. Keeping <these> to avoid <cost>."

## Stage 2: NAV ingestion

Decision rule:
- If `data/curated/nav_daily/` already has ≥ 3 yearly partitions AND no `--clean` → run `uv run mfs ingest navs` (incremental, ~5 sec)
- Otherwise → run `uv run mfs ingest navs --backfill` (5–15 min)

State this decision and the reasoning to the user before running.

## Stage 3: NSE TRI benchmarks

`uv run mfs ingest benchmarks` — fetches all 9 equity TRI tickers via `niftyindices.com/Backpage.aspx/getTotalReturnIndexString` in 360-day chunks. Expected ~3–5 min.

The 3 hybrid/multi-asset tickers (Hybrid 65:35, Hybrid 50:50, Equity Savings) will log `benchmark.empty` warnings — that's expected; they need manual CSVs and are exempt from the equity-TRI success contract. If any **equity** ticker returns 0 rows or raises after retries, `ingest_all_known()` will raise `IngestError`. Re-run once; if it fails again, investigate (server outage? renamed Trading_Index_Name?) and STOP.

## Stage 4: Risk-free rate (real RBI scrape)

`uv run mfs ingest tbill` — scrapes weekly 91-day T-bill cut-off press releases from RBI (`BS_PressReleaseDisplay.aspx`) with retry, then forward-fills to daily. Expected ~10 sec on a warm cache, up to ~25 min for a cold backfill (12k+ prids). Defaults to 5-year history.

Hard rules: if RBI returns 5xx for more than `max_tbill_scrape_failure_rate` (default 5%) of prids after retries, the stage raises `IngestError`. If the latest auction is older than `max_rf_lag_days` (default 14), the stage raises `IngestError`. The synthetic 6.5% fallback only ever writes when the user explicitly passes `--allow-fallback`.

## Stage 5: Scheme master

`uv run mfs build scheme-master` — ~5 sec. Derives the canonical scheme dimension from today's AMFI NAVAll snapshot + the NAV history. Should report ~14k rows.

## Stage 6: Freshness gate (always run before compute)

`uv run python -c "from mfs.freshness import check_freshness; r = check_freshness(raise_on_fail=True); print(r)"`

This is a hard gate. It verifies (a) latest NAV ≤ `max_nav_lag_bdays` Indian business days behind, (b) every equity TRI ≤ `max_bench_lag_bdays` behind, (c) latest T-bill auction ≤ `max_rf_lag_days` behind, (d) scheme master rebuilt today. If it raises, the pipeline halts before compute. **Do not skip this stage; do not pass `skip_freshness=True` to `compute.orchestrator.run()`.**

`compute.orchestrator.run()` itself also calls `check_freshness(raise_on_fail=True)` internally — running the stage directly is belt-and-braces.

## Stage 7: Compute metrics

`uv run mfs compute metrics` — 5–10 min for ~400 eligible Direct+Growth schemes. CPU-bound, you'll see statsmodels RuntimeWarnings ("divide by zero in scalar divide", "degrees of freedom") — those are benign edge cases inside individual regressions, not failures. Mention them once, ignore thereafter.

After it completes, run a diagnostic — do not skip this:

```bash
uv run python -c "
import polars as pl
df = pl.read_parquet('data/metrics/computed_metrics/as_of_date=$(date +%Y-%m-%d)/data.parquet')
print('Total:', df.height)
for col in ['r_squared_3y','beta_3y','capture_efficiency','info_ratio_3y','alpha_3y_annualized']:
    s = df[col].drop_nulls()
    if not s.is_empty():
        print(f'{col:25s} p25={s.quantile(0.25):.3f}  med={s.median():.3f}  p75={s.quantile(0.75):.3f}')
"
```

Show the user the distribution. This is where threshold mismatches become visible.

## Stage 8: Rank

`uv run mfs rank --top-n 25`

After it runs, count how many categories got non-empty output:

```bash
ls data/output/shortlist/$(date +%Y-%m-%d)/ | wc -l
```

If fewer than ~10 categories have output → invoke the **Rank diagnostic** playbook below.

## Rank diagnostic playbook

If the rank step produces output for very few categories, the most likely cause is a filter mismatch. Run this per-filter funnel diagnostic:

```bash
uv run python -c "
import polars as pl
df = pl.read_parquet('data/metrics/computed_metrics/as_of_date=$(date +%Y-%m-%d)/data.parquet')
s1 = df.filter(pl.col('data_quality_flag') == 'GOOD')
s2 = s1.filter(pl.col('capture_efficiency').is_not_null() & (pl.col('capture_efficiency') > 1.15))
s3 = s2.filter(pl.col('info_ratio_3y').is_not_null() & (pl.col('info_ratio_3y') > 0.5))
s4 = s3.filter(pl.col('r_squared_3y').is_not_null() & (pl.col('r_squared_3y') >= 0.70) & (pl.col('r_squared_3y') <= 0.90))
print(f'computed={df.height}  GOOD={s1.height}  capture>1.15={s2.height}  IR>0.5={s3.height}  R2[0.70,0.90]={s4.height}')
"
```

Identify which gate is dropping the most funds.

**Known threshold issues** (from prior runs):
- **R² band [0.70, 0.90] is too narrow.** Most diversified Indian active equity funds have R² in [0.85, 0.97]; capping at 0.90 disqualifies the median fund. Original methodology was for "high-conviction" funds only.
- **Capture efficiency > 1.15** can be aggressive in the current ranking universe (median observed ~1.03). Loosen to > 1.0 to demand only that the fund isn't *worse* than its benchmark on the asymmetry axis.
- **IR > 0.5** disqualifies ~half the universe (the median IR is around 0.2). 0.5 is the original methodology but harsh for early-cycle Indian equity.

Action: **ask the user via AskUserQuestion** which thresholds to relax. Unless `--no-rank-ask` was passed.

If `--no-rank-ask`: relax `configs/pipeline.yaml`:
- `filters.r_squared_band: [0.70, 0.98]`
- `filters.capture_efficiency_min: 1.00`
- `filters.info_ratio_3y_min: 0.2`

Then rerun `uv run mfs rank --top-n 25`.

## Stage 9: Final report

Print to the user:
- Total schemes ingested
- Total benchmark tickers
- Total funds in computed_metrics
- Per-category fund counts in the shortlist output
- Path to the output directory
- Any thresholds that were adjusted, with before/after values and a one-line justification

Then offer to:
- Open one specific category's CSV to eyeball (default suggestion: Flexi Cap)
- Re-rank with different weights without recomputing

## Known intermittent issues — diagnose-and-halt playbook

These are issues seen in prior runs. Network 5xx + connection errors are retried automatically inside each ingest stage; if a stage still raises `IngestError` or `FreshnessError`, **STOP** and tell the user the root cause. Never edit code to bypass a check; never run compute/rank on stale or partial inputs.

### IngestError: `AMFI NAVAll returned N bytes but parsed 0 rows`
**Symptom**: `ingest_today` raises after retries.
**Cause**: AMFI returned an HTML error page or rate-limited.
**Action**: inspect `data/raw/amfi_nav/<year>/<month>/<day>/NAVAll.txt`. If it's HTML → re-run after 30s. If the schema changed (new column order) → update `_detect_layout()` in `src/mfs/ingest/amfi_nav.py`. STOP if neither fix applies.

### IngestError: `AMFI NAV backfill: N window(s) failed`
**Symptom**: `ingest_backfill` raises with a list of windows.
**Cause**: AMFI bulk-history endpoint had partial outages.
**Action**: Re-run `uv run mfs ingest navs --backfill`. The cache means already-fetched windows are skipped. If the SAME windows fail twice in a row, surface the issue and STOP.

### IngestError: `NSE equity TRI ingest: N ticker(s) failed`
**Symptom**: `ingest_all_known` raises with per-ticker reasons.
**Cause**: NSE intermittent 5xx, or a `Trading_Index_Name` rename.
**Action**: Re-run `uv run mfs ingest benchmarks` once. If a specific ticker fails twice, curl `https://iislliveblob.niftyindices.com/assets/json/IndexMapping.json` (UTF-8 BOM) and check whether its `Trading_Index_Name` changed. Update `NSE_TRI_MAP` in `src/mfs/ingest/benchmarks.py` and re-run. STOP otherwise.

### IngestError: `RBI T-bill walk: N/M prids failed after retries`
**Symptom**: T-bill ingest raises with a failure rate above `max_tbill_scrape_failure_rate`.
**Cause**: RBI infrastructure degraded.
**Action**: Re-run `uv run mfs ingest tbill` (cached prids are skipped). If the failure rate stays high, STOP and tell the user RBI is down — don't lower the threshold.

### IngestError: `Risk-free data stale: latest auction=...`
**Symptom**: T-bill ingest succeeds but the latest scraped auction is older than `max_rf_lag_days`.
**Cause**: RBI hasn't published a recent T-bill cut-off (e.g., holiday weeks), OR the title regex no longer matches a new wording.
**Action**: Check the RSS feed manually for a recent "T-Bill Auction Result: Cut-off" press release. If one exists but wasn't matched, inspect the latest cached HTML in `data/raw/fbil_tbill/rbi_press_releases/` and update the regexes in `src/mfs/ingest/fbil_tbill.py` (`_TENORS_RE`, `_CUTOFF_RE`, `_TBILL_AUCTION_RE`). STOP if the RBI feed itself is genuinely behind.

### FreshnessError at the freshness gate
**Symptom**: `check_freshness` raises with a list of stale datasets.
**Cause**: An earlier ingest stage succeeded numerically but the data underneath is too old.
**Action**: Re-run the specific stage(s) whose data is stale (NAV / benchmark / T-bill). Do NOT proceed to compute. If a stage refuses to update its data (the latest available really is too old), tell the user and STOP.

### Rank step writes very few files
**Symptom**: `Wrote N < ~10 category shortlist file(s)`.
**Cause**: Hard filters too strict for the current universe.
**Action**: This is NOT a data freshness issue — invoke the **Rank diagnostic playbook** above and ask the user about threshold relaxation.

### `numpy/lib/_nanfunctions_impl.py … Degrees of freedom <= 0`
**Symptom**: warning during the rank stage's z-score step.
**Cause**: A category has only 1 surviving fund; std-dev undefined.
**Impact**: cosmetic; that fund gets z=0 (handled in `_zscore_one`). No action needed.

## Critical: do not modify methodology silently

If you change `configs/pipeline.yaml` (composite weights, filter thresholds, R² band) → **announce it before doing it**, with the prior value and the new value. Never edit `category_thresholds.yaml`, `benchmarks.csv`, or compute logic without explicit user confirmation, even when fixing a "broken" run. The user is a CFA L1; methodology decisions are theirs.

## End of skill

After Stage 8, end the turn with the final report and the offer-to-inspect prompt. Do not loop. Do not auto-run a second pipeline.
