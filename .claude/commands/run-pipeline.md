---
description: Run the mfs mutual-fund pipeline end-to-end with narrated stages; halt on stale data or scrape failures rather than producing garbage
allowed-tools: Bash(uv:*), Bash(uv run:*), Bash(uv sync:*), Bash(rm:*), Bash(mkdir:*), Bash(ls:*), Bash(find:*), Bash(cat:*), Bash(head:*), Bash(tail:*), Bash(du:*), Bash(stat:*), Bash(wc:*), Bash(grep:*), Bash(awk:*), Bash(cut:*), Bash(column:*), Bash(curl:*), Bash(date:*), Bash(make:*), Read, Edit, Write, Glob, Grep, AskUserQuestion
argument-hint: [--clean] [--skip-phase2] [--allow-fallback] [--no-rank-ask]
---

# Run the mfs pipeline end-to-end

You are running the Indian mutual fund evaluation pipeline at `/Users/suryavamseeayyagari/mfs/` end-to-end and narrating every step. Your job is to **execute, observe, and halt cleanly** if any stage produces stale or partial data — the pipeline enforces freshness and scrape-success invariants in code (`mfs.errors.PipelineError` / `IngestError` / `FreshnessError`).

**Storage is PostgreSQL** (DSN `postgresql:///mfs`, override `MFS_DB_URL`). All curated data (NAV, benchmarks, risk-free, scheme_master, holdings, PTR, AUM, constituents, stock-ADV, computed_metrics) lives in Postgres — NOT in parquet. The only files written are the ranked output CSVs/parquets under `data/output/shortlist/<as_of>/`. Do not look for `data/curated/` or `data/metrics/` — that was a legacy parquet store, since migrated to Postgres.

## The single canonical command

There is one end-to-end runner: **`uv run mfs pipeline`**. It orchestrates, in order, and **halts at the first REQUIRED stage that fails** (no partial/stale data ever reaches compute or rank):

```
ingest navs (today) → ingest benchmarks → ingest tbill → build scheme-master
  → COVERAGE GATE A (BLOCKING: NAV/benchmark/risk-free/scheme_master)
  → ingest amfi-aum → ingest managers (PTR) → ingest bhavcopy
  → ingest constituents (best-effort) → ingest holdings
  → COVERAGE GATE B (ADVISORY: holdings/PTR/AAUM/constituents/stock-ADV)
  → freshness gate → compute phase1 → rank-deep (stage 1 + 2 + 3)
```

**Two coverage gates** print a bordered per-source report (`DATA COVERAGE — GATE A/B`) answering six questions per source: need-from / have-from / have-till / fresh-by / expected-vs-actual / gap+fix. **Echo these reports verbatim** — they are the data-quality posture of the run, and the user runs evaluations only through this command.
- **Gate A is BLOCKING** and runs *before* the expensive ~50-AMC Phase-2 scrape, so broken base data halts in seconds, not 15+ minutes. A blocking breach exits 2 with the failing source, the gap, and the remediation — surface that, don't bury it.
- **Gate B is ADVISORY** (it is the *only* coverage net for the monthly/quarterly signals, whose freshness gates are `null`). Gaps are reported loudly and the affected funds are excluded from those metrics downstream; the run continues. A run ends with a `RUN DATA-QUALITY SUMMARY` — relay it.

Flags: `--as-of YYYY-MM-DD` (default today), `--skip-phase2` (Phase-1-only debug: skips holdings/PTR/AUM/bhavcopy/constituents — produces an empty Stage 2, only use for debugging Phase 1), `--allow-fallback` (emit synthetic 6.5% T-bill if RBI fetch fails — avoid), `--full` (force a from-scratch re-ingest — see below).

**Incremental by default.** Each ingest stage fetches only the gap, not the full history: benchmarks fetch per-ticker from `MAX(date) − 90d` (a cold/empty ticker still backfills full history); bhavcopy walks only from its latest stored date forward; factsheets skip the pdfplumber re-parse when the PDF bytes **and** the scheme-match universe are byte-identical to the last successful ingest (a corrected factsheet or a changed scheme_master still re-parses). This is what takes a warm run from ~hours to minutes. **The safety net is the coverage gate**: incremental fetches less, then Gate A/B proves the gap actually closed — so "faster" never means "silently stale." Use **`--full`** for a periodic deep refresh (re-fetch all benchmark history, re-walk the full bhavcopy window, re-parse every factsheet) — e.g. to pick up a deep NSE TRI restatement older than the 90-day tail.

**Prefer `mfs pipeline` over hand-running stages.** Run stages individually only when diagnosing a specific failure (see the playbook). Narrate the `[pipeline] <stage> ...` lines it emits as they stream.

## Two invariants (do not violate)

1. **Every dataset must be fresh.** NAVs, NSE TRI closes, and the 91-day T-bill series each have a max-lag in `configs/pipeline.yaml` under `freshness:` (NAV/benchmark 5 business days, T-bill 14 days, scheme_master 1 day). The monthly/quarterly signals (holdings, constituents, PTR, AUM, stock-ADV) have their `freshness:` lag set to `null` = the FreshnessError gate is disabled for them — **but Coverage Gate B now covers them** (expected-vs-actual entity counts, reported as advisory gaps). Before compute, the pipeline asserts the daily series are in-bounds and raises `FreshnessError` otherwise — **do not bypass; do not pass `skip_freshness=True`.**

2. **Every scrape must succeed within its retry budget.** The HTTP layer retries transient errors. If retries are exhausted for AMFI / NSE / RBI, the stage raises `IngestError`. **Do not silently skip a failed window/ticker/prid.** Re-run the failing stage once; if it still fails, surface the root cause (server outage? renamed field? broken regex?) and STOP.

Arguments passed: `$ARGUMENTS`

## How to narrate

Before a stage: what it is, what it does (one sentence), expected runtime.
After: what changed (rows ingested / partitions written), anything unexpected (empty output, warnings, slow). If you fix something, explain **what** broke and **why** the fix works. Keep each block to 2-4 sentences.

## Argument handling

- `--clean` → force a full re-backfill of the daily series before ranking: `rm -rf data/output/shortlist`, then `uv run mfs ingest navs --backfill`, `uv run mfs ingest tbill --backfill`, `uv run mfs ingest benchmarks`, then `uv run mfs pipeline`. (DB tables upsert in place — there's nothing else to wipe.)
- `--skip-phase2` → pass through to `mfs pipeline --skip-phase2` (Phase-1-only; Stage 2 will be empty, expected).
- `--allow-fallback` → pass through to `mfs pipeline --allow-fallback`.
- `--no-rank-ask` → don't prompt before relaxing filter thresholds; report and continue with defaults.
- (no args) → smart incremental: `uv run mfs pipeline` (ingests today's NAVs, refreshes benchmarks/tbill/monthly signals, recomputes, re-ranks).

## Pre-flight (always run)

1. `cd /Users/suryavamseeayyagari/mfs && pwd` — confirm working dir.
2. `uv --version && ls pyproject.toml` — confirm uv + project.
3. `uv run mfs status` — Postgres row counts + date bounds per table (nav_daily, benchmark_daily, risk_free_daily, scheme_master, computed_metrics, fund_log_returns) + latest shortlist. This is your before-snapshot.
4. If `mfs status` errors with a connection failure → Postgres isn't set up. Run `uv run mfs db init` (creates the DB + applies schema). Only if it's a brand-new empty DB will a full backfill be needed (treat like `--clean`).
5. State to the user which mode you're running (incremental / clean / skip-phase2) and why.

If `pyproject.toml` is missing → STOP (wrong directory). If `uv` is missing → STOP (install uv).

## Stage reference (what `mfs pipeline` runs — for accurate narration)

- **ingest navs** — AMFI daily NAVAll. Incremental (~5 s) on a warm DB; `--backfill` does full history (5–15 min).
- **ingest benchmarks** — fetches the **23 NSE equity TRIs** (9 broad + 14 sector) via `niftyindices.com/Backpage.aspx/getTotalReturnIndexString` in ≤360-day chunks, then **synthesizes the 3 hybrid TRIs** (Hybrid 65:35, 50:50, Equity Savings) from the NIFTY-50 sleeve + risk-free. ~3–5 min. If an **equity** ticker returns 0 rows after retries → `IngestError`.
- **ingest tbill** — scrapes weekly 91-day T-bill cut-off press releases from RBI (`BS_PressReleaseDisplay.aspx`), then forward-fills to daily. ~10 s warm, up to ~25 min cold. Raises `IngestError` if >5% of prids fail after retries, or if the latest auction is older than `max_rf_lag_days`.
- **build scheme-master** — derives the canonical scheme dimension from today's NAVAll + NAV history. ~5 s, ~14k rows. (Closed-ended/interval and non-equity are excluded; only Direct+Growth with a benchmark are ranked → ~664 funds.)
- **ingest amfi-aum** — one GET to AMFI's quarterly per-scheme Average AUM endpoint; covers all schemes in one shot (`source_amc='amfi_aaum'`). The pipeline auto-selects the latest quarter. (Manual form: `uv run mfs ingest amfi-aum --quarter Q4-2026`, where Q4-2026 = Jan–Mar 2026.)
- **ingest managers** — runs every registered factsheet adapter (41 AMCs). Each downloads the latest monthly factsheet PDF and extracts **PTR** (+ factsheet-path holdings without ISIN). NOTE: this produces **Portfolio Turnover Ratio, not manager tenure** — manager-tenure and stress-test extraction were retired. ~10–30 s per AMC.
- **ingest bhavcopy** — NSE daily bhavcopy for the last ~75 days → `stock_adv_daily` (per-ISIN turnover + close), feeding the 60-day median ADV used by AUM Impact Cost. Symbol→ISIN map (`EQUITY_L.csv`) refreshes weekly.
- **ingest constituents** — benchmark index constituent weights from manual CSVs (`data/raw/index_constituents/manual/<ticker>/<YYYY-MM>.csv`). **Best-effort** — no-ops cleanly if absent. Feeds Active Share only.
- **ingest holdings** — per-AMC monthly portfolio Excels (~46 AMCs) with **ISINs** → `holdings_monthly`. Unblocks AUM Impact Cost and tightens Active Share matching.
- **freshness gate** — hard gate before compute (NAV/benchmark/T-bill/scheme_master lags). Raises `FreshnessError` if any daily series is stale.
- **compute phase1** — Performance & Consistency metrics for every eligible Direct+Growth scheme. CPU-bound; benign statsmodels RuntimeWarnings ("divide by zero", "degrees of freedom") appear — mention once, ignore.
- **rank-deep** — the production ranking, three stages (Phase 2 compute runs internally on the Stage-1 pool):
  - **Stage 1** — Phase-1-only composite, hard filters, per-category ranking. → `<as_of>/stage1/`.
  - **Stage 2** — take top-20/category, drop funds missing a **required Phase-2 metric** (`style_drift_3y`, `ptr_latest`, `aum_impact_cost_days`), re-rank with Stage-2 weights, keep top-5. **`active_share` is NOT required** (it's a soft signal, currently null universe-wide until ≥3 months of holdings+constituents accumulate). → `<as_of>/stage2/` with `coverage.csv` + `dropped.csv`.
  - **Stage 3** — iterative pairwise portfolio-overlap **DROP** (cross-category): repeatedly drop the worse-ranked fund of the highest-overlap pair >30% until none remain. → `<as_of>/stage3/` with `dropped.csv` (each drop names the kept fund + shared holdings) + `overlap_pairs.csv`. **Note: this DROPS funds — it is not informational.**

## Post-run review (after `mfs pipeline` succeeds)

1. `uv run mfs status` — after-snapshot (compare row counts to pre-flight).
2. Coverage + drops:
   ```bash
   AS=$(uv run python -c "import polars as pl,glob,os; print(sorted(os.path.basename(p) for p in glob.glob('data/output/shortlist/*') if os.path.isdir(p))[-1])")
   echo "latest run: $AS"
   column -t -s, data/output/shortlist/$AS/stage2/coverage.csv | head -30
   echo "--- stage2 data-drops (missing required Phase-2 metric) ---"; column -t -s, data/output/shortlist/$AS/stage2/dropped.csv | head
   echo "--- stage3 overlap drops (dropped -> kept) ---"; column -t -s, data/output/shortlist/$AS/stage3/dropped.csv | head -40
   ```
3. Structural sanity-check on the Stage-1 outputs (these ARE parquet files; the per-table data is in Postgres):
   ```bash
   uv run python - <<'PY'
   import polars as pl, glob, os, sys
   files=sorted(p for p in glob.glob(f"data/output/shortlist/{os.environ.get('AS','')}/stage1/*.parquet") if "mf_report" not in os.path.basename(p))
   CORE={"as_of_date","canonical_category","rank","scheme_code","scheme_name","composite_score",
         "alpha_3y_annualized","sortino_3y","info_ratio_3y","capture_efficiency","r_squared_3y","beta_3y","data_quality_flag"}
   fails=[]
   for p in files:
       cat=os.path.basename(p).replace(".parquet","").replace("_"," "); df=pl.read_parquet(p)
       miss=CORE-set(df.columns)
       if miss: fails.append(f"{cat}: missing {sorted(miss)}")
       if df["rank"].to_list()!=list(range(1,df.height+1)): fails.append(f"{cat}: ranks not 1..N")
       if df["scheme_code"].n_unique()!=df.height: fails.append(f"{cat}: duplicate scheme_code")
       if df["composite_score"].null_count()>0: fails.append(f"{cat}: null composite_score")
   print(f"stage1 categories: {len(files)}")
   print("STRUCTURAL:", "PASS" if not fails else "FAIL")
   for f in fails: print("  FAIL:", f)
   sys.exit(2 if fails else 0)
   PY
   ```
   - Exit 2 (structural fail) → STOP, tell the user the shortlist is corrupted, investigate the rank stage. Don't present picks.
   - Exit 0 → continue.
   Do NOT auto-fix anomalies — flag them, let the user decide. (`active_share_median_1y` being all-null is EXPECTED, not a fault.)

## Final report

- Total schemes (scheme_master), benchmark tickers, funds in computed_metrics (from `mfs status`).
- Stage 1 / Stage 2 (survivors, data-drops) / Stage 3 (final picks, overlap-drops) counts.
- Output directory path.
- Any thresholds changed, with before/after + a one-line justification.
- Offer to: open a category's CSV, deep-dive a flagged fund, or re-rank with different weights (`mfs rank-deep`) without re-ingesting.

## Rank diagnostic playbook (if very few funds make Stage 2)

Stage 1 hard filters live in **code** (`src/mfs/rank/filters.py`, not the yaml), and are deliberately permissive floors: drops `INSUFFICIENT_HISTORY`/`POOR`, then catastrophic-only metric floors — `capture_efficiency > 0.50`, `info_ratio_3y > -1.0`, `r_squared_3y ∈ [0.40, 1.00]`, `beta_3y ∈ [0.30, 1.70]` (all null-tolerant). The funnel (per-gate):

```bash
uv run python - <<'PY'
import polars as pl; from datetime import date
from mfs.db import queries as q
m=q.computed_metrics_at(date.today())  # or pass the run's as_of
import mfs.rank.filters as F
d=m.filter((pl.col("data_quality_flag")!="INSUFFICIENT_HISTORY")&(pl.col("data_quality_flag")!="POOR"))
print("computed:",m.height," after data-quality:",d.height)
PY
```

If Stage 2 is near-empty, the usual cause is **missing Phase-2 data**, NOT filters — i.e. `ingest holdings` / `ingest amfi-aum` / `ingest managers` / `ingest bhavcopy` didn't populate, so `ptr_latest` / `aum_impact_cost_days` are null and Stage 2's completeness filter drops everyone. Check `stage2/dropped.csv`'s `missing_metrics` column. Re-run the missing ingest stage. Only relax filters (via `AskUserQuestion`, unless `--no-rank-ask`) if the funnel shows a metric floor is the culprit — and **announce any `configs/pipeline.yaml` change before making it** (note: the live floors are in `filters.py`, so changing the yaml `filters:` block alone has no effect on hard-filtering).

## Diagnose-and-halt playbook

Network 5xx/connection errors are auto-retried inside each stage. If a stage still raises, STOP and surface the root cause. Never edit code to bypass a check; never compute/rank on stale or partial inputs.

- **`IngestError: AMFI NAVAll returned N bytes but parsed 0 rows`** — AMFI returned HTML/rate-limit. Inspect `data/raw/amfi_nav/<y>/<m>/<d>/NAVAll.txt`; re-run after 30 s; if the column layout changed, update `_detect_layout()` in `src/mfs/ingest/amfi_nav.py`.
- **`IngestError: AMFI NAV backfill: N window(s) failed`** — re-run `uv run mfs ingest navs --backfill` (cached windows skip). Same windows twice → STOP.
- **`IngestError: NSE equity TRI ingest: N ticker(s) failed`** — re-run `uv run mfs ingest benchmarks`. A ticker failing twice → curl `https://iislliveblob.niftyindices.com/assets/json/IndexMapping.json` and check for a renamed index; update `NSE_TRI_MAP` in `src/mfs/ingest/benchmarks.py`. STOP otherwise.
- **`IngestError: RBI T-bill walk: N/M prids failed`** — re-run `uv run mfs ingest tbill` (cached prids skip). Persistent → RBI is down; STOP, don't lower the threshold.
- **`Risk-free data stale` / `FreshnessError`** — RBI hasn't published a recent cut-off, or the title/YTM regex missed a new wording. Check the RBI RSS for a recent "T-Bill Auction Result"; if one exists but wasn't parsed, inspect the latest cached HTML in `data/raw/fbil_tbill/rbi_press_releases/` and the regexes in `src/mfs/ingest/fbil_tbill.py` (`_TENORS_RE`, `_TBILL_RESULT_TITLE_RE`, `_YTM_BLOCK_RE`, the 91-day tenor guard `_is_91day_result`). STOP if RBI is genuinely behind.
- **A single factsheet/holdings adapter fails** — as of the fault-isolation change, one AMC failing (a 404, changed URL/layout, parser yields 0 rows, or an `amc_code` that doesn't match the adapter slug) no longer crashes the run: it is caught, logged (`managers.amc_failed` / `holdings.amc_failed`), and surfaces as that AMC's gap in **Coverage Gate B**. The run continues; the affected funds are excluded from holdings/PTR downstream. To fix: `uv run mfs ingest managers --list` / `--list`, then re-run that AMC (`--amc <slug>`). Only when **every** adapter fails does `run_all` raise `IngestError` (systemic network/config issue).
- **`CoverageError` (Gate A halt)** — a BLOCKING source (NAV/benchmark/risk-free/scheme_master) is empty, stale, too-short-history, or below the 50% entity floor. The rendered Gate A table names the source, the gap, and the remediation — run that and re-try. Do NOT bypass; do not compute on incomplete base data. (A partial gap *above* the floor shows as `[GAP]` but passes — surface it, don't halt.)
- **`numpy … Degrees of freedom <= 0`** during ranking — a category with one surviving fund; std-dev undefined; the fund gets z=0. Cosmetic, no action.

## Critical: do not modify methodology silently

If you change `configs/pipeline.yaml` (composite weights, filter thresholds) → **announce it first**, with prior and new values. Never edit `category_thresholds.yaml`, `benchmarks.csv`, `src/mfs/rank/filters.py`, or compute logic without explicit user confirmation, even when fixing a "broken" run. The user is a CFA L1; methodology decisions are theirs.

## End of skill

After the post-run review + final report, end the turn with the offer-to-inspect prompt. Do not loop; do not auto-run a second pipeline.
