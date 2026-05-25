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
- State what stage you're in (e.g., "Stage 3 of 10: NSE benchmark ingestion")
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

## Stage 5b: Manager-tenure ingest (Phase 2.1+)

`uv run mfs ingest managers` — runs every registered AMC adapter (currently `hdfc`; more added in Phase 2.1 PRs). Each adapter downloads the latest monthly factsheet PDF, parses out per-scheme (manager_name, start_date) rows, fuzzy-matches scheme names to scheme_codes, and writes to the `managers` table. Expected ~10-30 sec per AMC adapter.

Coverage report per AMC:
- `rows_written` — rows upserted to the managers table.
- `matched_schemes` — printed scheme names that matched something in scheme_master.
- `unmatched` — printed names that scored below the fuzzy-match threshold (logged with their best score). Investigate if this number is non-trivial.

If an adapter raises `IngestError`, **stop** and surface the cause. Likely root causes:
- URL pattern changed (AMC moved their factsheet location).
- Layout changed (parser extracts 0 manager rows).
- Scheme master amc_code doesn't match the adapter's amc_slug — re-run `mfs build scheme-master` after fresh NAV ingest.

If `max_manager_data_lag_days` in pipeline.yaml is non-null and the managers table is older than that window, freshness check (Stage 6) will fail. Either re-run this stage or temporarily set the threshold to null while debugging.

### Stage 5b.1 (optional): User-provided manager gap-fill (Phase 3.B)

`uv run mfs ingest managers --user-provided` — loads `data/raw/managers/user_provided.csv` if it exists. This is the narrow gap-fill path for genuinely un-scrapable AMCs: user supplies complete `(scheme_code, manager_name, manager_start_date, is_lead)` tuples and they're upserted with `source_amc='user'`. The CSV is OPTIONAL — if absent, this stage no-ops cleanly. If present, unknown scheme_codes are skipped (logged) and malformed rows are rejected per-row without crashing the stage. Run it AFTER the factsheet adapters so user rows can overwrite an auto-scraped value on the same `(scheme, manager, start_date)` PK if needed.

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
cols = [
    'r_squared_3y','beta_3y','capture_efficiency','info_ratio_3y','alpha_3y_annualized',
    # Phase 2 metrics (beta_3y_std, r_squared_3y_mean, style_drift_3y populated
    # from v2.0; the remainder are filled by Phase 2.1-2.3).
    'beta_3y_std','r_squared_3y_mean','style_drift_3y',
    'active_share_median_1y','ptr_latest','aum_impact_cost_days',
    'stress_test_days_50pct','manager_tenure_years',
]
for col in cols:
    if col not in df.columns:
        continue
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

## Stage 9: Sanity-check the final output

After the rank stage writes the shortlist CSVs, read every category file back and run a three-tier checklist. Surface findings inline so the user knows whether to trust the output. Do **not** auto-fix anomalies — flag them and let the user decide.

Run this single diagnostic script (it covers all three tiers and prints a structured report):

```bash
uv run python - <<'PY'
import polars as pl, glob, os, sys
from datetime import date

as_of = date.today().isoformat()
# Per-category parquets only — exclude the consolidated mf_report.parquet (validated separately).
files = sorted(
    p for p in glob.glob(f'data/output/shortlist/{as_of}/stage1/*.parquet')
    if not os.path.basename(p).startswith('mf_report')
)
EXPECTED_COLS = {
    "as_of_date","canonical_category","rank","scheme_code","scheme_name","composite_score",
    "z_ret_3y_median","z_ret_3y_p25","z_alpha_3y_annualized","z_sortino_3y","z_info_ratio_3y",
    "z_capture_efficiency","ret_3y_median","ret_3y_p25","alpha_3y_annualized","sortino_3y",
    "info_ratio_3y","capture_up","capture_down","capture_efficiency","r_squared_3y","beta_3y",
    # Phase 2 columns. z_active_share_median_1y and z_style_drift_3y may be all-null
    # until Phase 2.1+ ingestion lands, but the columns must be present in the
    # shortlist schema.
    "z_active_share_median_1y","z_style_drift_3y",
    "beta_3y_std","r_squared_3y_mean","style_drift_3y",
    "active_share_median_1y","ptr_latest","aum_impact_cost_days",
    "stress_test_days_50pct","manager_tenure_years",
    # Phase 3.F transparency column.
    "tenure_data_status",
    "data_quality_flag",
}
TOP_N_CAP = 25  # matches --top-n on the rank call

structural = []
statistical = []
methodological = []
all_rows = []

for p in files:
    cat = os.path.basename(p).replace('.parquet','').replace('_',' ')
    df = pl.read_parquet(p)
    all_rows.append(df.with_columns(pl.lit(cat).alias('_cat')))

    missing = EXPECTED_COLS - set(df.columns)
    if missing:
        structural.append(f"{cat}: missing columns {sorted(missing)}")
    if df['rank'].to_list() != list(range(1, df.height+1)):
        structural.append(f"{cat}: ranks not contiguous 1..N")
    if df['scheme_code'].n_unique() != df.height:
        structural.append(f"{cat}: duplicate scheme_code within category")
    if df['composite_score'].null_count() > 0:
        structural.append(f"{cat}: null composite_score rows")
    if df.height > TOP_N_CAP:
        structural.append(f"{cat}: {df.height} rows exceeds top_n cap of {TOP_N_CAP}")

    # Statistical
    for r in df.iter_rows(named=True):
        b = r['beta_3y']; r2 = r['r_squared_3y']; a = r['alpha_3y_annualized']; ce = r['capture_efficiency']
        sc = r['scheme_code']; nm = r['scheme_name']
        if b is not None and not (0.3 <= b <= 1.5):
            statistical.append(f"{cat:18s} {sc} {nm[:55]:55s}  beta={b:+.3f} out of [0.3,1.5]")
        if r2 is not None and r2 < 0.60:
            statistical.append(f"{cat:18s} {sc} {nm[:55]:55s}  r2={r2:.3f} < 0.60")
        if a is not None and abs(a) > 0.30:
            statistical.append(f"{cat:18s} {sc} {nm[:55]:55s}  alpha={a:+.3f} > |30%|")
        if ce is not None and ce > 2.0:
            statistical.append(f"{cat:18s} {sc} {nm[:55]:55s}  capture_eff={ce:.3f} > 2.0")

    # near-duplicate composite_scores within category
    scores = df['composite_score'].to_list()
    for i in range(len(scores)-1):
        if scores[i] is not None and scores[i+1] is not None and abs(scores[i]-scores[i+1]) < 1e-3:
            statistical.append(
                f"{cat:18s} rank {i+1} and {i+2} composite scores within 1e-3 — possible undetected duplicate"
            )

    # Methodological
    if df.height < 3:
        methodological.append(f"{cat}: only {df.height} fund(s) — peer-relative scoring fragile")
    bad_quality = df.filter(pl.col('data_quality_flag') != 'GOOD').height
    if bad_quality > 0:
        methodological.append(f"{cat}: {bad_quality} shortlisted fund(s) with data_quality_flag != GOOD")
    top = df.head(1).to_dicts()[0]
    if top['composite_score'] is not None and top['composite_score'] < 0:
        methodological.append(f"{cat}: rank-1 fund has negative composite_score ({top['composite_score']:+.3f})")
    if top['alpha_3y_annualized'] is not None and top['alpha_3y_annualized'] < 0:
        methodological.append(f"{cat}: rank-1 fund has negative alpha ({top['alpha_3y_annualized']:+.3f})")

# Cross-category overlap
union = pl.concat(all_rows)
dup = union.group_by('scheme_code').agg(pl.col('_cat').unique().alias('cats')).filter(pl.col('cats').list.len() > 1)
if dup.height > 0:
    for r in dup.iter_rows(named=True):
        structural.append(f"cross-cat: scheme_code {r['scheme_code']} appears in {r['cats']}")

print(f"=== Structural ({len(structural)}) ===")
for x in structural: print(f"  FAIL: {x}")
if not structural: print("  PASS")
print(f"=== Statistical ({len(statistical)}) ===")
for x in statistical: print(f"  WARN: {x}")
if not statistical: print("  PASS")
print(f"=== Methodological ({len(methodological)}) ===")
for x in methodological: print(f"  WARN: {x}")
if not methodological: print("  PASS")
print(f"\nTotal funds across categories: {union.height}")
sys.exit(2 if structural else 0)
PY
```

**Interpretation rules:**
- Exit code 2 → at least one **structural** check failed. **STOP**; tell the user the shortlist is corrupted and do NOT present the top picks. Investigate the rank stage code or rerun with `--clean`.
- Exit code 0 → structural passed. Surface the warnings (if any) inline and continue to the final report.

**Report format to surface to the user:**
1. Per-tier counts (e.g., "Structural: PASS · Statistical: 3 warnings · Methodological: 1 warning").
2. If any warnings: a small table listing each flagged row (`category | scheme | metric | value`).
3. Top-3 per category (rank, scheme_name, composite_score, alpha_3y, sortino_3y) — same as before.
4. Offer to: open a specific category's CSV, deep-dive into any flagged fund, or rerun with different composite weights.

## Stage 10: Two-stage deep ranking (Phase 3 hybrid)

`uv run mfs rank-deep`

This is the production ranking path. It runs Stage 1 (same as `mfs rank` above), then layers Stage 2 + Stage 3 on top:

- **Stage 2 — Phase 2 data-completeness filter**. From each category's Stage 1 top-20, keep only funds that have every Phase 2 metric measured (Style Drift, Active Share, PTR, AUM Impact Cost, Manager Tenure — plus Stress Test for Mid Cap / Small Cap only). Output: top-5 survivors per category at `data/output/shortlist/<as_of>/stage2/<category>.csv`. If fewer than 5 survive in a category, every row gets `partial_coverage_flag=True`. Also emits:
  - `stage2/dropped.csv` — every fund dropped from a Stage 2 pool with the list of NULL metrics that disqualified it + AUM.
  - `stage2/coverage.csv` — per-category n/AUM survival rates with `top_dropped_amc` (the AMC contributing the most drops in that category — i.e. the next ingest gap to close).

- **Stage 3 — Cross-category pairwise overlap**. Across the full Stage 2 survivor set, compute pairwise portfolio overlap (sum of min-weight on shared securities, keyed by ISIN when present, normalized security_name as fallback). Pairs above 30% overlap get listed in `stage3/overlap_pairs.csv` with their top-3 shared holdings; no funds are dropped — this is informational so the user knows which pairs are too correlated to hold together. Summary at `stage3/overlap_summary.csv`.

Surface to the user after `rank-deep` runs:
1. **Coverage summary**: how many categories had < 5 survivors and why (from `coverage.csv`).
2. **Top dropped AMC overall**: the AMC most often missing data — the next ingestion gap to fix.
3. **Pairs flagged for overlap**: if any, list them with their overlap % so the user can prune their selection.

If the user is using `--no-rank-ask`, default to skipping the Stage 2/3 review prompt and just report the file paths.

## Stage 11: Final report

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

After Stage 10, end the turn with the final report and the offer-to-inspect prompt. Do not loop. Do not auto-run a second pipeline.
