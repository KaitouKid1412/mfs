# Phase 7 — Critical issues plan (ranking correctness & confidence)

**For the implementing session (Claude Opus 4.8).** This plan finishes the
high-value items the 2026-06-10 audit left open after the Stage-2 remediation.
Every item here can change rankings or the *confidence* we have in them. The
companion low/medium plan is `docs/phase7/low_medium_issues_plan.md`.

## Before you start — read these
- `docs/phase6/EXECUTION_STATE.md` — what shipped in Stage 2 and the operator follow-ups.
- `docs/audit/2026-06-10_pipeline_audit.md` — the original audit (the "why").
- The three spike memos referenced below already contain the detailed how:
  `docs/audit/ter_endpoint_spike_2026-06.md`, `docs/audit/hybrid_debt_sleeve_spike.md`,
  `docs/audit/benchmark_refit_2026-06.md`.

## Ground rules (hard-won — follow them)
- `pytest`, `git`, `psql`, and `uv run` (incl. network ingest) all work in-session and cost no plan tokens — verify inline, don't spawn agents to verify.
- **One item = one commit.** Full suite green before each commit: `uv run pytest tests/ -q --tb=line -p no:warnings; echo pytest_exit=$?` — read the printed `pytest_exit`, not a piped tail.
- **Apply each item's DB migration immediately after its commit** via `uv run mfs db init` (idempotent). A lost migration silently broke a recompute once.
- Respect the project invariants: fail-fast on stale/partial data; **no manual data entry** (auto-scrape clean tuples or skip the AMC); AUM is AMFI-AAUM-only; incremental-by-default with `--full`.
- End commit messages with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Commit only when an item is green; do not push unless asked.

## User decisions already locked (2026-06-13) — do not re-litigate
- **TER:** BUILD the real AMFI scraper (C2 below).
- **Survivorship:** accept forward-only + document; do NOT attempt historical backfill.
- **Backtest → weights:** the backtest REPORTS and STOPS for the user's approval; never auto-retune weights.
- **Reverse-engineered adapters:** keep + document; revisit only on breakage (handled in the low/medium plan).

---

## C1 — Run the full retro-IC backtest and report (validate the weights)

**What:** `tools/backtest_ic.py` is built and smoke-verified but the full
2016+ run has never been executed. The composite weights (alpha 0.25, rolling
returns ~0.50, etc.) have **no out-of-sample validation** — audit findings
2c/3/6 ("No backtest or documented rationale for any weight").

**Why:** This is the single highest-value remaining item. The whole pipeline
bets that funds ranked top on trailing metrics outperform going forward. The
smoke run already hinted Mid Cap's composite may have **negative** IC. Until
this runs, we don't know which categories the ranking actually helps in.

**How:**
1. Run the full backtest (hours; incremental via its parquet cache under `data/output/backtest/cache/`):
   ```bash
   uv run python tools/backtest_ic.py --since 2016-01-01 --horizons 1y,3y
   ```
   Use `--categories <a,b>` to shard if you want to checkpoint; the cache makes re-runs cheap.
2. Read `tools/backtest_ic.py`'s pre-registered verdict thresholds (in its docstring): the IC level / t-stat / top-N spread that would JUSTIFY vs REFUTE the current weights, per category and pooled.
3. Produce a report `docs/audit/backtest_results_<YYYY-MM>.md` with: per-category Spearman IC (1y + 3y), pooled mean IC + t-stat (non-overlapping windows), top-5-vs-median spread, and the weight-perturbation sensitivity table (incl. the `p25_collapsed` variant). State the survivor-only caveat prominently (see C-note below).
4. **For every category whose IC fails the threshold, propose** an evidence-based weight change (e.g. drop alpha weight where alpha IC is negative; collapse ret_median/ret_p25 if `p25_collapsed` matches baseline). Put the proposal in the report as a diff against `configs/pipeline.yaml`.
5. **STOP. Do not change any weight.** Surface the report and the proposed `composite_weights_stage1/stage2` diff to the user for approval. (Locked decision: report-and-stop.)

**Survivor-only caveat (must appear in the report):** pre-2026-06 dead funds
are absent (forward-only containment, by user decision), so backtest ICs are
computed on survivors and are optimistically biased. The conclusion "weights
are/aren't predictive" is directionally valid but the magnitudes are upper bounds.

**Acceptance:** `docs/audit/backtest_results_<YYYY-MM>.md` exists with per-category
IC, the verdict against pre-registered thresholds, and a concrete (un-applied)
weight-change proposal. No `configs/pipeline.yaml` weight changes committed.

**Needs the user:** approval of the proposed weight changes before any are applied (a follow-up task, not part of C1).

---

## C2 — Build the AMFI TER (expense-ratio) ingester

**What:** `src/mfs/ingest/amfi_ter.py` is a manual-CSV stub; TER is absent from
the DB and from scoring (audit 2c/3: "Expense ratio entirely absent from
scoring despite being the most evidence-backed forward predictor"). User
decision: **build it.**

**Why:** Net-of-fee NAV partially internalises cost, but a direct TER signal is
the most reliable forward predictor in the literature (low cost → higher net
returns persistently). It belongs as a display column and a stage-2 tiebreaker.

**How (full checklist is in `docs/audit/ter_endpoint_spike_2026-06.md`):**
1. Verify the endpoint (network): confirm the `amfiindia.com/ter-of-mf-schemes` XHR / form-backed endpoint and its response shape (the AAUM ingester `src/mfs/ingest/amfi_aum.py` is the proven pattern for AMFI form endpoints — mirror it).
2. Build `fetch` + `parse` in `amfi_ter.py` producing clean `(scheme_code, as_of_month, ter_pct)` tuples; reuse `_scheme_match` with the B5 collision guards; reject implausible TER (e.g. `0 < ter <= 3.0`) per the no-half-data invariant.
3. Schema: idempotent `CREATE TABLE IF NOT EXISTS scheme_ter_monthly` in `src/mfs/db/schema.sql` (CHECK on ter range; PK `(scheme_code, as_of_month)`); writer in `src/mfs/db/writers.py` + reader in `src/mfs/db/queries.py`.
4. Wire into the pipeline: a Phase-2 ingest stage in `src/mfs/pipeline.py`; add a Gate B coverage contract in `src/mfs/coverage.py` (advisory) + a freshness threshold in `configs/pipeline.yaml` (monthly cadence, ~45d).
5. Scoring: add TER as a **display column** in stage outputs and a **deterministic stage-2 tiebreaker** (lower TER wins ties) in `src/mfs/rank/stage2.py` — NOT a composite-weighted term (weight changes go through C1's backtest, not here).
6. Tests: a parser fixture (real AMFI sample → exact pins), the range-rejection path, the tiebreaker (equal composite → lower TER ranks higher), Gate B contract.
7. Migration: `uv run mfs db init`, then ingest one month and verify coverage.

**Acceptance:** `uv run mfs ingest ter` (new) populates `scheme_ter_monthly`;
`scheme_ter_monthly` coverage shows in Gate B; a stage-2 tie is broken by TER in
a test; full suite green.

---

## C3 — Finish the benchmark-fit cleanup (Energy re-fit + low-confidence review)

**What:** Stage 2 remapped Value (→NIFTY 500 TRI, R² 0.90) and Energy
(→NIFTY Infrastructure TRI, R² 0.81), but Energy was chosen before NIFTY
Commodities TRI was ingested, and several categories remain flagged
`benchmark_fit_low_confidence` (MNC ~0.77, Equity Savings ~0.78, Infrastructure
~0.78). Audit dim 6: "Category-level benchmark assignment fits poorly... alpha
there is mostly unexplained return, not skill."

**Why:** Alpha only means *skill* if the benchmark explains the fund's returns.
A poorly-fit benchmark turns market beta into fake alpha — and alpha is the
heaviest-weighted input. Every mis-fit category is a category whose rankings
can't be trusted.

**How (evidence procedure is in `docs/audit/benchmark_refit_2026-06.md`):**
1. NIFTY Commodities TRI is now ingested. Re-run the Energy refit:
   ```bash
   uv run python tools/benchmark_fit.py refit --category Energy
   ```
   If NIFTY Commodities TRI beats NIFTY Infrastructure TRI's 0.81 median R², remap Energy in `configs/benchmarks.csv` (dated `#` comment row) and note it in the memo.
2. Run `uv run python tools/benchmark_fit.py report` and, for each still-flagged low-confidence category, run `refit` against plausible candidate indices; remap where a clearly better fit exists, otherwise add a one-line documented acceptance in `configs/benchmarks.csv`.
3. After any remap: `uv run mfs build scheme-master` (re-stamps `benchmark_ticker`), then a recompute (`uv run mfs pipeline` or `uv run mfs compute phase1 --as-of <today>`), then re-verify `benchmark_fit.py report` shows the improvement.

**Acceptance:** `tools/benchmark_fit.py report` shows no remaining un-reviewed
low-confidence category — each is either remapped (R² improved, evidence in the
memo) or carries a documented acceptance. Full suite green.

---

## C4 — Verify active-share activation (scheduled ~2026-07-10)

**What:** The 0.10 stage-2 active-share weight was dead universe-wide (one month
of constituents). Stage 2 wired monthly constituents derivation + NIFTY 50/Bank
TRI trackers; it needs 3 monthly snapshots and activates automatically once
June holdings → June constituents land (~2026-07-10). The `rank_deep` banner
prints `n_matched_months=X/3` until then.

**Why:** Until verified live, we don't know the 0.10 weight behaves correctly
across categories (esp. Large Cap / BFSI, which were the gap the tracker
additions closed). An incorrectly-activating weight silently distorts stage 2.

**How:**
1. After the first pipeline run on/after ~2026-07-10, confirm the banner shows `3/3` and active_share is non-null for a healthy share of the stage-2 pool: `psql -d mfs -c "SELECT canonical_category, count(*) , count(active_share_median_1y) FROM computed_metrics m JOIN scheme_master s USING(scheme_code) WHERE as_of_date='<run>' GROUP BY 1 ORDER BY 1;"`
2. Specifically verify Large Cap and BFSI now receive active_share (they were null-forever before the tracker fix).
3. Confirm the D3 [REVIEW-ALIGNED] rule holds: a fund *missing* active_share once it's live draws the disclosure penalty (not renormalize-only). Spot-check one such fund in `stage2/coverage.csv`.
4. Sanity-check the reward direction in Large Cap (audit 2c flagged that high active-share in large-cap India often means small-cap drift, not skill) — if the top active-share large-caps are style-drifters, note it for C1's weight review.

**Acceptance:** a short note in `docs/audit/active_share_activation_<date>.md`
confirming 3/3 snapshots, non-null coverage incl. Large Cap/BFSI, correct
penalty behavior. No code change expected unless step 3/4 surfaces a defect.

**Needs the user:** nothing, but this is **time-gated** — it can't be completed before ~2026-07-10. Until then it stays open.

---

## C5 — Hybrid debt-sleeve: probe a real debt index, swap if available

**What:** The three synthetic hybrid benchmarks use the 91-day T-bill as the
debt sleeve, which one-sidedly understates the benchmark and inflates hybrid
alpha (audit 3/6; beat-rates 93-100%). Stage 2 made the bias **disclosed** on
every affected row (`benchmark_is_synthetic`) and chose PATH B (document, don't
ship an unverified scrape).

**Why:** 84 hybrid funds are scored against a benchmark that's too easy. The
disclosure is honest, but a real debt index would make hybrid alpha — and thus
hybrid rankings — actually meaningful.

**How (decision procedure is in `docs/audit/hybrid_debt_sleeve_spike.md`):**
1. Probe (network) whether a real NIFTY debt index TRI (Composite Debt / G-Sec /
   Short Duration) is fetchable from niftyindices.com without auth, per the memo's PATH-A check.
2. **If yes:** add it to the NSE TRI ingest map (`src/mfs/ingest/benchmarks.py`), ingest its history, and rebuild the synthetic sleeves in `src/mfs/ingest/synthetic_hybrid.py` to use `w_debt × debt_index_return` instead of the T-bill. Recompute; verify hybrid beat-rates fall to something realistic; keep the `benchmark_is_synthetic` flag if any modelling assumption remains.
3. **If no:** leave the disclosure permanent, tighten the memo's wording, and close this item as "accepted — no scrapeable debt index."

**Acceptance:** either the hybrid sleeves use a real debt index (verified by a
drop in hybrid beat-rates + a passing test pinning the new composition) **or**
the memo records a definitive "not scrapeable" with the disclosure kept. Full suite green.

**Needs the user:** nothing if the probe is decisive; surface to the user only if the debt index requires a paid/authenticated source (then it becomes a procurement decision like survivorship).

---

## Suggested order
C1 (backtest — kick it off first; it runs for hours in the background) →
C3 (benchmark fit — feeds C1's honesty) → C2 (TER) → C5 (hybrid probe) →
C4 (active-share — whenever the calendar allows, ≥2026-07-10).

C1 and C3 interact: re-fit benchmarks (C3) before trusting the backtest's
category ICs (C1), or note in C1 that Value/Energy ICs predate the remap.
