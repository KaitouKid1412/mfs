# Stage 2 — Full remediation plan

Covers everything from the 2026-06-10 audit not handled in Stage 1: 7 workstreams, ~80 tasks.
Produced by 7 workstream planners + 2 adversarial plan reviewers (conflict/feasibility + coverage
against all ~107 audit findings); all reviewer resolutions are already applied to the task text
below (marked `[REVIEW-...]`). Stage 1 (docs/phase6/stage1_urgent_fixes.md) must be complete,
including P-0 baseline commit and P-1 backup.

## Locked decisions (2026-06-11)

- **D1 — Signed alpha:** median over ALL windows (t-censor removed); `alpha_confidence` is a
  display column, never a filter.
- **D2 — Stage-1 nulls:** funds missing any required core metric are hard-dropped and listed in an
  "excluded — insufficient data" output.
- **D3 — Stage-2 disclosure nulls (PTR / style-drift / active-share):** soft-neutral — renormalize
  the missing metric's weight, apply a small fixed penalty calibrated ~= median observed PTR penalty
  (missing ~= average-bad, never better than disclosed-bad), flag "partial disclosure data".
  active_share joins the penalty set when it activates (~2026-07); renormalize-only until then.
- **D4 — Stage-3 overlap:** keep both funds, flag the breach with overlap %; no cross-category
  drops (restores the locked 2026-05-24 decision).

## Execution order

**Phase 2.0 — before the next pipeline run after Stage 1 (time-sensitive):**
F-2 (pipeline.py extraction — every later stage edit rebases onto it once, not continuously);
D1 + D2 (scheme_master diff-sync + history tables — every pipeline run before they land destroys
another universe snapshot); B5 + B6 in one release (stricter matcher BEFORE the new GROWTH
candidates enter); B8 (4xx translation).

**Phase 2.1 — parallel tracks** (the cross-workstream contract below is binding):
- *Compute track:* A1-1 first, then A1-2/3/4/8/9 in parallel; A1-5/6/7/11/12 ride along; C4 (absorbs
  B10) and B4 in this window.
- *Ingest track:* B1 -> B2/B9/B11/B12; B3, B13, B14, B15; F-4 -> F-5 -> C2 -> C3/C5.
- *Foundations track:* F-3, F-6, F-7, F-8 -> F-9, C1, C7.

**Phase 2.2 — the single coordinated recompute:** A1-10, gated on the full compute track PLUS the
universe-changers (B5, B6, D1) and the Stage-1 backfills, so ONE recompute covers the corrected
universe with Gate A passing honestly. A1-10 also deletes the archived 2026-06-08 partition.

**Phase 2.3 — re-rank sequence (strictly ordered, same files):**
A2-1 -> A2-2 -> A2-3 -> A2-4 (recalibrate penalty on the post-A1-10 partition) -> A2-5 -> A2-6 ->
A2-7 -> A2-8 -> A2-10 -> A2-11 -> A2-9 (pinned e2e last). B7 gate-tightening and D9 land here.

**Phase 2.4 — methodology, output, ops:** D3 -> D10, D4 -> D5, D6, D7, D8 (endpoint spike FIRST —
F-6's amfi_ter disposition waits on it); E1/E2/E3 -> E4 -> E5/E6, E7; F-10..F-15, F-14 last.

## Cross-workstream contract (binding for every task below)

- **Acceptance anchor:** wherever a task's acceptance command says `--as-of 2026-06-08`, substitute
  the latest post-A1-10 as_of. The 2026-06-08 partition is archived, contaminated, and deleted by A1-10.
- **liquidity_flag:** string enum `OK | HIGH | SEVERE` at 30/90 days-to-exit (E3's design; A2-6
  emits it; A2-9 and E4 pin the string values). The 60-day boolean is dead.
- **Artifact names:** `stage1/excluded.csv` (D2 hard-drops, with exclusion_reason),
  `missing_disclosures` column (D3 flags), `overlap_breaches.csv` (D4 flags),
  `passive_alternative.csv` (D7 verdicts).
- **exclusion_reason vocabulary:** `INSUFFICIENT_HISTORY | MISSING_CORE_METRIC:<name> | STALE_NAV |
  FILTER:<name>` — D2's history tables and E5's report sections both consume it.
- **Shared-file merge order:** `rank/filters.py`: A1-3 -> A2-1 -> A2-2 -> D2 · `rank/stage2.py` +
  `rank/score.py`: A2-4 -> A2-5 -> A2-6 -> E1 -> E3 -> D8 · `rank/shortlist.py`: A2-2 -> A2-5 ->
  A2-7 -> D2 -> D7 -> E1/E2 -> E4 -> F-10. CLI stage wiring: everything edits `mfs/pipeline.py`
  (post F-2), never cli.py.
- **force/--full semantics (B9=C5=C2):** one flag meaning re-download AND re-parse AND re-write.

## Conflict resolutions applied (changelog)

1. A2-6 vs E3 `liquidity_flag` -> E3's OK/HIGH/SEVERE tiering wins.
2. B10 vs C4 fund_log_returns -> merged into C4 (same-transaction incremental refresh + Gate A check).
3. Urgent-3 vs B2 statement-date -> Urgent-3 builds the mechanism; B2 re-scoped to coverage extension.
4. B9 vs C5 force semantics -> unified (see contract).
5. C2 vs F-5 run_all -> F-5 extracts the shared core first; C2 threads the shared helper.
6. F-6 vs D8 amfi_ter -> D8's endpoint spike decides; F-6 excludes amfi_ter.py until then.
7. B7 vs D9 constituents lag -> D9 owns the key (75d with the derive cadence).
8. A2-4 active_share deviation from locked D3 -> aligned (penalty applies once it activates).
9. Stage-1 step-4 partition purge -> archive now, delete in A1-10; acceptance re-anchored.
10. A1-10 gating expanded to universe-changers; A2-4 penalty recalibrated post-A1-10; D3 also
    depends on A1-4 (epoch) — all applied to task text. C6's RBI worker bump dropped (politeness).
11. Cross-reference IDs fixed (signed alpha = A1-2; hard-drop = A2-2). C4 citation label fixed (dim 8).

## New tasks added by the coverage review

A1-11 (aum_impact ADV-coverage guard), A1-12 (point-in-time as_of bounds), A2-10 (PTR category
exemption), A2-11 (deterministic tie-break), B13 (partition-shrinkage guard), B14 (PTR
plausibility/unit-flip tripwire), B15 (Gate B ERROR status), F-15 (ToS/provenance inventory) —
plus extensions to A1-6 (Sortino/IR pins), D9 (NIFTY 50/Bank TRI trackers), F-6, F-9 (pip-audit).

## Consciously deferred (accepted risks — recorded, not forgotten)

- Holdings adapter fixture coverage stays 3/46: B3's weight gates + B2's statement-date validation
  are the runtime tripwires; revisit if a silent parse regression slips through them.
- Explicit segregated-portfolio universe flag: handled de facto by A1-1 (zero-NAV removal) + A2-2
  (hard-drop); monitor per A1's risk note.
- Stage-1 minimum-cohort z guard: mitigated at display level by E1 cohort-size columns + D7
  index-verdicts; full guard deferred to post-D3-backtest weight work.
- Advisory lock for standalone CLI commands: doc note in F-14; transactional writes bound the damage.
- ISIN->base_fund_id uniqueness assertion (Nippon variants); benchmarks.csv index-rename detection
  (loud equity-ingest failures compensate); per-AMC consecutive-failure alerting (B12 ledger +
  Gate B attribution compensate); min-investment/closed-to-inflow execution metadata (no clean
  auto-scrape source — out per the no-manual-entry invariant).


## Workstream A1 — Metric/compute correctness

Fixes every confirmed compute-layer defect so stored metrics mean what their names say: signed alpha with a separate confidence column (D1), zero-NAV rejection at ingest plus a non-finite guard in the log-return path, a per-scheme staleness gate against as_of, one trailing-epoch convention for all '3y'/'5y' rolling metrics, config wiring for the history gate, ISIN-based active-share matching before the metric activates (~2026-07), dead-code/docstring truth fixes (returns, aum_impact), and escalation of silently-swallowed per-scheme compute failures. Shape: nine code tasks each independently testable, then one coordinated full recompute (A1-10) because D1 + zero-NAV cleanup + staleness + epoch unification change every stored metric — ranking-workstream changes (D2/D3/D4) should re-rank exactly once, after that recompute. All file:line details verified against current code and the live DB today (211,859 zero-NAV rows over 559 schemes; min stored alpha +0.0113 over 399 funds confirmed).

### A1-1 [M] Zero-NAV: reject at ingest, CHECK constraint, non-finite guard in alignment, clean DB, refresh log-return cache

*Files:* `src/mfs/ingest/amfi_nav.py:119-124; src/mfs/schemas.py:9-12; src/mfs/db/schema.sql (nav_daily, after line 16); src/mfs/compute/alignment.py:16-18,90-96; src/mfs/db/writers.py:396; src/mfs/cli.py (db_app subcommand)`

**Change:** Both ingest-reject AND compute-guard (fail-fast invariant = primary at ingest; compute guard = belt for any legacy/edge rows). (1) Ingest: in src/mfs/ingest/amfi_nav.py parse_amfi_text, extend the skip at line 123 from `if nav is None or nav_date is None: continue` to also skip `nav <= 0`; count skipped rows and emit one `log.warning('amfi_nav.nonpositive_nav_skipped', n=...)` per parse. Update src/mfs/schemas.py:9-12 NavDaily to `nav: float = Field(gt=0)` (currently defined but unwired — keeps the documented contract true). (2) Schema: in src/mfs/db/schema.sql add idempotent `ALTER TABLE nav_daily DROP CONSTRAINT IF EXISTS chk_nav_positive; ALTER TABLE nav_daily ADD CONSTRAINT chk_nav_positive CHECK (nav > 0);` (mirror the chk_scheme_aum_source_amfi pattern at schema.sql:185). (3) Compute guard: in src/mfs/compute/alignment.py align_scheme, after the log-return block at lines 90-96, null out non-finite values: for each of fund_log_ret/bench_log_ret/rate_daily_log apply `pl.when(pl.col(c).is_finite()).then(pl.col(c)).otherwise(None)` — polars drop_nulls in sortino.py:20/capture.py:37/info_ratio.py:22/alpha.py:49-51 does NOT drop NaN/inf, so this converts poison to dropped rows. Also fix the stale alignment.py:16-18 docstring claim that log returns are read from the fund_log_returns cache (no compute code reads it; queries.fund_log_returns has zero compute callers). (4) Data cleanup (engineer runs, ordered): `psql -d mfs -c "DELETE FROM nav_daily WHERE nav <= 0"` (expect ~211,859 rows over 559 schemes — re-verify count first; pg_dump the table beforehand), then apply the CHECK + new columns via `uv run mfs db init`, then rebuild the cache via writers.refresh_fund_log_returns_all() (src/mfs/db/writers.py:396; expose as a `mfs db refresh-log-returns` CLI subcommand — currently no entry point exists).

**Tests:** tests/test_amfi_nav_parser.py: a NAVAll text fixture containing a `0.00000` and a negative NAV row parses without those rows and reports the skip count. New tests/test_alignment.py: synthetic aligned frame with one nav=0 row → fund_log_ret is null (not inf/NaN) at and after that row; rolling_sortino/rolling_capture on the same frame return finite values (reproduces the audit's clean-0.0485→NaN case and asserts it no longer NaNs).

**Acceptance:** `uv run pytest tests/test_amfi_nav_parser.py tests/test_alignment.py` green. After cleanup: `psql -d mfs -c "SELECT COUNT(*) FROM nav_daily WHERE nav <= 0"` → 0; an INSERT of a nav=0 row fails with chk_nav_positive violation.

*Audit findings:* 2a zero-NAV NaN-poison (alignment.py:92, confirmed); 1a 211,859 zero NAVs / NaN-stored-as-GOOD (confirmed)

### A1-2 [M] D1: signed alpha (median over ALL windows) + alpha_confidence display column end-to-end

*Files:* `src/mfs/compute/alpha.py:1-15,30,58-94; src/mfs/compute/orchestrator.py:84-85,117-133; src/mfs/db/schema.sql:91-99; src/mfs/db/writers.py:154-165; src/mfs/db/queries.py:126-137; src/mfs/rank/shortlist.py:23-51`

**Change:** In src/mfs/compute/alpha.py rolling_alpha_beta_r2: remove the t-stat gate at lines 76-78 — append every window's alpha_ann and tstat unconditionally; alpha_ann = median over ALL windows (signed), alpha_tstat = median t-stat over ALL windows. Add new output key `alpha_confidence` = share of windows with |tstat| >= 1.0 (None when no windows); rename ALPHA_TSTAT_FILTER_MIN (line 30) to ALPHA_CONFIDENCE_TSTAT_MIN and update its comment — it no longer filters anything. Rewrite the module docstring (lines 1-15): delete the false 'drops out of ranking via the existing not-null filter' claim (lines 12-14) and document signed-alpha + confidence semantics; state explicitly that alpha_confidence is display-only, never a filter (locked D1). Plumb the column: src/mfs/compute/orchestrator.py — add "alpha_confidence": None to the row dict (after alpha_3y_tstat, line 85) and `alpha_confidence=abr["alpha_confidence"]` in row.update (lines 117-133); src/mfs/db/schema.sql — `ALTER TABLE computed_metrics ADD COLUMN IF NOT EXISTS alpha_confidence DOUBLE PRECISION;` next to the Phase-2 ALTERs (lines 91-99), applied via `uv run mfs db init`; src/mfs/db/writers.py:154-165 _COMPUTED_METRICS_COLS — insert "alpha_confidence" after "alpha_3y_tstat"; src/mfs/db/queries.py:126-137 _COMPUTED_METRICS_COLS — same insert (the two lists must stay identical); src/mfs/rank/shortlist.py:23-51 STAGE1_OUTPUT_COLS — add "alpha_confidence" after "alpha_3y_annualized" so it appears in stage1 CSVs/mf_report (stage2's frame inherits computed_metrics columns; verify carry-through to stage2 output and add to its column list only if an explicit one exists). No filtering or scoring change on alpha_confidence anywhere.

**Tests:** tests/test_metrics_math.py: (a) NEW negative-alpha golden fixture — synthetic fund with constant negative daily drift vs benchmark (mirror of test_single_window_alpha_recovers_drift_noise_free at lines 57-63, drift -0.0001) → rolling_alpha_beta_r2 alpha_ann < 0 and within 1e-3 of the analytic value; (b) REWRITE test_rolling_alpha_filters_insignificant_tstat_windows (lines 121-131): pure-noise fund now returns alpha_ann NOT None and ≈ 0, with low alpha_confidence (< 0.5); (c) alpha_confidence bounds: strong-drift fixture (lines 138-146) → alpha_confidence == 1.0; assert 0 <= alpha_confidence <= 1; (d) test_rolling_alpha_accepts_strongly_positive_drift stays green unchanged.

**Acceptance:** `uv run pytest tests/test_metrics_math.py` green. After A1-10 recompute: `psql -d mfs -c "SELECT MIN(alpha_3y_annualized), COUNT(*) FILTER (WHERE alpha_3y_annualized < 0), COUNT(*) FILTER (WHERE alpha_confidence IS NULL AND alpha_3y_annualized IS NOT NULL) FROM computed_metrics WHERE as_of_date='<new_as_of>'"` → min < 0, negative count > 0, third count = 0 (today: min +0.0113, 0 negatives).

*Audit findings:* 2a/2c/6 alpha positive-by-construction (alpha.py:30,76, confirmed); 6 null-alpha docstring contradiction (confirmed)

### A1-3 [M] Per-scheme as_of staleness gate: flag STALE when last NAV lags as_of beyond N trading days

*Files:* `src/mfs/compute/orchestrator.py:37-57,65-75; src/mfs/config.py:45-47; configs/pipeline.yaml (quality block); src/mfs/rank/filters.py:49-50; new tests/test_orchestrator_quality.py`

**Change:** src/mfs/compute/orchestrator.py: pass as_of into the quality check — extend _data_quality_flag(aligned, min_years=...) (line 37) with `as_of: date` and `max_nav_lag_bdays: int`, and call it with as_of from compute_phase1_for_scheme (line 75). Gate: take master-calendar dates <= as_of (alignment.master_calendar() is already loaded at lines 48-50); if the scheme's max NAV date < the N-th-from-last calendar date, return new flag "STALE" before the GOOD/POOR checks. N: add `max_scheme_nav_lag_bdays: int = 10` to QualityConfig (src/mfs/config.py:45-47) and the configs/pipeline.yaml quality block — 10 trading days = 2x freshness.max_nav_lag_bdays (config.py:53), tolerating per-scheme publication lag while catching anything dead for weeks (live offenders lag months-to-years: scheme 118929 last NAV 2022-01-14 ranked #37 in ELSS; 5 ranked stage1 schemes with last NAV < 2026-06 per verifier). src/mfs/rank/filters.py: add `df = df.filter(pl.col("data_quality_flag") != "STALE")` next to the INSUFFICIENT_HISTORY/POOR drops (lines 49-50) and mention STALE in the module docstring. Keep STALE rows in computed_metrics (visible, just unranked) — consistent with D2's excluded-section reporting, which the ranking workstream owns.

**Tests:** New tests/test_orchestrator_quality.py (orchestrator currently has zero tests — audit dim 7): monkeypatch alignment.master_calendar to a synthetic business-day calendar; aligned frame ending 30 trading days before as_of → "STALE"; ending 3 trading days before → "GOOD" (3y+ span, <5% missing); exactly-N-days boundary case. tests/test_ranking.py: a row with data_quality_flag="STALE" is dropped by apply_hard_filters.

**Acceptance:** `uv run pytest tests/test_orchestrator_quality.py tests/test_ranking.py` green. After A1-10 recompute: `psql -d mfs -c "SELECT cm.scheme_code FROM computed_metrics cm JOIN (SELECT scheme_code, MAX(nav_date) mx FROM nav_daily GROUP BY 1) n USING (scheme_code) WHERE cm.as_of_date='<new_as_of>' AND cm.data_quality_flag='GOOD' AND n.mx < '<new_as_of>'::date - 20"` → 0 rows; schemes 118929/119518/147864/119960/135654 flagged STALE.

*Audit findings:* 2a dead/stale funds ranked fresh (orchestrator.py:46, confirmed)

### A1-4 [M] Epoch consistency: all rolling metrics use trailing windows whose endpoints fall in the trailing 3y/5y

*Files:* `src/mfs/compute/rolling.py; src/mfs/compute/alpha.py:~62; src/mfs/compute/sortino.py:~27; src/mfs/compute/info_ratio.py:~29; src/mfs/compute/capture.py:~44; tests/test_metrics_math.py`

**Change:** Adopt the recommended convention: every '<W>y' metric is the median over rolling W-year windows whose ENDPOINTS fall in the trailing ~W years — what returns.py already does (lines 29-31), while alpha/sortino/IR/capture walk all windows since inception (iter_windows starts at index.min()+window, rolling.py:42-45). Add a shared helper in src/mfs/compute/rolling.py: `def trim_to_trailing_endpoints(pdf, window_years, slack_years=0.05)` returning pdf filtered to index >= index.max() - Timedelta(days=int(365.25*(2*window_years + slack_years))); after iter_windows' min+window start, endpoints then span only the trailing window_years+slack. Apply it immediately before the iter_windows loop in src/mfs/compute/alpha.py (before line 62), sortino.py (before line 27), info_ratio.py (before line 29), capture.py (before line 44). Document the convention in rolling.py's module docstring. State in the PR: beta_3y/r_squared_3y medians and beta_3y_std/r_squared_3y_mean (hence style_drift_3y) become trailing-3y measures — which finally matches their _3y names; old funds' alpha is no longer diluted by decade-old windows (~540 windows → ~159 for a 2013-vintage fund).

**Tests:** tests/test_metrics_math.py: regime-change fixture — 8y synthetic series with strong positive fund drift in years 1-5 and zero excess drift in years 6-8; with the trim, rolling_alpha_beta_r2 (3y) alpha_ann ≈ 0 (stale outperformance no longer contributes; assert below a small bound where the untrimmed behavior reported a large positive median). Unit test on trim_to_trailing_endpoints: for a 10y daily frame, iter_windows endpoints over the trimmed frame span <= window_years + slack + one step. Existing alpha/sortino tests use ~3-4y fixtures where the trim is a no-op — must stay green.

**Acceptance:** `uv run pytest tests/test_metrics_math.py` green including the regime-change test. Spot check after A1-10: `uv run mfs audit-scheme <2013-vintage scheme>` window counts ~159 (one 3y span of weekly endpoints), not ~540.

*Audit findings:* 2a/6 epoch inconsistency (returns.py:30, rolling.py:42-45, assessment — unify per scope decision)

### A1-5 [S] Wire quality.min_history_years_for_ranking into _data_quality_flag (preserve effective 3y gate) — depends on: A1-3

*Files:* `src/mfs/compute/orchestrator.py:37,75; src/mfs/config.py:45-47; configs/pipeline.yaml quality block`

**Change:** src/mfs/compute/orchestrator.py: in compute_phase1_for_scheme, call _data_quality_flag with min_years=get_pipeline_config().quality.min_history_years_for_ranking (today line 75 passes nothing, so the 3.0 default at line 37 silently wins). CRITICAL: configs/pipeline.yaml says 5 and the QualityConfig default (src/mfs/config.py:46) is 5, while live behavior is 3 — wiring without changing the YAML would silently shrink the GOOD universe (465 GOOD funds match the 3y gate per audit). In the same commit set pipeline.yaml quality.min_history_years_for_ranking: 3 and the QualityConfig default to 3, with a comment that 3 is the live deliberate gate. Any future move to 5y is a separate product decision.

**Tests:** tests/test_orchestrator_quality.py (from A1-3): monkeypatch get_pipeline_config to min_history_years_for_ranking=4 → a 3.5y-span frame returns INSUFFICIENT_HISTORY; with 3 → GOOD. Asserts the config is actually honored.

**Acceptance:** `uv run pytest tests/test_orchestrator_quality.py` green. After A1-10: GOOD-count for the new as_of in the same ballpark as before modulo STALE reclassifications: `psql -d mfs -c "SELECT data_quality_flag, COUNT(*) FROM computed_metrics WHERE as_of_date='<new_as_of>' GROUP BY 1"`.

*Audit findings:* 2a dead config min_history_years (orchestrator.py:37); top-defects #8 dead config

### A1-6 [S] returns.py: fix docstring formula, delete dead n_days_year/cfg, add deterministic golden pin

*Files:* `src/mfs/compute/returns.py:1-7,21,66,78; tests/test_metrics_math.py`

**Change:** src/mfs/compute/returns.py: rewrite the module docstring (lines 1-7) — the code computes calendar CAGR R = (NAV_end/NAV_start)^(1/years) - 1 with years = (d_end - d_start).days/365.25 (lines 79-82), not the documented (252/window_length_days) exponent. Delete dead `n_days_year = cfg.returns.trading_days_per_year` (line 66), the now-unused `cfg = get_pipeline_config()` (line 21), and the misleading 'Annualize on trading-day count' comment (line 78); drop the get_pipeline_config import if unused. Pure doc/dead-code change — zero behavior delta. [REVIEW-EXTENDED] Also add hand-computed numeric pins for Sortino and IR on a small synthetic series (catching a missing sqrt(252) or wrong ddof — today they are sign-only assertions).

**Tests:** tests/test_metrics_math.py: alongside the existing 5x-wide range assertion (line ~82 asserts only 0.07 < median < 0.35), add a deterministic pin: NAV series compounding exactly 10%/yr on calendar days → rolling_distribution median == 0.10 within 1e-6 (audit dim-7 recommendation; guards future formula swaps).

**Acceptance:** `uv run pytest tests/test_metrics_math.py` green; `grep -n n_days_year src/mfs/compute/returns.py` → no matches; ret_3y for any scheme unchanged pre/post (`uv run mfs audit-scheme <code>` diff).

*Audit findings:* 2a returns docstring/dead n_days_year (returns.py:4,66); dim-7 weak returns assertion

### A1-7 [S] aum_impact: remove dead top-10 machinery; code and docstring agree (metric = worst single holding)

*Files:* `src/mfs/compute/aum_impact.py:11-13,36,65-110; tests/test_aum_impact.py`

**Change:** src/mfs/compute/aum_impact.py:108-110 — `days_to_exit.sort(reverse=True); worst = days_to_exit[:top_n]; return max(worst)` equals max(days_to_exit); TOP_N_ILLIQUID (line 36) and the top_n parameter (line 70) never affect the result. Behavior-preserving fix (chosen over mean-of-top-10, which would change the stored metric and interact with the rank-side penalty recalibration owned by the ranking workstream): replace lines 108-110 with `return max(days_to_exit)`, delete TOP_N_ILLIQUID and the top_n parameter, rewrite the module docstring (lines 11-13) to 'reports the worst-case (max) days-to-exit across all ADV-resolved equity holdings'. Grep 'top 10\|TOP_N_ILLIQUID' under src/ and docs/ and fix any repeats of the claim.

**Tests:** tests/test_aum_impact.py: existing 2.0-days golden pin stays green; add a 12-holding fixture where the global max days-to-exit sits at position 11 by descending order → result equals the global max (regression against reintroducing a head-slice).

**Acceptance:** `uv run pytest tests/test_aum_impact.py` green; `grep -rn TOP_N_ILLIQUID src/` → no matches; stored aum_impact_cost_days values identical pre/post.

*Audit findings:* 2a aum_impact dead top-10 (aum_impact.py:108, unverified-low)

### A1-8 [M] active_share: ISIN-first matching with canonicalized-name fallback

*Files:* `src/mfs/compute/active_share.py:11-15,52,57-67,70-114; src/mfs/db/queries.py:181; tests/test_active_share.py`

**Change:** Both sides now have 100% ISIN (verified live today: holdings_monthly 95,988/95,988; index_constituents_monthly 2,285/2,285; queries.holdings_for_scheme and constituents_for_ticker already return isin). src/mfs/compute/active_share.py: in _normalize_equity_weights (lines 70-92) and _normalize_constituent_weights (lines 95-114), key each row on `isin` when non-null/non-empty, else on normalize_name(security_name) — same dict, ISIN and name keys coexist; mismatch classes like 'Larsen & Toubro' vs 'Larsen and Toubro' (currently double-counted |w_f|+|w_b|, biasing active share UP) collapse via ISIN. For the name fallback: canonicalize '&' → ' and ' in normalize_name (lines 57-67) before _PUNCT_RE strips it, and drop bare 'co'/'inc' tokens from _LTD_RE (line 52) to stop over-merges. Update the now-false implementation note (lines 11-15: 'factsheets do NOT print ISINs' — true in the Phase-2.2 factsheet era, false post-Phase-5 SEBI-Excel ingestion) and the stale claim in src/mfs/db/queries.py:181. Must merge before ~2026-07 when the third monthly constituent snapshot activates the metric (MIN_SNAPSHOTS_FOR_MEDIAN=3, active_share.py:50).

**Tests:** tests/test_active_share.py: (a) identical portfolios under different spellings but same ISINs ('Larsen & Toubro Ltd' vs 'Larsen and Toubro Limited') → active_share_one_month == 0.0 (currently 100); (b) ISIN-present rows match despite entirely different names; (c) isin=None rows still fall back to name matching (extend existing name-based fixtures with isin=None so they stay valid); (d) mixed portfolio: one ISIN match + one name-fallback match in the same computation.

**Acceptance:** `uv run pytest tests/test_active_share.py` green, including the &/and case returning 0.0.

*Audit findings:* 2a active_share name-matching despite 100% ISIN (active_share.py:91)

### A1-9 [M] Orchestrator: escalate per-scheme exception swallowing to counted, reported skips with a halt threshold

*Files:* `src/mfs/compute/orchestrator.py:164-185,282-302; src/mfs/config.py:45-47; configs/pipeline.yaml quality block; tests/test_orchestrator_quality.py`

**Change:** src/mfs/compute/orchestrator.py: run_phase1 (lines 173-175) and run_phase2 (lines 293-295) log.warning and continue — a systemic breakage (DB type change, benchmark corruption) silently shrinks the universe. Change both loops to collect failures as [(scheme_code, repr(e))], keep the per-scheme warning, and after the loop: log.error a summary (n_failed, n_attempted, first 10 codes) and raise PipelineError (src/mfs/errors.py:11) when n_failed/n_attempted > threshold. Add `max_scheme_compute_failure_rate: float = 0.01` to QualityConfig (src/mfs/config.py:45-47) and the pipeline.yaml quality block — 1%: a handful of genuinely broken schemes still skip-and-report (no-half-data invariant: skip the scheme, never store partial junk), anything systemic halts (fail-fast invariant). Include the failed codes in the raised message so /run-pipeline surfaces them.

**Tests:** tests/test_orchestrator_quality.py: monkeypatch compute_phase1_for_scheme to raise for k of n schemes and q.scheme_master to a synthetic eligible frame (no DB): k/n above threshold → PipelineError naming failed codes; below threshold → run completes, returned frame has n-k rows, summary records k. Mirror one case for run_phase2 (monkeypatch q.computed_metrics_at + compute_phase2_for_scheme).

**Acceptance:** `uv run pytest tests/test_orchestrator_quality.py` green; with compute_phase1_for_scheme forced to always raise, `uv run mfs compute phase1` exits non-zero with the failure summary instead of writing an empty/partial partition.

*Audit findings:* dim-7 untested exception-swallow path (orchestrator.py:173-175,293-295); 'wrongness is silent' systemic pattern

### A1-10 [M] Full recompute of computed_metrics on the corrected stack + verification battery — depends on: A1-1, A1-2, A1-3, A1-4, A1-5, A1-8, A1-9, A1-11, A1-12, B5, B6, D1, STAGE1-U1, STAGE1-U2, STAGE1-U3

*Files:* `Operational — uses src/mfs/cli.py compute commands, configs/pipeline.yaml pipeline_version, and the A1-1 cleanup; no new code`

**Change:** [REVIEW-EXPANDED gating] Recompute runs only after ALL universe-changing tasks (B5 matcher, B6 option-parser, D1 master diff-sync) and the Stage-1 backfills, so ONE recompute covers the corrected universe with Gate A passing honestly (never skip_freshness). This recompute also retires the archived 2026-06-08 partition: delete it here. D1 + zero-NAV cleanup + staleness + epoch trim + config wiring change every stored metric, so a coordinated recompute is mandatory before any re-ranking. Engineer runs, in order, AFTER A1-1..A1-5, A1-8, A1-9 merge: (1) A1-1 data cleanup (pg_dump nav_daily; DELETE nav<=0; `uv run mfs db init` for the CHECK + alpha_confidence column; refresh_fund_log_returns_all via the new CLI subcommand). (2) `uv run mfs compute phase1 --as-of <run_date>` then `uv run mfs compute phase2 --as-of <run_date>` (or let `mfs rank-deep`/the pipeline drive phase2 over the Stage-1 pool, matching how the next production run executes). Freshness Gate A must pass — if NAV/benchmarks are stale that day, fix ingestion first per the fail-fast invariant; do NOT use skip_freshness. Bump pipeline_version in configs/pipeline.yaml so old censored-alpha partitions are distinguishable. (3) Verification battery (psql, read-only): [a] `SELECT MIN(alpha_3y_annualized), COUNT(*) FILTER (WHERE alpha_3y_annualized < 0) FROM computed_metrics WHERE as_of_date='<d>'` → min < 0, count > 0; [b] alpha_confidence non-null wherever alpha non-null and all values in [0,1]; [c] `SELECT COUNT(*) FROM computed_metrics WHERE as_of_date='<d>' AND (sortino_3y='NaN'::float8 OR info_ratio_3y='NaN'::float8 OR capture_efficiency='NaN'::float8)` → 0; [d] A1-3 staleness query → 0 rows, and side-pocket schemes 148265/148273/148274 absent from the partition (their all-zero NAV history was deleted); [e] data_quality_flag histogram vs the 2026-06-08 run recorded in the PR. Hand the new partition to the ranking workstreams (D2/D3/D4) for the single re-rank.

**Tests:** None new — per-task tests cover the code; this task's verification is the psql battery above.

**Acceptance:** All five psql checks pass on the new as_of partition; `uv run mfs rank` completes on it without error; partition row count within ~5% of the prior run minus deleted all-zero-NAV schemes and STALE reclassifications (any larger delta explained in the PR).

*Audit findings:* Recompute implication of D1 + zero-NAV + staleness + epoch (all confirmed 2a findings)

### A1-11 [S] aum_impact ADV-coverage guard: null the metric when unresolved weight >20%

*Files:* `src/mfs/compute/aum_impact.py:100-102`

**Change:** Compute adv_unresolved_pct = total portfolio weight of holdings with no NSE EQ-series ADV match. If >20%, set aum_impact_cost_days NULL (metric is meaningless — e.g. ICICI Pru US Bluechip computes liquidity over <5% of weight); always emit adv_unresolved_pct as a column so stage 2/report can show it.

**Tests:** Synthetic portfolio with 97% unresolved weight -> NULL + pct=0.97; fully resolved -> unchanged.

**Acceptance:** Post-A1-10: international FoF-style funds show NULL aum_impact + high adv_unresolved_pct instead of an optimistic days-to-exit.

*Audit findings:* 1a aum_impact silently ignores ADV-less holdings [medium/confirmed-evidence]

### A1-12 [S] Point-in-time as_of bounds for PTR/holdings reads in compute + shortlist AUM map

*Files:* `src/mfs/compute/orchestrator.py:235-245, src/mfs/rank/shortlist.py (_build_aum_map)`

**Change:** Mirror the existing AUM on_or_before=as_of guard (orchestrator.py:241) for ptr_latest and holdings month selection in compute_phase2_for_scheme; pass on_or_before=as_of into shortlist._build_aum_map. Without this, D2 history tables and the D3 backtest are dishonest for any historical as_of.

**Tests:** Fixture with a PTR row dated AFTER as_of -> ignored; holdings month > as_of -> ignored.

**Acceptance:** `mfs rank --as-of <past date>` provably reads only data <= that date (add a unit asserting the SQL bound).

*Audit findings:* 6 point-in-time discipline inconsistent [medium]

**Workstream risks:**

- Universe and ordering shift: signed alpha + epoch trim + staleness flags will materially reorder every category, remove ~5 stale ranked funds and the 3 side-pocket schemes; old as_of partitions remain positively-censored — never mix partitions across the change (pipeline_version bump in A1-10 makes them distinguishable).
- D2 interplay: signed alpha makes the cohort include negative alphas, but ~54-66 funds still carry null alpha (short history). The Stage-1 hard-drop for missing core metrics (D2, ranking workstream) must land on the SAME recompute/re-rank as D1 — otherwise null-alpha funds keep category-average (z=0) credit against a now-signed cohort, which is strictly worse than today.
- Epoch trim shrinks window counts for old funds (~540 → ~159 for 3y weekly): medians get noisier, and beta_3y_std/style_drift_3y become trailing-3y measures — Stage-2 style-drift penalty distributions will shift; flag to the D3 calibration owner before the joint re-rank.
- min_obs interplay after the trim: iter_windows' total-length precheck (rolling.py:32) runs on the trimmed frame — verify funds with exactly ~3y history (one window) still produce metrics where they did before; the existing short-fixture tests guard this.
- DELETE of ~211,859 nav rows is irreversible in the DB (raw NAVAll archives on disk allow re-derivation); pg_dump nav_daily first. The new CHECK also makes any future upstream zero-NAV publication fail the ingest transaction loudly — intended fail-fast, but worth a line in the runbook.
- Side-pocket/segregated schemes lose their accidental NaN-based exclusion once zero NAVs are gone; explicit universe exclusion of segregated portfolios is scheme_master scope (another workstream) — until it lands, watch the new run for segregated schemes appearing in rankings.
- active_share ISIN matching cannot be validated against production until a third constituent month lands (~2026-07); unit tests are the only guard, and the missing NIFTY 50 TRI / Bank TRI constituents (audit 2c) are an ingestion gap outside A1 that will make the weight activate unevenly.
- The 10-business-day staleness threshold may STALE-flag legitimately suspended funds or schemes during AMFI publication outages; it is config-exposed (quality.max_scheme_nav_lag_bdays) precisely so it can be tuned without code change.

**Ordering notes:** A1-1 first (zero NAVs poison every metric and the cleanup changes inputs to everything downstream). A1-2, A1-3, A1-4, A1-8, A1-9 are mutually independent and can run in parallel after/alongside A1-1; A1-5 rides on A1-3's new test file; A1-6 and A1-7 are anytime S-tasks. A1-8 must merge before ~2026-07, when the third constituent month activates active_share. A1-10 is the synchronization point: run exactly once after all A1 code merges, and BEFORE the ranking workstream's D2/D3/D4 re-rank — re-ranking against old censored-alpha partitions would bake the bias back in, and D2's hard-drop must ship with the same release as signed alpha (see risks). Cross-workstream: the urgent NAV-gap backfill and T-bill fix (audit urgent items 1-2, operational workstream) must complete before A1-10's recompute date so freshness Gate A passes honestly — A1-10 must not be run with skip_freshness.


## Workstream A2 — Ranking/scoring correctness

Makes the three-stage ranker honest about missing data and restores the locked overlap decision. Stage 1: funds missing core metrics are hard-dropped into a visible 'excluded — insufficient data' artifact (D2) instead of silently scoring category-average; bonus-option dedupe moves before z-scoring. Stage 2: missing disclosure metrics (PTR/style-drift/AUM-impact/active-share) stop being fatal — weights renormalize, a calibrated fixed penalty (0.025 = median observed non-zero PTR penalty) applies, and funds are flagged 'partial disclosure' (D3); stage-1 full-universe z-scores are carried instead of re-z-scored in tiny pools; the AUM-impact penalty becomes a log ramp that discriminates 10 vs 924 days-to-exit. Stage 3: iterative cross-category drops are replaced with keep-both + breach flags (D4), so no category is ever wiped. Dead config is reconciled with yaml as single source of truth. A pinned end-to-end rank_deep fixture test locks all of it down.

### A2-1 [M] Config single-source-of-truth: wire filters.py to pipeline.yaml, delete dead keys, fix weight-sum comments

*Files:* `configs/pipeline.yaml:44-49,53-54,64-68,93-94; configs/category_thresholds.yaml:1-33; src/mfs/config.py:68-77,129; src/mfs/rank/filters.py:13,26-29,42,55-76; docs/phase4/staged_pipeline_refactor_plan.md:184; .claude/commands/run-pipeline.md:131,144`

**Change:** DECISION: yaml becomes the single source of truth with the CURRENT relaxed values; dead keys are deleted; the 0.95 comment is fixed to 0.85 (do NOT renormalize weights — z-composites are scale-invariant within a cohort, and A2-4 introduces per-row renormalization that uses the actual present-weight sum; silently rescaling to 0.95 would change scores with no decision behind it). Concretely: (1) configs/pipeline.yaml:44-49 filters block — set capture_efficiency_min: 0.50, info_ratio_3y_min: -1.00, r_squared_band: [0.40, 1.00], add beta_band: [0.30, 1.70] (the current code floors from filters.py:26-29). (2) src/mfs/config.py:68-73 FiltersConfig — update stale defaults (1.15 / 0.5 / (0.70,0.90)) to the same relaxed values and add beta_band: tuple[float,float] = (0.30, 1.70). (3) src/mfs/rank/filters.py — delete module constants at lines 26-29; apply_hard_filters reads cfg = get_pipeline_config() and uses cfg.filters.capture_efficiency_min / info_ratio_3y_min / r_squared_band / beta_band in the four filters at lines 55-76; update the module docstring line 13 ('Thresholds live in code (not configurable)'). (4) Delete the beta_bands block from configs/category_thresholds.yaml:1-33 (verified: no code reads it; only rankable_categories is consumed via get_thresholds). (5) Delete output: top_n_per_category from pipeline.yaml:93-94 and OutputConfig at config.py:76-77 + its reference in PipelineConfig:129 (verified never read; pool_size/final_size CLI flags govern). (6) Fix pipeline.yaml:66 comment 'sum to 0.95' -> 'sum to 0.85' and docs/phase4/staged_pipeline_refactor_plan.md:184 same claim; fix pipeline.yaml:53-54 stage-1 comment ('all eight z-scores are available for every fund...') to reference the D2 core-metric gate once A2-2 lands. (7) Update .claude/commands/run-pipeline.md:131 and :144 which state floors live in code not yaml.

**Tests:** tests/test_ranking.py: existing test_filters_drop_catastrophic_floors / test_filters_keep_marginal_metric_schemes must still pass unchanged (values identical, source moved). Add test_filters_read_from_config: build a PipelineConfig with a tightened capture_efficiency_min (monkeypatch mfs.rank.filters.get_pipeline_config or clear the lru_cache with a temp yaml) and assert a fund passing the relaxed floor is now dropped. Add a config test asserting FiltersConfig defaults equal the yaml values (drift guard).

**Acceptance:** uv run pytest tests/test_ranking.py -q passes; grep -rn 'beta_bands\|top_n_per_category' configs/ src/mfs/config.py returns no hits (except shortlist's unrelated local helper _top_n_per_category); grep -n 'CAPTURE_EFFICIENCY_FLOOR' src/mfs/rank/filters.py returns nothing; grep -n '0.95' configs/pipeline.yaml returns nothing.

*Audit findings:* 2b dead-config (filters.py:26-29), 2c dead/contradictory threshold config, 2b weight-sum comment drift (0.95 vs 0.85)

### A2-2 [M] D2: hard-drop funds missing core Stage-1 metrics + 'excluded — insufficient data' output — depends on: A1-2, A2-1

*Files:* `src/mfs/rank/filters.py (new constant + function, after line 77); src/mfs/rank/shortlist.py:123-151,154-183,186-199,294-299,353-379; .claude/commands/run-pipeline.md:81,88-94`

**Change:** Define CORE_STAGE1_METRICS = ('ret_3y_median', 'ret_3y_p25', 'alpha_3y_annualized', 'sortino_3y', 'info_ratio_3y', 'capture_efficiency') in src/mfs/rank/filters.py. The 5y pair (ret_5y_median/p25) is deliberately NOT core — the 5y->3y proxy fallback (score.py:43-46) is the designed path for sub-5y funds. Missing = is_null() OR is_nan() (NaN reaches here via the zero-NAV poisoning path, alignment.py:92; null-tolerant comparisons must not let NaN through). Add split_core_complete(df) -> (complete, excluded) to filters.py, called after apply_hard_filters; excluded carries scheme_code, canonical_category, data_quality_flag, and missing_core_metrics (';'-joined). In src/mfs/rank/shortlist.py: build_scored_stage1 (lines 123-151) returns (as_of, scored, excluded) — update both callers, rank() at line 194 and rank_deep() at line 295; enrich excluded with scheme_name via _join_scheme_master. In _write_stage1_outputs (lines 154-183): always write stage1/excluded.csv (header even when empty) sorted by (canonical_category, scheme_code), and log rank.stage1.excluded with per-category counts. Update the rank_deep done-log (shortlist.py:353-360) and result dict (:361-379) to include n_excluded + excluded_file. Update .claude/commands/run-pipeline.md stage-1 description (line 81) and sanity-check section to surface excluded.csv. INTERACTION NOTE (bake into the task doc/comment): this gate must land AFTER A1's D1 change (signed alpha, median over ALL windows). Pre-D1, 54/435 stage-1 survivors have null alpha because of the t-stat censor and would all be excluded; post-D1 null alpha only means zero regressable windows (short-history funds), which is exactly what D2 intends to exclude.

**Tests:** tests/test_ranking.py: REPLACE test_stage1_handles_null_alpha_without_nan (line 123 — it asserts the old null-tolerant behavior) with test_null_alpha_fund_excluded_and_listed (fund with null alpha lands in excluded with missing_core_metrics=='alpha_3y_annualized', absent from scored). Add: test_nan_core_metric_excluded (float('nan') sortino_3y -> excluded); test_young_fund_with_null_5y_not_excluded (full 3y set + null 5y pair survives and gets the proxy); test_excluded_csv_written_with_header_when_empty (via _write_stage1_outputs on a complete frame).

**Acceptance:** uv run pytest tests/test_ranking.py -q passes. Then offline against the live DB (read-only; writes only local CSVs): cp -r data/output/shortlist/2026-06-08 /tmp/shortlist_bak && uv run mfs rank --as-of 2026-06-08 — stage1/excluded.csv exists, and a polars one-liner over stage1/*.csv (excluding mf_report/excluded) confirms zero rows with null/NaN in any core metric (pre-D1 the excluded file will list ~54 null-alpha funds; post-D1 it should shrink to short-history funds only).

*Audit findings:* 2b null-metrics-score-as-average (critical, HDFC Defence #1 with one metric), 2c null-alpha-neutral-score, 3 alpha-docstring-phantom-filter, 1a NaN-through-GOOD interaction

### A2-3 [M] Move bonus-option dedupe before z-scoring; guard null base_fund_id — depends on: A2-2

*Files:* `src/mfs/rank/shortlist.py:68-120,146-151`

**Change:** In src/mfs/rank/shortlist.py build_scored_stage1 (lines 146-149), reorder to: apply_hard_filters -> split_core_complete (A2-2) -> _join_scheme_master (dedupe needs base_fund_id) -> _dedupe_legacy_unit_classes -> zscore_within_category -> composite_score_stage1. Because composite_score no longer exists at dedupe time, change the winner sort in _dedupe_legacy_unit_classes (lines 88-95) from ['_is_legacy','composite_score'] to ['_is_legacy','ret_3y_median','scheme_code'] descending=[False,True,False] nulls_last — duplicates have identical NAV-derived metrics, so the only meaningful key is preferring the non-bonus row; the rest is a deterministic tie-break. Guard the latent null-collapse trap: rows with null base_fund_id (fill_null('') at lines 79-87 currently makes them all share dedup key '') must be passed through untouched — partition them out before building _dedup_key and concat back after.

**Tests:** tests/test_ranking.py add: test_bonus_duplicate_does_not_contaminate_z_stats — category with funds A (growth, base_fund_id 'x'), A' (bonus, 'x_bonus', identical metrics), B, C; assert B's z_ret_3y_median from build-equivalent path equals the value computed on {A,B,C} without A' (i.e., the duplicate row is absent from the nanmean/nanstd). test_null_base_fund_id_rows_not_collapsed — two distinct funds with base_fund_id=None both survive dedupe. test_dedupe_keeps_growth_over_bonus still holds with the new sort key.

**Acceptance:** uv run pytest tests/test_ranking.py -q passes; on the 2026-06-08 offline re-rank, Thematic stage1 CSV still has 37 rows and the category z-stats now derive from 37 (not 38) funds — verify one fund's z shifts by exactly the duplicate-removal delta vs /tmp/shortlist_bak.

*Audit findings:* 2b bonus-dedupe-after-zscore (shortlist.py:146-149), 2b null-base_fund_id collapse trap

### A2-4 [L] D3: stage-2 disclosure nulls become soft-neutral — renormalize weights, calibrated fixed penalty, 'partial disclosure' flag; no drops — depends on: A2-1

*Files:* `src/mfs/rank/stage2.py:1-20,45-49,56-63,114-152,165-180,229-238; src/mfs/rank/score.py:63-76,90-140; src/mfs/config.py:109-115 (SoftPenaltiesConfig); configs/pipeline.yaml:80-91; .claude/commands/run-pipeline.md:82,93,144`

**Change:** [REVIEW-ALIGNED with locked decision D3] active_share JOINS the per-missing-metric penalty set the moment it activates (~2026-07 via D9); while it is null universe-wide, renormalization-only (penalizing everyone is meaningless). The penalty constant must be RE-CALIBRATED on the post-A1-10 partition (median non-zero PTR penalty among its stage-2 survivors), not the contaminated 2026-06-08 one. src/mfs/rank/stage2.py: rename REQUIRED_METRICS_ALL (lines 45-49) to DISCLOSURE_METRICS = ('ptr_latest', 'style_drift_3y', 'aum_impact_cost_days', 'active_share_median_1y') and DELETE the survival gate — in apply_stage2 (lines 114-152) every pool fund proceeds; _missing_metrics now feeds two new columns on survivors: partial_disclosure_flag (bool) and missing_disclosures (';'-joined). Keep writing stage2/dropped.csv with the empty-schema header (lines 229-238) for tooling compat; note vestigial in run-pipeline.md. Coverage rows (lines 165-180): n_dropped becomes structurally 0 — add n_partial_disclosure. src/mfs/rank/score.py: (a) per-row positive-weight renormalization in composite_score_stage2 — compute present_pos = sum of w over positive-weight keys whose z-col is non-null/non-NaN for the row, scale each positive term by (sum_all_pos / present_pos); style_drift's negative weight is never renormalized, contributes 0 when missing (current behavior). Implement in _weighted_sum (lines 63-76) behind a renormalize=False default so composite_score_stage1 is unchanged. (b) New fixed penalty in _apply_soft_penalties (lines 104-140): pipeline.yaml soft_penalties gains missing_disclosure: {enabled: true, penalty: 0.025} (+ MissingDisclosurePenalty model in config.py). CALIBRATION (re-derivable spec, run before landing): penalty := round(median of non-zero PTR penalties among the latest run's stage-2 survivors, 3); query: load data/output/shortlist/<latest>/stage2/*.csv excluding dropped/coverage/mf_report, compute pen = clip(max(0,(ptr_latest-1.5)/1.5)*0.05, 0, 0.05), take pen.filter(pen>0).median(). On 2026-06-08: 9 of 115 survivors penalized, median 0.026 -> 0.025. Application rule: the penalty applies once per missing metric in {ptr_latest, style_drift_3y, aum_impact_cost_days}; for any disclosure metric it applies only if the metric is 'live' in the fund's category pool (non-null for >=50% of the pool) so a universe-dead metric (active_share today) doesn't uniformly penalize everyone; active_share missing additionally triggers the weight renormalization (replacing today's silent zero). This satisfies 'missing scores like average-bad, never better than disclosed-bad': 0.025 == the median disclosed-bad penalty, and a missing fund can never beat an identical fund with a sub-threshold disclosed PTR by more than 0. Flag is set whenever any DISCLOSURE_METRIC is missing regardless of liveness. Update stage2.py module docstring (lines 1-20), run-pipeline.md:82 and :144 (stage 2 'drops funds missing a required Phase-2 metric' -> flags+penalizes).

**Tests:** tests/test_stage2.py: REWRITE test_apply_stage2_drops_fund_with_null_required_metric -> test_missing_ptr_kept_flagged_and_penalized (fund stays, partial_disclosure_flag True, composite exactly base - 0.025 vs its disclosed twin); test_apply_stage2_missing_metrics_column_lists_every_null -> assert missing_disclosures lists all nulls; test_required_metrics_match_required_all / test_required_metrics_excludes_removed_columns updated to DISCLOSURE_METRICS. New: test_renormalization_scales_positive_weights (fund missing active_share in a live-active_share cohort: positive z terms scaled by 0.85/0.75... use actual weight constants); test_dead_metric_no_uniform_penalty (active_share null for entire cohort -> no penalty, no renormalization difference between funds); test_missing_never_better_than_disclosed_bad (identical metrics, one missing PTR, one PTR=3.0: disclosed-bad scores 0.05 penalty > missing's 0.025 — assert missing fund ranks higher than worst-disclosed but lower than median-disclosed twin with PTR 1.5). tests/test_ranking.py:185 test_stage2_style_drift_null_does_not_penalize — update: null style_drift in a live cohort now costs the fixed penalty.

**Acceptance:** uv run pytest tests/test_stage2.py tests/test_ranking.py tests/test_ptr_penalty.py -q passes. Offline re-rank of 2026-06-08: stage2/dropped.csv has 0 data rows; the 14 previously-dropped funds (incl. Kotak Multicap 149185, quant Multi Cap 120823, HDFC Defence 151750 — modulo D2 exclusions) appear in stage2 outputs with partial_disclosure_flag=True; Multi Cap's stage-2 slate is non-degenerate.

*Audit findings:* 1a stage-2 silent exclusion of category winners, 2c missing-PTR-fatal severity inversion (stage2.py:45-49), 2b null-as-average at stage 2

### A2-5 [M] Carry stage-1 full-universe z-scores into stage 2; stop re-z-scoring tiny cohorts — depends on: A2-4

*Files:* `src/mfs/rank/shortlist.py:324-343; src/mfs/rank/zscore.py:36-57; src/mfs/rank/stage2.py:51-53,124-139`

**Change:** FEASIBILITY (assessed): phase-1 z-cols can be carried — scored_stage1 already holds full-universe z_* + composite_score for every pool member; phase-2 metrics (style_drift_3y, active_share_median_1y) exist ONLY for the candidate pool (orchestrator.run_phase2 is restricted to it), so they must be z-scored within the per-category top-20 pool — that is the maximal available cohort and, post-D3 (no drops), it is stable. Changes: (1) src/mfs/rank/shortlist.py rank_deep lines 335-338 — delete the re-zscore+re-composite of candidates_with_phase2; instead join scored_stage1's z_* columns and composite_score (renamed composite_score_stage1) onto candidates_with_phase2 by scheme_code, keeping the raw phase-2 metric columns from the reload. This also fixes the stage1_rank disagreement (10/129 rows in the audit) because pool ranking now uses the carried universe composite. (2) src/mfs/rank/zscore.py zscore_within_category (lines 36-57): add param metrics: Sequence[str] | None = None to restrict which Z_METRICS are (re)computed. (3) src/mfs/rank/stage2.py apply_stage2 lines 126-139: stop dropping ALL z_ cols; compute z only for ('style_drift_3y','active_share_median_1y') within the pool (pre-selection, once — the post-drop second re-z-score disappears with D3). Add MIN_COHORT_FOR_Z = 5: when the category pool has fewer than 5 rows, set the phase-2 z-cols to 0.0 (neutral) instead of n<5 noise — carried phase-1 z keep full-universe information (this fixes the Manufacturing N=2 case where nanstd over [NaN, 0.0405] zeroed ABSL's measured alpha and handed final #1 to a null-alpha fund). (4) Delete the now-dead z-recompute comment block stage2.py:127-129.

**Tests:** tests/test_stage2.py new: test_phase1_z_carried_not_recomputed (input pool carries z_alpha_3y_annualized values that differ from what pool-local re-z-scoring would produce; assert survivors retain the carried values exactly); test_tiny_cohort_phase2_z_neutral (N=2 category: z_style_drift_3y == 0.0 for both, carried z_alpha intact and composite ordering follows it — regression for the Manufacturing collapse); test_stage1_rank_matches_carried_composite (pool stage1_rank ordering == composite_score_stage1 ordering). tests/test_ranking.py:159/173 stage-2 reward/penalty tests: feed pools >= MIN_COHORT_FOR_Z so they still exercise live z-scoring.

**Acceptance:** uv run pytest tests/test_stage2.py tests/test_ranking.py -q passes. Offline re-rank of 2026-06-08: for every stage-2 row, z_alpha_3y_annualized equals the same scheme's value in stage1/<cat>.csv (spot-check with polars join, max abs diff 0.0); Manufacturing top pick is no longer the null-alpha fund when its rival has measured alpha (post-D2 the null-alpha fund is excluded at stage 1 anyway).

*Audit findings:* 2b stage-2 tiny-cohort re-z-scoring / N=2 z-collapse (stage2.py:126-134), 2b pool-vs-universe stage1_rank disagreement

### A2-6 [M] AUM-impact penalty: log-scale ramp to 500 days + liquidity flag in output — depends on: A2-4

*Files:* `src/mfs/rank/score.py:124-138; src/mfs/config.py:97-106; configs/pipeline.yaml:88-91; src/mfs/rank/stage2.py (flag column in apply_stage2)`

**Change:** [REVIEW-RESOLVED conflict with E3] liquidity_flag adopts E3's design: string enum OK/HIGH/SEVERE at 30/90 days (constants LIQUIDITY_FLAG_HIGH_DAYS=30, LIQUIDITY_FLAG_SEVERE_DAYS=90). A2-6 keeps the log-scale penalty ramp but emits E3's flag; the 60-day boolean is dropped. DECISION: log-scale ramp (not percentile — percentile would re-rank whenever the cohort changes and is opaque in config). src/mfs/rank/score.py:124-138: replace the linear ramp with penalty = max_penalty * clip( ln(days / threshold_days) / ln(saturation_days / threshold_days), 0, 1 ) for days > threshold_days, else 0; null days -> 0 (unchanged — D3's missing-disclosure penalty covers null). src/mfs/config.py AumImpactCostPenalty (lines 97-106): add saturation_days: float = 500.0; configs/pipeline.yaml:88-91: add saturation_days: 500. Calibration vs the 2026-06-08 survivor distribution (median 12.8d / p75 36.4d / p95 163d / max 924d; Small Cap median 79d): 10d -> 0.0075, 36d -> 0.021, 79d -> 0.030, 163d -> 0.038, >=500d -> 0.05 — monotone discrimination across the whole observed range instead of 63/115 funds flat-capped at 10d. Surface days_to_exit: aum_impact_cost_days is already a column in stage2/stage3 CSVs (verified in 2026-06-08 outputs) — additionally add a liquidity_flag column (aum_impact_cost_days > 60; judgment threshold, define as module constant LIQUIDITY_FLAG_DAYS in stage2.py) set in apply_stage2 so it flows into stage2 and stage3 mf_report, and ensure aum_impact_cost_days + liquidity_flag are included in the stage-2/3 report column sets.

**Tests:** tests/test_ranking.py:202 test_stage2_aum_impact_penalty_above_threshold: update expected values to the log curve. New pinned-value tests: days=5 -> 0; days=10 -> 0.05*ln(2)/ln(100) (~0.00753); days=500 -> 0.05; days=924 -> 0.05 (capped); days=None -> 0; monotonicity assert between 10/79/163. tests/test_stage2.py: test_liquidity_flag_set_above_60_days.

**Acceptance:** uv run pytest tests/test_ranking.py tests/test_stage2.py -q passes. Offline re-rank of 2026-06-08: Nippon India Small Cap (118778, 924d) carries liquidity_flag=True and the full 0.05 penalty while a ~12d fund carries ~0.009 — confirm distinct penalties via the stage2 CSV (composite delta vs disclosed metrics) instead of the previous uniform -0.05.

*Audit findings:* 2b/2c AUM-impact penalty saturation (score.py:124-138, Small Cap median 79d vs 10d cap), 4 liquidity flag absent for 924-day pick

### A2-7 [M] D4: stage 3 keep-both + flag (restore locked 2026-05-24 decision); update CLI and run-pipeline.md — depends on: A2-4

*Files:* `src/mfs/rank/stage3.py:1-16,31-39,42-155,158-235; src/mfs/rank/shortlist.py:345-378; src/mfs/cli.py:479-525; .claude/commands/run-pipeline.md:83,94,124`

**Change:** src/mfs/rank/stage3.py: delete the iterative drop while-loop (lines 101-143) and _stage2_rank (lines 34-39). apply_stage3 keeps the existing all-pairs overlap computation (lines 75-96), then KEEPS every survivor and annotates: overlap_flag (bool: any pair > threshold_pct), max_overlap_pct, n_overlap_breaches, overlap_breaches (';'-joined '<counterpart scheme_code> <scheme_name> (<category>) @ <overlap_pct>%'). Return (final_picks_with_flags, breaches_df, pairs_log_df) where breaches_df is one row per breaching PAIR (both codes, names, categories, stage2 ranks, overlap_pct, top_shared_holdings) replacing drops_df. run(): write stage3/overlap_breaches.csv (replaces dropped.csv — delete the dropped.csv writer at lines 186-194; grep for consumers first: run-pipeline.md:94 reads it), keep stage3/overlap_pairs.csv for pairs > threshold, and ADD stage3/overlap_matrix.csv with ALL pair_records unfiltered (audit dim-4: subset-buyers need sub-threshold overlaps; data already computed at line 95). Per-category files + mf_report now contain every stage-2 survivor with flag columns; module docstring (lines 1-16) rewritten to cite the locked decision (docs/phase3/two_stage_pipeline_plan.md:7: 'keep both funds when overlap > 30%, flag the breach (no auto-drop)'). Callers: shortlist.rank_deep log (lines 353-360) and result dict (372-378) — n_dropped -> n_flagged, dropped_file -> breaches_file; cli.py rank-deep echo (lines 519-523) and docstrings (lines 481-483, 490-496) — 'Pairs strictly above this are dropped' -> 'flagged'. .claude/commands/run-pipeline.md: line 83 (stage-3 'DROP ... not informational' -> keep-both+flag), line 94 (stage3/dropped.csv -> overlap_breaches.csv), line 124 (final picks / overlap-drops -> flags).

**Tests:** tests/test_stage3.py rewrite: test_identical_portfolios_drop_worse_rank -> test_identical_portfolios_both_kept_and_flagged (both in final, overlap_flag True, max_overlap_pct 100, each lists the other in overlap_breaches); test_cascade_three_overlapping_funds -> all three kept, n_overlap_breaches counts pairs; test_cross_category_drops_apply -> cross-category breach flagged, both kept; test_threshold_boundary_exactly_30_no_drop -> no flag at exactly 30.0; test_pair_with_missing_holdings_reported_no_drop -> reason recorded in pairs log, no flag; test_run_writes_drop_to_dropped_csv -> test_run_writes_breaches_and_matrix (overlap_breaches.csv + overlap_matrix.csv written, matrix has C(n,2) rows); keep empty/single-survivor tests (flags default False).

**Acceptance:** uv run pytest tests/test_stage3.py -q passes. Offline re-rank of 2026-06-08: number of stage3 category CSVs == number of stage2 category CSVs (26; previously 21 — ELSS, Flexi Cap, Balanced Advantage, Equity Savings, Contra restored); stage3 row count == stage2 survivor count (~115); Parag Parikh Flexi Cap (122639) present with overlap_flag=True listing PPFAS ELSS @ 79.4%.

*Audit findings:* 2b stage-3 greedy drop empties 5/26 categories (stage3.py:101-143), 4 category wipe-out + stale-keeper drops + subset-buyer overlap matrix; reverses the documented violation of the 2026-05-24 locked decision

### A2-8 [S] Fix phantom docstrings: zscore.py pro-rata claim, alpha.py not-null-filter claim — depends on: A2-2, A2-4

*Files:* `src/mfs/rank/zscore.py:17-20; src/mfs/compute/alpha.py:12-14; src/mfs/rank/score.py:7-10`

**Change:** src/mfs/rank/zscore.py:17-20 — delete 'active_share triggers pro-rata redistribution' (no redistribution exists; post-A2-4 the true behavior is per-row positive-weight renormalization in composite_score_stage2 — say that, and that style_drift NaN contributes zero plus the missing-disclosure penalty). src/mfs/compute/alpha.py:12-14 — the claim 'the scheme then drops out of ranking via the existing not-null filter' is false today; after A1-D1 + A2-2 it becomes true via the CORE_STAGE1_METRICS gate — rewrite to: 'If no windows could be regressed, alpha is None and the scheme is hard-dropped at Stage 1 by the core-metric gate (rank/filters.py CORE_STAGE1_METRICS) and listed in stage1/excluded.csv.' Coordinate with A1: if A1's D1 task rewrites the alpha module docstring wholesale (t-stat censor removal), this task only verifies the rank-side reference; do not double-edit. Also fix the stale score.py module docstring (lines 7-10: 'with all Phase 2 metrics non-null' — false post-D3).

**Tests:** None (doc-only).

**Acceptance:** grep -n 'pro-rata' src/mfs/rank/zscore.py returns nothing; grep -n 'existing not-null filter' src/mfs/compute/alpha.py returns nothing; grep -rn 'non-null' src/mfs/rank/score.py docstring no longer claims the stage-2 pool requires all Phase 2 metrics.

*Audit findings:* 2b phantom pro-rata redistribution (zscore.py:18-20), 3/2c alpha docstring phantom not-null filter (alpha.py:12-14)

### A2-9 [M] Pinned end-to-end rank_deep test on a fixture universe — depends on: A2-2, A2-3, A2-4, A2-5, A2-6, A2-7

*Files:* `tests/test_rank_deep_e2e.py (new); reuse fixture helpers from tests/test_stage2.py/_df and tests/test_stage3.py/_make_loader where practical`

**Change:** [REVIEW-NOTE] e2e pins assert liquidity_flag string values (e.g. 'SEVERE'), matching E3's tiering. New tests/test_rank_deep_e2e.py — first test anywhere to import rank_deep (verified: zero today). Build a deterministic in-memory fixture: 3 categories x 6-10 funds of synthetic computed_metrics rows (all metric columns incl. phase-2) plus a scheme_master frame and per-scheme holdings frames. Monkeypatch at the seams rank_deep already uses: mfs.db.queries.latest_computed_metrics_date, computed_metrics_at, computed_metrics_for_schemes, scheme_master, latest_scheme_aum, holdings_for_scheme (shortlist.py:131,135,310, _join_scheme_master:58, _build_aum_map:256, _latest_holdings_loader:264) and mfs.paths.shortlist_dir -> tmp_path; call shortlist.rank_deep(as_of=date(2026,1,31), skip_phase2_compute=True). Fixture must exercise every behavior this workstream pins: (a) one fund with null alpha -> appears in stage1/excluded.csv, absent everywhere downstream (D2); (b) a Growth/Bonus duplicate -> exactly one survives and z-stats exclude the twin (A2-3); (c) one fund missing ptr_latest in a live-PTR cohort -> present in stage2 with partial_disclosure_flag=True and composite == disclosed-twin - 0.025 (D3); (d) a pair with 60% holdings overlap -> BOTH in stage3 final, overlap_flag=True with each other's code and 60.0 in overlap_breaches (D4); (e) one fund with aum_impact_cost_days=500 -> liquidity_flag=True, penalty 0.05 vs a 12d peer's ~0.009 (A2-6); (f) a 2-fund category -> phase-2 z neutral, carried phase-1 z drives order (A2-5). Pin exact artifacts: the set+order of scheme_codes per category in stage1/stage2/stage3 CSVs, excluded.csv contents, and composite_score values to 1e-9 against hardcoded expected constants (compute once, freeze). Any future scoring change must consciously update the pins.

**Tests:** The task IS the test. Mark with no network/DB access (pure monkeypatched).

**Acceptance:** uv run pytest tests/test_rank_deep_e2e.py -q passes in <30s with no DB; deliberately perturbing one stage-2 weight in a temp config makes the pinned-composite assertion fail (sanity-check the pin actually binds).

*Audit findings:* Test-quality gap: zero tests import rank_deep; integration pin for D2/D3/D4 + A2-3/5/6

### A2-10 [S] PTR penalty: exempt structurally high-turnover hybrid categories — depends on: A2-4

*Files:* `configs/pipeline.yaml, src/mfs/rank/score.py`

**Change:** Add ptr_penalty_exempt_categories: [Balanced Advantage, Equity Savings] to pipeline.yaml (20 of 31 funds with PTR>1.5 are these categories, where multi-x turnover is structural arbitrage mechanics, not churn). Exempt them from the >1.5 ramp. Revisit with the D3 backtest (alternative: category-relative percentile ramp).

**Tests:** BAF fund PTR=3.0 -> no penalty; Mid Cap fund PTR=3.0 -> penalized.

**Acceptance:** Post-re-rank: no BAF/Equity Savings fund carries ptr_penalty.

*Audit findings:* 2c uniform PTR threshold penalizes structural turnover [medium]

### A2-11 [S] Deterministic final tie-break in all ranked outputs — depends on: A2-7

*Files:* `src/mfs/rank/stage2.py, stage3.py, shortlist.py`

**Change:** Sort key everywhere rankings are emitted: (composite desc, scheme_code asc). Today degenerate/tied cohorts (all-zero z) tie-break on arbitrary row order, so same-day re-runs are not reproducible.

**Tests:** Two funds with identical composites -> stable order across repeated runs.

**Acceptance:** Re-running rank twice on the same partition produces byte-identical CSVs.

*Audit findings:* critic run-provenance nondeterminism

**Workstream risks:**

- Sequencing with workstream A1: if A2-2 (D2 hard-drop) lands before A1's signed-alpha change, ~54/435 stage-1 survivors with t-stat-censored (not truly missing) alpha get excluded — a large, temporary universe shrink. Mitigation: land A2-2 after A1-D1, or feature-gate the alpha element of CORE_STAGE1_METRICS until D1 merges.
- The 0.025 missing-disclosure penalty is calibrated from n=9 penalized funds in one run (median 0.026); it is a point-in-time constant. Re-run the spec'd calibration query after PTR-coverage work (other workstreams) materially changes the survivor PTR distribution, and treat the yaml value as the audit trail.
- D3+D4 together change the final report's shape (≈115 flagged rows across 26 categories instead of 33 picks across 21): downstream consumers of stage2/dropped.csv and stage3/dropped.csv (run-pipeline.md sanity checks, any user scripts) will break if the doc updates in A2-4/A2-7 are skipped; the old artifacts are kept/renamed deliberately — verify with grep before deleting any writer.
- Stage-2 scores are not comparable across the change boundary (renormalization + carried z + log AUM ramp all shift composite values): the first post-remediation run cannot be diffed rank-for-rank against 2026-06-08; document a one-time discontinuity in the run notes.
- A2-4, A2-5, and A2-6 all edit stage2.py/score.py — land them sequentially (4 then 5 then 6), not in parallel branches, or merge conflicts will silently re-introduce the drop gate or double z-scoring.
- liquidity_flag threshold (60 days) and MIN_COHORT_FOR_Z (5) are judgment constants not derived from the audit; both are isolated as named constants so they can be re-tuned without touching logic.
- Per-row weight renormalization can overweight the few present metrics for a fund missing several disclosures; bounded today because phase-1 z are always present post-D2 and only active_share renormalizes among positive weights, but re-check if more positive-weight phase-2 metrics are added (e.g. when active_share goes live ~2026-07).

**Ordering notes:** Order: A2-1 (config plumbing everything else reads) → A2-2 (D2; gate on A1's D1 signed-alpha task landing first — 'A1-D1' here denotes whatever id the A1 planner assigned to replacing the t-stat censor in compute/alpha.py) → A2-3 (builds on A2-2's reordered build_scored_stage1) → A2-4 (D3, biggest behavioral change) → A2-5 (z-carry; assumes the D3 no-drop cohort) → A2-6 (log ramp; touches the same score.py/stage2.py files as 4/5, land after) → A2-7 (D4; independent of 4-6 logically but its acceptance check on the 2026-06-08 offline re-rank assumes D3 flags exist, so land after A2-4) → A2-8 (docstrings; after the behaviors they describe exist) → A2-9 (pinned e2e test, last — it freezes the combined behavior). All acceptance re-rank commands are offline (read-only DB, local CSV writes): back up data/output/shortlist/2026-06-08 to /tmp before the first re-run since rank/rank-deep overwrite that directory. Nothing here touches ingestion, so the fail-fast/AMFI-AAUM/incremental invariants are unaffected; D2/D3 keep the no-nullable-strict-fields spirit by making missing data either a visible hard exclusion (core metrics) or a flagged, penalized soft state (disclosure metrics) — never silent category-average credit.


## Workstream B — Ingestion robustness & gates

Workstream B hardens the ingestion layer where the audit found "wrongness is mostly silent": unvalidated downloads that permanently poison caches, wrong-month artifacts ingested as current, fuzzy-match poaching that stores one fund's portfolio under another, a parser bug that silently excludes 4 large ICICI funds from the universe, partial NAV days accepted as full, all monthly freshness gates disabled, raw-traceback 4xx exits, a 2-transaction NAV/log-returns crash window, a 2.6h/run kotak retry pathology, and 3 dormant adapters. Shape: validate content before caching (B1) so everything downstream can trust artifacts; make per-scheme silent-wrongness loud (B2/B3/B5); make the universe and gates correct (B4/B6/B7); make failure modes clean and cheap (B8-B11); make registration structural (B12). All tasks preserve fail-fast/no-half-data: bad tuples are skipped per-scheme with loud logs, systemic staleness halts.

### B1 [M] Content validation on all downloads: magic-bytes/content-type check before caching; delete cached artifact on parse failure

*Files:* `src/mfs/io/content_check.py (new), src/mfs/io/http.py:52-58, src/mfs/ingest/managers/_base.py:51-65, src/mfs/ingest/holdings/_generic.py:322-326, src/mfs/ingest/holdings/kotak.py:354-385, src/mfs/ingest/holdings/_run.py:119-127, src/mfs/ingest/managers/_run.py:143-146`

**Change:** Create src/mfs/io/content_check.py with sniff_kind(data: bytes) -> str ('pdf' if startswith b'%PDF-', 'xlsx_zip' if b'PK\x03\x04', 'xls_ole2' if b'\xd0\xcf\x11\xe0', 'html' if lstripped lowercase startswith b'<!doctype' or b'<html', 'csv_text', else 'unknown') and validate_payload(data, expected: str, url: str) raising IngestError on mismatch (always reject 'html' for binary expectations — this is the trustmf.com WAF 200-with-487-byte-React-shell case documented in managers/trust.py). Wire it in: (1) io/http.py:52 download_to gains keyword expect: str | None = None; validate BEFORE writing the .tmp file so a bad payload never reaches the cache. (2) managers/_base.py:51-65 fetch → download_to(url, out, expect='pdf'). (3) holdings/_generic.py:322-326 GenericHoldingsAdapter.fetch_excel → expect='excel' (accept xlsx_zip OR xls_ole2). (4) kotak.py:354-385 fetch_excel writes bytes directly at lines 379-382 — validate data magic there; an HTML/JSON payload counts as a failed attempt (continue the retry loop), never written. (5) In holdings/_run.py:119-127, when adapter.parse_excel raises OR yields zero records for a cached file, delete excel_path (log holdings.cache_evicted) so the next run re-fetches instead of skipping forever; same eviction in managers/_run.py when both parse_holdings and parse_ptr yield zero records from a cached PDF. One-time cleanup: delete the 5 corrupt axis 2026-03 .xlsx cache files the 1c audit found ('File contains no valid workbook part') — find via a script that tries openpyxl.load_workbook over data/raw/holdings/axis/2026-03/.

**Tests:** New tests/test_content_validation.py: (a) download_to with monkeypatched fetch_bytes returning the 487-byte React HTML shell under a .pdf name → IngestError and NO file on disk; (b) %PDF- bytes pass for expect='pdf'; PK and OLE2 pass for expect='excel'; (c) holdings/_run parse failure on a cached file deletes the file; (d) kotak fetch_excel with HTML payload retries instead of caching. Extend tests/test_holdings_generic.py with a zero-record-parse eviction case.

**Acceptance:** uv run pytest tests/test_content_validation.py tests/test_holdings_generic.py -q passes; grep confirms no remaining bare download_to call for PDF/Excel artifact paths without expect=.

*Audit findings:* 1b 'No content validation on downloads + unconditional cache reuse' (io/http.py:52); 1c side-observation (corrupt axis xlsx cached forever); 8 'fetch layer trusts cached artifacts'

### B2 [M] Generalize internal statement-date validation to all holdings adapters; advisory check on the factsheet path — depends on: B1, STAGE1-urgent-3 (quant wrong-month fix + cache/DB purge)

*Files:* `src/mfs/ingest/holdings/_generic.py (extract_statement_date, parse path), src/mfs/ingest/holdings/_run.py:103-139, src/mfs/ingest/managers/_run.py:98-104 (advisory scan), src/mfs/ingest/holdings/bajaj_finserv.py:116 (reuse regex)`

**Change:** [REVIEW-RESCOPED] Stage-1 Urgent-3 already builds the generic mechanism (find_statement_months in _generic.py, expect_ym threaded through parse_sebi_excel, StatementDateMismatchError -> cache unlink -> per-AMC abort). B2 = audit & extend COVERAGE only: verify every holdings adapter (incl. the ~8 direct-call ones) flows through the check, add per-adapter date-extraction overrides where the banner is nonstandard, and add the ADVISORY check on the managers/factsheet path. Do NOT build a second helper. EXTENDS the Stage-1 quant fix (urgent item 3: quant April-files-as-May + cache purge) — do not duplicate its quant-specific banner parse; lift it into shared code. Add extract_statement_date(rows: list[tuple]) -> date | None to holdings/_generic.py: scan the first ~15 rows of a sheet for the SEBI banner variants ('AS ON 30 Apr 2026', 'as on April 30, 2026', 'Monthly Portfolio Statement as on ...' — regex with both day-month-year and month-day-year forms; bajaj_finserv.py:116 already has a working 'as on' regex to reuse). Call it inside _parse_one_sheet/parse_sebi_excel and surface the found date on the parse result (e.g. module-level return or a sentinel record attribute); in holdings/_run.py:118-128 after parsing, if a statement date was found and (year, month) != ym → do NOT write the scheme's rows: log holdings.wrong_month_artifact (amc, scheme, found, requested), delete the cached Excel (so the real month's file can land later — the permanent-poison mechanism from the quant incident), and count it in the run summary dict. If no banner is found, proceed (most adapters print it; absence alone must not nuke coverage) but log at debug. For the managers/factsheet path (assessment deliverable): add an advisory first-page scan in managers/_run.py after fetch — regex for 'as on / Data as on <date>' on page 1 text via pdfplumber; on month mismatch log managers.statement_date_mismatch as a WARNING only (factsheet front pages vary too much for a hard gate); record the verdict in the per-AMC result dict so Gate B output can show it.

**Tests:** Extend tests/test_holdings_generic.py: synthetic workbook with banner 'Monthly Portfolio Statement AS ON 30 Apr 2026' parsed with ym='2026-05' → zero rows written, cache file deleted, summary counts 1 wrong-month skip; ym='2026-04' → rows written. Regression fixture mirroring the quant layout. Unit test for extract_statement_date over ≥4 real banner string variants (quant, abakkus, bajaj_finserv, generic).

**Acceptance:** uv run pytest tests/test_holdings_generic.py -q passes. After the next monthly pipeline run: psql -d mfs -c "SELECT source_amc FROM holdings_monthly h1 WHERE as_of_month='<this-month>' GROUP BY 1" shows no AMC whose rows are byte-identical to the prior month (the quant signature), and run summary lists wrong-month skips explicitly.

*Audit findings:* 1b 'quant holdings: April portfolios ingested as May 2026 — no validation of file's internal statement date' (quant.py:100); 1b summary 'No adapter validates the artifact's internal statement date'

### B3 [M] Per-portfolio weight-sum and min-holdings sanity gate at holdings ingest

*Files:* `src/mfs/ingest/holdings/_run.py:119-139 (+ summary keys at 185-202), src/mfs/ingest/managers/_run.py:206-242`

**Change:** In holdings/_run.py after records are parsed for a scheme (lines 119-128), compute total = sum(r.weight_pct). Look up the scheme's canonical_category from the sm DataFrame already loaded at line 58. Gate (applies when canonical_category is not null, i.e. rankable): if total outside hard bounds [40, 115] → skip the scheme (do NOT append its rows), log holdings.weight_sum_breach(amc, scheme, total) at ERROR, count in run summary — per the no-half-data invariant a mis-scaled portfolio is worse than a missing one. WARN (but still write) for [40,60) and (105,115]. Bounds are empirical, not the 95-105 a clean spec would suggest: live rankable scheme-months span 47.9%-108.8% because ISIN-less cash/derivative rows are dropped at parse (verified 2026-06-11: 211 of 751 rankable scheme-months sit in 50-95, min ~48 for Thematic) — 95-105 would discard ~30% of good months. Document this rationale in a comment. Same gate, same bounds, in managers/_run.py _resolve_holdings (factsheet holdings path) per printed scheme. Additionally: for equity-category schemes (exclude FoF-flavoured names matching r'(?i)fof|fund of fund'), require >= 5 holdings rows; fewer → skip + log holdings.too_few_holdings. This is the generic net for the Motilal-class contamination (the contaminated 152651 April row-set was exactly 4 ETF rows).

**Tests:** New tests/test_holdings_weight_gate.py: records summing 186% → scheme skipped, error logged, summary counter incremented; 160% → skipped; 99% → written; 48% (Thematic-style) → written with WARN; equity scheme with 4 records → skipped; FoF name with 4 records → written.

**Acceptance:** uv run pytest tests/test_holdings_weight_gate.py -q passes. Standing SQL check returns 0 rows for newly ingested months: SELECT scheme_code, as_of_month, SUM(weight_pct) FROM holdings_monthly h JOIN scheme_master USING (scheme_code) WHERE canonical_category IS NOT NULL AND as_of_month > '2026-05-01' GROUP BY 1,2 HAVING SUM(weight_pct) NOT BETWEEN 40 AND 115;

*Audit findings:* 1b 'holdings_monthly carries scheme-months with weight sums 120-186% — no portfolio-level weight-sum sanity check' (_run.py:141); 1a Motilal contamination rec ('flag any active equity fund with <8 holdings post-ingest')

### B4 [M] Partial NAV-volume day detection in amfi_nav ingest — depends on: STAGE1-urgent-2 (pipeline NAV stage → ingest_since + backfill)

*Files:* `src/mfs/ingest/amfi_nav.py:196-219 and 258-269, src/mfs/db/queries.py (new helper)`

**Change:** Add q.nav_daily_day_counts(n: int) to db/queries.py returning the last n (nav_date, count) pairs. In amfi_nav.py ingest_today, after the zero-row check at lines 210-216: let latest_d = df['nav_date'].max() and n_latest = rows at latest_d; compute baseline = median of trailing per-day counts where count >= 1000 over the last 30 calendar days (the >=1000 floor excludes already-known partial days like the live 12/5/20-row days; full days run ~8,600). If n_latest < 0.5 * baseline → treat latest_d as a partial publication: EXCLUDE its rows from the upsert (filter the frame), log amfi_nav.partial_day_rejected(date, n_rows, baseline) at ERROR, and proceed with the remaining full-day rows — the rejected day arrives complete on a later run via the bulk-history path (Stage-1 urgent item 2 switches the pipeline NAV stage to ingest_since). If filtering leaves the frame empty, raise IngestError (whole snapshot is partial — halt per fail-fast). Apply the same per-day threshold inside ingest_backfill's per-window parse (amfi_nav.py:258-269) so backfilled windows can't re-introduce partial days; there, a sub-threshold day inside a window is dropped with the same ERROR log. Empirical justification (live DB 2026-06-11): full days ~7,900-8,600 rows; partial days 5-823 rows; the 2026-06-08 run computed metrics over an 823-row latest day.

**Tests:** Extend tests/test_amfi_nav_parser.py: monkeypatch q.nav_daily_day_counts → baseline 8600; synthetic parse output with 8000 rows on day D-1 + 500 rows on day D → upsert called WITHOUT day D rows, error logged; 8600-row day passes untouched; all-partial snapshot raises IngestError.

**Acceptance:** uv run pytest tests/test_amfi_nav_parser.py -q passes; after deployment, psql -d mfs -c "SELECT nav_date, count(*) FROM nav_daily GROUP BY 1 ORDER BY 1 DESC LIMIT 10" shows no new day below ~50% of the trailing median.

*Audit findings:* 1b 'AMFI NAV: only a zero-row check — partial-volume days are accepted' (amfi_nav.py:210); 1a/8 NAV-hole findings (detection half; the backfill half is Stage-1 urgent item 2)

### B5 [L] Scheme-match collision resolution: require unique best match with margin; fix + purge the Motilal Multi Cap contamination; PTR-path collision resolution

*Files:* `src/mfs/ingest/managers/_scheme_match.py:184-229, src/mfs/ingest/managers/_run.py:148-156, 206-278, src/mfs/ingest/holdings/_run.py:80-96 (shared semantics), one-time SQL purge`

**Change:** (1) _scheme_match.py match_one (lines 200-229): after process.extract, enforce uniqueness-with-margin: exact canonical equality (canon_in == candidate key) always wins immediately. Otherwise compute plain fuzz.ratio for all candidates within 2 points of top token_set score; accept only if the best plain-ratio exceeds the runner-up's plain-ratio by >= 5 points; AND reject the subset-poaching case: token_set_ratio == 100 with plain ratio < 80 and no exact equality → ambiguous. On rejection return MatchResult(printed, None, None, score) with a new field ambiguous=True and log scheme_match.ambiguous(printed, top_candidates) at WARNING so skips are loud, per 'ambiguity -> skip scheme'. (2) Token guard: reject any match where the printed canonical name contains a discriminator token absent from the matched key — module-level frozenset {'FOF','PASSIVE','FACTOR','ETF','INDEX','NEXT'} (covers 'Multi Factor Passive Fund of Funds'→'Multi Cap Fund' and 'Nifty Next 50'→'Nifty 50'). (3) managers/_run.py: port holdings' best-score-per-code collision resolution (holdings/_run.py:80-96) to _resolve_ptr/_resolve_holdings (two printed names → same scheme_code: keep the higher score, log the loser), replacing the current first-parse-order-wins dedupe at line 156; and fix the dead match_threshold parameter by passing threshold=match_threshold at _run.py:228 and :264. (4) One-time data purge (engineer runs; this incident's rows verified live): psql -d mfs -c "DELETE FROM holdings_monthly WHERE scheme_code='152651' AND as_of_month='2026-04-01' AND source_amc='motilal_oswal'" (exactly 4 ETF rows). (5) Verification sweep before merge: run match_one old-vs-new over every printed name in the cached discovery/factsheet artifacts (a throwaway script over data/raw/) and review the diff — every changed outcome must be either a poach prevented or an ambiguity made loud; update the bespoke rewrites that become redundant (kotak.py:129-131 _PTR_NAME_REWRITES, dsp.py:80-93, quant.py:99-106 _SKIP_NAMES, motilal_oswal.py:194-221) only if the sweep proves them covered.

**Tests:** Extend tests/test_scheme_match.py: (a) 'ABC Mid Cap Fund' with only 'ABC LARGE AND MID CAP FUND' as candidate → no match, ambiguous/poach-rejected; (b) both present → exact wins; (c) regression: 'Motilal Oswal Multi Factor Passive Fund of Funds' against a candidate set containing 'MOTILAL OSWAL MULTI CAP FUND' but not 154239 → no match; (d) 'ABC Nifty Next 50 Index Fund' vs 'ABC NIFTY 50 INDEX FUND' only → no match; (e) all existing matcher tests still pass (kotak/dsp rewrite fixtures in tests/test_managers_kotak.py, test_managers_dsp.py). New PTR-collision test in tests/test_parse_cache.py-adjacent module or new tests/test_managers_run.py: two printed names → same code keeps higher score.

**Acceptance:** uv run pytest tests/test_scheme_match.py tests/test_managers_kotak.py tests/test_managers_dsp.py tests/test_managers_quant.py -q passes; psql -d mfs -c "SELECT count(*) FROM holdings_monthly WHERE scheme_code='152651' AND as_of_month='2026-04-01'" returns 0 post-purge; the old-vs-new sweep diff is attached to the PR and shows no lost true match.

*Audit findings:* 1b 'Subset scheme names score 100 in fuzzy match' (_scheme_match.py:206); 1a 'Holdings cross-contamination: Motilal Multi Factor FoF → Multi Cap' (_run.py:83-105); 1b 'run_for_amc(match_threshold) dead parameter' (_run.py:228)

### B6 [M] Option-parser fix: 'Cumulative' → GROWTH; no-token single-option default; DIRECT/UNKNOWN sanity listing

*Files:* `src/mfs/master/scheme_master.py:118-134, 168-210`

**Change:** Empirical token enumeration (done 2026-06-11 against live scheme_master, satisfying the enumerate-first requirement): of 73 active DIRECT, rankable-category, option_type=UNKNOWN rows, 4 contain 'cumulative' (exactly the ICICI Equity Savings 133054 / Manufacturing 145075 / India Opportunities 145897 / PHD 143874 growth plans) and 61 carry NO option token at all (no growth/idcw/dividend/cumulative/bonus); before merging, also grep the latest cached NAVAll under data/raw/amfi_nav/ for any further option-like trailing tokens (e.g. 'APPRECIATION') and add them if found. Change scheme_master.py _classify_plan_option (lines 118-134): add elif 'cumulative' in name_lc: option = 'GROWTH' (after the idcw/dividend branch so 'Cumulative IDCW' oddities keep IDCW precedence). Then add a post-pass in build() (after the per-row loop at lines 179-205): group rows by (base_fund_id, plan_type); where a row has option UNKNOWN and contains none of the enumerated option tokens and NO sibling row in the group has option_type != UNKNOWN, default it to GROWTH (single-option funds like Samco '... - Direct Plan'). Keep UNKNOWN when an IDCW/GROWTH sibling exists (genuine ambiguity). Finally, log a sanity listing at the end of build(): every remaining active DIRECT+UNKNOWN scheme in a rankable category (scheme_code, name) — expected to shrink from 73 toward ~0; this listing is the recurring tripwire for future AMFI naming drift.

**Tests:** Extend tests/test_scheme_master.py: 'ICICI Prudential Equity Savings Fund - Direct Plan - Cumulative option' → ('DIRECT','GROWTH'); 'Samco Mid Cap Fund - Direct Plan' with no sibling → GROWTH after post-pass; same name WITH an '... - Direct Plan - IDCW' sibling → stays UNKNOWN; 'X Fund - Direct Plan - Cumulative IDCW' → IDCW.

**Acceptance:** uv run pytest tests/test_scheme_master.py -q passes. After the next pipeline run (scheme_master rebuild is network-touching — do not run here): psql -d mfs -c "SELECT scheme_code, option_type FROM scheme_master WHERE scheme_code IN ('133054','145075','145897','143874')" shows GROWTH for all 4, and the UNKNOWN-DIRECT-rankable count drops from 73 to <10; the 4 ICICI funds appear in computed_metrics at the next compute.

*Audit findings:* 1a critical 'Growth plans named Cumulative (ICICI Pru) or with no option token (Samco) silently excluded' (scheme_master.py:129-133); 1a 'reconcile the 73 DIRECT/UNKNOWN rows'

### B7 [M] Wire the monthly freshness gates: real thresholds in pipeline.yaml with per-source blocking rationale — depends on: STAGE1-urgent-1 (T-bill fix — lands before any gate-tightening so the run can pass at all)

*Files:* `configs/pipeline.yaml:33-42, (comment only) src/mfs/freshness.py docstring, docs note in docs/phase4/staged_pipeline_refactor_plan.md if it tracks gate posture`

**Change:** [REVIEW-NOTE] The max_constituents_lag_days row is OWNED BY D9 (75d, landing with the pipeline-integrated derive cadence) — B7 sets all other thresholds and must not set the constituents key. Replace the five nulls at configs/pipeline.yaml:36-42 with calibrated values + rationale comments (current MAX dates verified 2026-06-11): max_holdings_lag_days: 75 (table max 2026-05-01, lag 41d — one missed month + slack), max_ptr_lag_days: 75 (max 2026-05-01; the freshness check gates the GLOBAL MAX(as_of_month) via q.latest_ptr_date, so quant's deliberate 2024-03 annual-report rows can never trip it — no per-AMC exemption machinery needed, document this in the comment), max_aum_lag_days: 150 (quarterly AAUM; max 2026-03-01, lag 102d — halts only if a full quarter is skipped), max_stock_adv_lag_days: 10 (daily bhavcopy with a 75d ingest window), max_constituents_lag_days: 100 initially (max 2026-04-01, lag 71d — 75 would halt within days unless the tracker-derive job runs for May; tighten to 75 in the same PR that puts tools/derive_constituents.py on the monthly cadence). Halting semantics decision: these table-level checks run in check_freshness (cli.py:1168, raise_on_fail=True) and therefore HALT the pipeline — correct per fail-fast, because a stale global max means the entire source went dark for >1 cycle (systemic), unlike per-AMC gaps which stay in advisory Gate B (coverage.py:276-277 unchanged). Encode that classification as a comment block in pipeline.yaml ('BLOCKING = whole-source staleness via freshness.py; ADVISORY = per-fund coverage via Gate B'). No code change needed in freshness.py:144-175 — it already evaluates non-null thresholds.

**Tests:** Extend an existing freshness test or add tests/test_freshness_thresholds.py: with monkeypatched q.latest_holdings_date returning as_of-100d and the new config, check_freshness(raise_on_fail=True) raises FreshnessError naming 'Holdings'; with -41d it passes; same matrix for ptr/aum/stock_adv/constituents. Add a config-sanity test asserting none of the five thresholds is null anymore.

**Acceptance:** uv run pytest tests/test_freshness_thresholds.py -q passes; uv run mfs --help unaffected; dry verification against live DB: a check_freshness(as_of=date(2026,6,11)) call in a REPL (read-only) returns ok=True with the new thresholds (no insta-halt).

*Audit findings:* 1a/8 'Nothing blocks ranking on stale monthly signals: all five thresholds null, Gate B never halts' (pipeline.yaml:36-42, freshness.py:157)

### B8 [M] Translate uncaught 4xx/RuntimeError into IngestError at required-stage ingester entry points

*Files:* `src/mfs/ingest/bhavcopy.py:60-80, 202+, src/mfs/ingest/amfi_aum.py:90-120, 196+, src/mfs/master/scheme_master.py:168-175`

**Change:** cli.py:1069 _stage catches only (PipelineError, IngestError); three required stages can currently abort the run with raw tracebacks. Wrap at the top-level entry points (matching the pattern amfi_nav.ingest_today already uses at amfi_nav.py:203-206): (1) bhavcopy.py ingest_recent (line 202) — wrap the body's fetch loop boundary: except httpx.HTTPError as e: raise IngestError(f'bhavcopy: NSE fetch failed: {e}') from e (NSE archives 403 is the proven case; _fetch_with_retry calls raise_for_status at bhavcopy.py:68). (2) amfi_aum.py ingest_quarter (line 196) — same wrap around its fetch_bytes calls (io/http.py:48 raise_for_status path). (3) scheme_master.py build (lines 170-174) — wrap amfi_nav.fetch_today() in try/except httpx.HTTPError and convert the bare RuntimeError('Empty AMFI NAVAll snapshot...') at line 174 to IngestError. Do NOT change bhavcopy's required=True stage status: a clean IngestError halt preserves the fail-fast invariant; whether stock-ADV should degrade-continue is a separate product decision recorded as a risk, not made here.

**Tests:** Extend tests/test_bhavcopy.py: monkeypatched 403 response → ingest_recent raises IngestError (not httpx.HTTPStatusError). New asserts in tests/test_scheme_master.py: empty snapshot → IngestError. New small test for amfi_aum 4xx → IngestError (monkeypatch http.fetch_bytes to raise httpx.HTTPStatusError).

**Acceptance:** uv run pytest tests/test_bhavcopy.py tests/test_scheme_master.py -q passes; grep -n 'RuntimeError' src/mfs/master/scheme_master.py returns nothing; a simulated 403 in a REPL exits the pipeline via the '[pipeline] FAIL at <stage>' path (exit 2), not a traceback.

*Audit findings:* 8 'Uncaught 4xx / RuntimeError in required pipeline stages escape _stage as raw tracebacks' (cli.py:1069, bhavcopy.py:68, scheme_master.py:171-174); memory: 'uncaught 4xx crashes pipeline'

### B9 [M] --full re-downloads current-month artifacts (fetch-layer cache revalidation) — depends on: B1

*Files:* `src/mfs/ingest/managers/_run.py:72-104, src/mfs/ingest/holdings/_run.py:41-56 and 103-117 and 205-242, src/mfs/cli.py:1030-1036 and 1158, src/mfs/ingest/managers/_base.py:51-65 (docstring), src/mfs/ingest/holdings/_generic.py:309-326 (docstring)`

**Change:** [REVIEW-NOTE] force semantics unified with C5: a single --full/force flag means re-download AND re-parse AND re-write (one signature, one cli.py call-site change, coordinated with C2/C5). Today --full bypasses only the parse-skip (managers/_run.py:99-100 + run_all(force)); cached PDFs/Excels are returned by mere existence (managers/_base.py:62-63, _generic.py:324-326) so corrected/republished files are unreachable without manual deletion. Implement deletion-based revalidation, scoped to the run's data month (bounded cost, no signature changes to the many bespoke fetch overrides): (1) managers/_run.py run_for_amc — when force=True and pdf_path is None, delete paths.factsheet_raw(amc_slug, ym) if it exists BEFORE calling adapter.fetch(ym) (every fetch implementation checks that canonical path). The existing _parse_cache sha256 marker then does the right thing: unchanged re-downloaded bytes still skip the re-parse only when force=False, and a changed file re-ingests. (2) holdings/_run.py — add force: bool = False to run_for_amc/run_all; when force, delete paths.holdings_excel_raw(amc_slug, ym, excel_filename) before adapter.fetch_excel at line 111. (3) cli.py:1158 — change the holdings stage to lambda: holdings.run_all(full-aware): holdings.run_all(ym=None, force=full). (4) Update the now-false docstrings ('Re-uses an existing cached PDF unconditionally — delete the file to force a re-fetch' in _base.py and _generic.py) and the --full help text at cli.py:1030-1036 to state that --full re-downloads the current data month's artifacts. Document the cost: ~640 holdings Excels + ~41 factsheets re-fetched (~minutes serially, hours if kotak's month probe fails — B11 bounds that).

**Tests:** Extend tests/test_incremental_ingest.py: with a cached artifact on disk and a monkeypatched downloader serving NEW bytes, run_for_amc(force=True) re-downloads and parses the new bytes (assert downloader called, parsed content reflects new bytes); force=False does not call the downloader. Same pair for holdings run_for_amc.

**Acceptance:** uv run pytest tests/test_incremental_ingest.py -q passes; mfs pipeline --full --skip-phase2 unchanged in behavior (Phase-1 paths untouched); code review confirms deletion is scoped to ym only (historical months never re-fetched).

*Audit findings:* 8 'Fetch layer trusts cached artifacts by mere existence; even --full never re-downloads' (_base.py:54-58, _generic.py:324, managers/_run.py:99-100); memory: incremental-by-default + --full escape hatch

### B10 [S] Close the nav_daily → fund_log_returns two-transaction crash window; add Gate A reconciliation check

*Files:* `src/mfs/db/writers.py:77-93, 360-393, src/mfs/coverage.py (Gate A custom check) or src/mfs/freshness.py:86-97`

**Change:** MERGED INTO C4 — do not implement separately. C4 owns the same-transaction incremental refresh + Gate A reconciliation.

**Tests:** Extend tests/test_atomic_write.py or new tests/test_nav_logreturns_atomicity.py: monkeypatch connect() to count distinct connections during upsert_nav_daily → exactly one; inject an exception after the nav upsert sub-step and assert neither table changed (transaction rolled back, using a test DB/fixture conn). Gate test in tests/test_coverage.py: mismatched max dates → blocking failure with the remediation string.

**Acceptance:** uv run pytest tests/test_coverage.py -q passes; psql -d mfs -c "SELECT (SELECT MAX(nav_date) FROM nav_daily WHERE nav>0)=(SELECT MAX(date) FROM fund_log_returns)" returns t and the new gate reports OK on the live DB.

*Audit findings:* 8 'fund_log_returns cache refreshed in a second transaction after NAV commit; no gate detects divergence' (writers.py:86-92, 360-393)

### B11 [M] Kotak absent-month probe + TTL'd negative cache (kills the 2.6h/run retry pathology) — depends on: B1

*Files:* `src/mfs/ingest/holdings/kotak.py:153-154, 261-284, 354-399`

**Change:** kotak.py: discovery (lines 261-284) builds URLs for ~120 funds without checking the month exists; each unpublished-month download then burns 20 retries x 1.5s (lines 153-154, 375-385) ≈ 2.6h/run during the first ~10 days of a month. Add a month-existence probe in discover_scheme_urls (or at the top of the orchestrator call path): pick TWO stable fund codes from the schemesearch catalog (two, so one delisted fund can't false-negative) and call the cheap folderlist leaf f'{_API}/folderlist?scheme=<code>/<year>/<month>/Consolidated' with a SMALL budget (5 attempts, 1.5s sleep — the transient-400 flakiness self-heals within seconds per the module docstring; a truly absent month 400s deterministically). If BOTH probes exhaust their budget → write a TTL'd negative-cache marker data/raw/holdings/kotak/<ym>/.month_absent containing an ISO timestamp, then raise IngestError(f'kotak: month {ym} not yet published (probe negative)') — per-AMC isolation in holdings/_run.py:221-232 records it and the run continues; coverage Gate B attributes the gap. On the next run, if the marker exists and is younger than 20h, raise the same IngestError immediately (zero HTTP); older marker → delete and re-probe, so a newly published month is picked up at most one day late and the no-half-data invariant is never violated (we skip the AMC entirely, never partially). Keep _MAX_RETRIES=20 for individual schemes once the month is confirmed present (real flakiness needs the budget).

**Tests:** New tests/test_holdings_kotak.py with a mocked httpx client: (a) month absent (folderlist 400s deterministically) → IngestError after exactly 2 probe budgets (≤10 leaf calls), marker file written; (b) second call within TTL → IngestError with zero HTTP calls; (c) marker older than 20h + month now present → probe passes, downloads proceed, marker removed; (d) month present → no behavior change vs today.

**Acceptance:** uv run pytest tests/test_holdings_kotak.py -q passes; simulated absent-month run completes the kotak adapter in <60s (assert via the mocked-clock test), vs ~9,400s measured in the 2026-06-08 run.

*Audit findings:* 1c 'Kotak holdings adapter burns ~2.6h per run retrying downloads for an unpublished month' (kotak.py:153; verifier-recalibrated to medium severity, but top speed win)

### B12 [M] Adapter auto-registration via pkgutil + registry==package test; activate the three dormant manager adapters with an explicit BLOCKED ledger — depends on: B1

*Files:* `src/mfs/ingest/managers/__init__.py:24-66, src/mfs/ingest/holdings/__init__.py, src/mfs/ingest/managers/_run.py:304-332, src/mfs/ingest/managers/trust.py + jio_blackrock.py (docstrings), tests/test_adapter_registry.py (new)`

**Change:** (1) Replace the hand-maintained import lists in managers/__init__.py:24-66 (and, for symmetry/regression-proofing, holdings/__init__.py which is currently complete but equally hand-maintained) with pkgutil.iter_modules auto-import of every non-underscore module in the package — a new written adapter can never again be silently dormant. (2) Activation decisions per docstring evidence: the_wealth_company — activate plainly; its parse_ptr correctly yields zero records until the AMC prints PTR (docstring: funds <8 months old), so registration writes nothing and records a clean per-AMC result. trust — activate; the WAF 200-HTML wall (docstring 'BLOCKED on download') now fails LOUDLY at fetch because B1 rejects the HTML shell before caching, which is the audit's preferred 'recorded per-AMC failure' outcome. jio_blackrock — activate; its auth-gated Strapi document index makes fetch fail per-AMC, also loud. To keep known-walled AMCs from reading as regressions, add a module-level _KNOWN_BLOCKED: dict[str, str] = {'trust': 'WAF serves HTML shell for all PDF paths (see module docstring)', 'jio_blackrock': 'factsheet index behind auth-gated Strapi API'} in managers/_run.py; run_all annotates those slugs' failure rows with blocked_reason so Gate B/run summaries distinguish 'known-walled' from 'newly broken'. (3) Fix the now-false docstrings (trust.py 'the parser below is written and registered' becomes true; jio_blackrock.py 'picked up automatically'). (4) New tests/test_adapter_registry.py: for each package, assert set(registered_adapters()) equals the set of amc_slug values of adapter classes found by walking the package modules — this test fails today for managers (3 missing) and passes after the change; also assert _KNOWN_BLOCKED keys ⊆ registered slugs.

**Tests:** tests/test_adapter_registry.py as described (registry == package contents for both packages; blocked-ledger keys registered; no module defining an adapter class is unregistered). Existing tests/test_adapter_isolation.py must still pass (run_all isolation semantics unchanged).

**Acceptance:** uv run pytest tests/test_adapter_registry.py tests/test_adapter_isolation.py -q passes; uv run python -c "from mfs.ingest import managers; print(sorted(managers.registered_adapters()))" lists 44 slugs including jio_blackrock, trust, the_wealth_company.

*Audit findings:* 1b 'Three written manager adapters never imported, @register_adapter never runs' (managers/__init__.py:24); dims 5/7 duplicates ('hand-maintained import list', 'no registry-completeness test')

### B13 [S] Partition-shrinkage guard: refuse DELETE+reinsert that loses previously-good schemes

*Files:* `src/mfs/ingest/holdings/_run.py:172, src/mfs/ingest/managers/_run.py:168-177`

**Change:** Before the partition DELETE, compare incoming distinct-scheme count vs the existing DB partition for (source_amc, month). Any shrinkage -> IngestError for that AMC (fail-fast; --force overrides with a loud log). Today a scheme that parsed last run but fails this run silently loses its prior-good rows.

**Tests:** Existing partition 10 schemes, new parse yields 7 -> refused, DB untouched; 10->10 or 10->12 -> proceeds.

**Acceptance:** Simulated partial-failure re-run leaves the prior partition intact and reports the refusal.

*Audit findings:* 1b holdings re-run silently deletes prior schemes [medium] — owned by NO task before review

### B14 [S] PTR plausibility bounds + unit-flip tripwire; holdings weight bounds

*Files:* `src/mfs/schemas.py:121-133, src/mfs/ingest/managers/_run.py`

**Change:** Pydantic bounds: PtrRecord ptr gt=0 le=25; holdings weight_pct ge=-5 le=110 (shorts/derivatives margin). Month-over-month tripwire: if an AMC's median PTR shifts >5x AND >0.5 absolute vs its previous stored month -> IngestError for that AMC (catches a percent-vs-fraction convention flip writing 100x-wrong PTR).

**Tests:** ptr=9.14 (percent leaking through as 0.0914*100) vs prior 0.09 -> refused; normal drift -> passes.

**Acceptance:** Unit-flip fixture refused at ingest; existing 41 AMC parses still pass.

*Audit findings:* 1b geometry picks adjacent number / 7 unit-flip tripwire missing [medium]

### B15 [S] Gate B contract-query failures become status=ERROR, never silent EMPTY ok=True

*Files:* `src/mfs/coverage.py:397-406`

**Change:** A contract evaluation exception -> status ERROR; ok=False if the source is blocking-grade, loud render for advisory. Today broken gate SQL is indistinguishable from a legitimate data gap.

**Tests:** Monkeypatched query raising -> ERROR row in render; blocking source -> gate fails.

**Acceptance:** `coverage.run_gate` with an induced SQL error shows [ERROR], not [EMPTY ok].

*Audit findings:* 8 Gate B swallows query errors [low]

**Workstream risks:**

- B5 matcher tightening can orphan currently-valid matches and silently shrink holdings/PTR coverage — the mandatory old-vs-new sweep over cached artifacts (B5 step 5) is the control; do not merge without reviewing its diff, and watch Gate B entity counts on the first post-merge run.
- B6 adds 4+ funds to the ranked universe and changes the DIRECT+GROWTH candidate index that fuzzy matching is built on (build_candidate_index filters option_type=GROWTH) — previously-unmatchable printed names (e.g. Motilal 154239) start matching, and the _parse_cache universe fingerprint will force re-parses across all AMCs on the next run (expected, but the run will be slower once). Land B5 in the same release so new candidates enter under the stricter matcher.
- B7 makes whole-source staleness blocking: if the constituents tracker-derive job (tools/derive_constituents.py) is not put on a monthly cadence, max_constituents_lag_days=100 halts the pipeline around mid-July 2026. The threshold values encode today's DB state — re-verify MAX dates at merge time.
- B4's partial-day exclusion relies on the Stage-1 ingest_since switch to eventually deliver the excluded day; if Stage 1 slips, a rejected partial day plus the 5-bday NAV freshness gate could halt runs near publication boundaries (correct per fail-fast, but operationally surprising — document in run-pipeline.md).
- B9 --full now re-downloads the current month's ~680 artifacts; against flaky hosts (kotak prodtest, NSE) this lengthens --full runs and increases exposure to transient failures — per-AMC isolation contains it, but advise running --full off-peak.
- B1 content validation could reject a legitimate non-standard payload (e.g. an AMC serving xlsx with a BOM or a CSV-as-xls) — the sniffer must be tested against the real cached corpus in data/raw/ before merge (cheap: run sniff_kind over all 1,357 cached holdings files and 41 factsheets; expect zero 'html'/'unknown' on currently-parseable files except the 5 known-corrupt axis files).
- The B5 purge and axis cache deletion are one-time manual mutations of live state; they must be run by the engineer at deploy time (this planning environment is DB-read-only) and noted in the run log so the next ingest's shrinkage isn't misread as a regression.

**Ordering notes:** B1 first — it is the foundation: B2's cache-eviction semantics, B9's re-download safety, B11's kotak byte-writes, and B12's trust activation all assume content validation exists. Then two independent parallel tracks: (a) correctness-of-matching/universe: B5 → B6 (same release; B6's new GROWTH candidates must enter under B5's stricter matcher; run the one-time 152651 purge with B5), then B2 and B3 (per-scheme gates, independent of each other); (b) operational: B8, B10, B11, B12 in any order. B4 should land with or after Stage-1 urgent item 2 (ingest_since switch) since its reject-partial-day semantics depend on backfill existing. B7 goes LAST: enable blocking thresholds only after Stage-1 urgent items 1-3 (T-bill fix, NAV backfill, quant purge) and the constituents monthly cadence are confirmed, otherwise the new gates halt on known-stale state. Cross-workstream: B2 extends (not duplicates) the Stage-1 quant statement-date fix; B6 feeds the compute/ranking workstream a changed universe — coordinate the first recompute; B7's gate posture should be reflected in run-pipeline.md (docs workstream, if any).


## Workstream C — Collection speed

Collection is ~98% of pipeline wall-clock (audit dim 1c, grade C-): every per-AMC/per-ticker loop is serial, daily NAV ingest rewrites the 28M-row fund_log_returns cache, benchmarks are exposed to NSE latency (2.2 vs 33.8 min), and T-bill warm runs re-parse 4.56GB twice. This workstream adds stage timing first (so every win is measurable), then parallelizes the three I/O-bound loops with unchanged per-AMC fault-isolation and per-AMC transactions, makes log-return refresh incremental from min(new NAV date) per scheme, ports the managers parse-skip to holdings (decision: the win is skipping DELETE+reinsert churn, not the ~18s parse), trims T-bill warm/cold cost, and caches invariant compute series. Targets (with workstream B's kotak + RBI fixes): monthly refresh ~45-75 min clean / ~3 h pathological → ≤20 min; weekly ~25-40 min → ≤12 min; cold start ~13-16 h → ~5-7 h.

### C1 [S] Per-stage wall-clock instrumentation in `mfs pipeline`

*Files:* `src/mfs/cli.py:1065-1074, 1078-1094, 1179-1188`

**Change:** In src/mfs/cli.py:1065-1074, wrap the `_stage` helper's `fn()` call with time.perf_counter and (a) echo `[pipeline] {name} done in {elapsed:.1f}s`, (b) append (name, seconds) to a `stage_timings` list, (c) emit structlog `pipeline.stage.done` with stage+seconds for machine-readable history. In the `finally` block (cli.py:1179-1188), after the coverage summary, print a 'STAGE TIMING SUMMARY' table (guarded by try/except like the coverage render so a render error can't mask the real exception). Also time `_coverage_gate` (cli.py:1078-1094) the same way. Extract the timed-call core into a small module-level helper so it is unit-testable. This is the measurement basis for the before/after acceptance of C2-C6 — the audit had to derive all 1c timings from file mtimes and DB computed_at.

**Tests:** New tests/test_pipeline_stage_timing.py: call the extracted timed helper with a stub fn (sleep 0.05s), assert the timing record is captured and the summary renderer includes the stage name and a positive duration; assert an exception inside fn still propagates after the timing record is dropped/recorded.

**Acceptance:** `uv run pytest tests/test_pipeline_stage_timing.py -q` passes. On the next user-run pipeline, stdout shows per-stage `done in Xs` lines and a final timing summary table.

*Audit findings:* dim 1c overall (timings reconstructed from mtimes/computed_at); enables verification of all other 1c remediations

### C2 [M] Parallelize holdings and managers run_all over AMCs (ThreadPool, fault isolation preserved) — depends on: F-5

*Files:* `src/mfs/ingest/holdings/_run.py:205-242; src/mfs/ingest/managers/_run.py:281-332; src/mfs/config.py:133-138`

**Change:** Replace the serial loops in src/mfs/ingest/holdings/_run.py:221-232 and src/mfs/ingest/managers/_run.py:309-322 with a concurrent.futures.ThreadPoolExecutor submitting `run_for_amc` per slug and collecting via as_completed. Preserve exactly: (a) results dict keyed by slug with the same success/error shapes (error/error_type/rows_written* keys), (b) the `failed` list + `run_all.partial` warning, (c) the all-failed systemic IngestError (raise only after the pool drains; keep the deterministic 'first error' by reading results[sorted(slugs)[0]]). Worker count: add `ingest_workers: int = 6` to Settings (src/mfs/config.py:133-138, env MFS_INGEST_WORKERS); use min(ingest_workers, len(slugs)). Politeness: each in-flight task targets ONE distinct AMC host and per-AMC downloads stay serial inside run_for_amc, so per-host concurrency remains 1 — no per-host throttling code needed. DB safety: run_for_amc opens fresh psycopg connections per call (db/connection.py:27-40) and its DELETE+upsert transaction is keyed by (source_amc, as_of_month) (holdings/_run.py:176-182, managers/_run.py:170-177) — disjoint row sets across threads, no shared state to guard. httpx clients are per-call (io/http.py:24-30). MFS_INGEST_WORKERS=1 restores serial behavior for debugging. Note: the managers pdfplumber parse is GIL-bound, but on incremental runs the parse-skip cache means fetch I/O dominates; this is fine.

**Tests:** Extend tests/test_adapter_isolation.py: existing 4 isolation/all-fail tests must pass unchanged against the pooled implementation. Add test_managers_run_all_is_concurrent and test_holdings_run_all_is_concurrent: monkeypatch run_for_amc with a fake that waits on a threading.Barrier(3, timeout=5) — passes only if ≥3 AMCs run simultaneously. Add a test that MFS_INGEST_WORKERS=1 (monkeypatched settings) still produces identical results dicts.

**Acceptance:** `uv run pytest tests/test_adapter_isolation.py -q` passes. On the next monthly user-run pipeline (after workstream B's kotak fix), C1 timing shows 'ingest managers' + 'ingest holdings' combined ≤10 min vs the audit's ~40 min (audit bound: ~8 min).

*Audit findings:* Serial collection loops (dim 1c medium, holdings/_run.py:221 + managers/_run.py:309 + only fbil threaded)

### C3 [M] Parallelize benchmark per-ticker ingest; tighten NSE timeouts

*Files:* `src/mfs/ingest/benchmarks.py:404-443, 134-183`

**Change:** In src/mfs/ingest/benchmarks.py:404-443 (ingest_all_known), replace the serial `for ticker in NSE_INDEX_NAME_MAP` loop (:424-437) with a ThreadPoolExecutor(max_workers=3) over tickers — ALL tickers hit the same host (niftyindices.com), so cap at 3 to bound NSE load; tenacity already retries 429/5xx as TransientHttpError (benchmarks.py:169-170, decorator :134-139). Preserve semantics: per-ticker `out` dict, `equity_failures` collected from completed futures, and the aggregate IngestError raised only after ALL futures finish (same message format listing every failed equity ticker). Keep the `latest_map` watermark prefetch (:422) — it already avoids per-ticker re-query and is computed before the pool starts. Additionally, in fetch_tri_window (:164) replace `timeout=s.http_timeout` (60s) with an explicit httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0) so a hung NSE read fails into the tenacity retry in ≤30s — this bounds the per-window worst case that produced the 33.8-min run (~88s/ticker from latency/retries). upsert_benchmark_daily from threads is safe: per-call connections, disjoint ticker PK ranges.

**Tests:** Add to tests/test_incremental_ingest.py (which already stubs benchmarks): (1) monkeypatch ingest_ticker; assert all NSE_INDEX_NAME_MAP tickers appear in the result dict; (2) make two equity-mapped tickers raise; assert IngestError message lists both; (3) concurrency check via threading.Barrier(2, timeout=5) inside the stub; (4) non-NSE-mapped ticker returning 0 rows does NOT enter equity_failures (existing exemption preserved).

**Acceptance:** `uv run pytest tests/test_incremental_ingest.py -q` passes. C1 timing on subsequent user-run pipelines shows 'ingest benchmarks' ≤5 min even on a slow-NSE day (audit worst case 33.8 min → ~4 min) and ~1 min typical.

*Audit findings:* Serial collection loops — benchmark latency variance (dim 1c medium: 2.2 min vs 33.8 min for 23 incremental fetches, benchmarks.py:424-437)

### C4 [M] Incremental fund_log_returns refresh from min(new NAV date) per scheme; single rebuild after backfill (ABSORBS B10 — single implementation) — depends on: STAGE1-U2

*Files:* `src/mfs/db/writers.py:77-93, 360-420; src/mfs/ingest/amfi_nav.py:184-193, 222-290`

**Change:** [REVIEW-MERGED with B10] One implementation: incremental per-scheme-watermark refresh of fund_log_returns running ON THE SAME connection/transaction as the NAV upsert (closes the dim-8 two-transaction crash window, writers.py:77-93,360-393), plus B10's Gate A nav_max==flr_max reconciliation check. (a) Rework refresh_fund_log_returns (src/mfs/db/writers.py:360-393) to take per-scheme watermarks `{scheme_code: min_new_nav_date}` instead of bare codes: load the pairs into a TEMP table; one set-based `DELETE FROM fund_log_returns USING tmp WHERE scheme_code matches AND date >= tmp.min_date`; one INSERT...SELECT computing LAG-based log returns over only `nav_daily` rows with `nav_date >= (the latest positive-NAV date strictly before min_date for that scheme, via a scalar subquery/LATERAL), nav > 0`, inserting only rows with date >= min_date. Correctness invariant: a batch can only change NAVs it contains, so returns for dates < batch-min are untouched; the day-of-min return needs exactly one preceding NAV, which the lookback row supplies. (b) In upsert_nav_daily (writers.py:77-93), compute the per-scheme min nav_date from the batch (group_by aggregation on `out`) and call the new refresh ON THE SAME connection/transaction as the nav_daily upsert — now cheap, and it closes the audit's NAV-commit-then-refresh two-transaction crash window (dim 11 finding; reference, do not build separate machinery). (c) Backfill: thread a `refresh_log_returns: bool = True` parameter through upsert_nav_daily and _append_year_partition (src/mfs/ingest/amfi_nav.py:184-193); ingest_backfill (:222-290) passes False per 90-day window and calls w.refresh_fund_log_returns_all() (writers.py:396-420, currently unused by this path) exactly once after the window loop when total_rows > 0 and no IngestError was raised. ingest_today keeps refresh=True. Expected: daily NAV stage ~3 min → <40s; backfill saves the ~1-2.5h of 54 redundant full-table refreshes.

**Tests:** New tests/test_log_returns_refresh.py: (1) unit — monkeypatch the refresh fn; upsert_nav_daily passes the correct {scheme: min_date} mapping for a mixed-date batch; (2) backfill orchestration — monkeypatch upsert_nav_daily + refresh_fund_log_returns_all, run ingest_backfill over local fixture windows, assert per-window refresh suppressed and the one-shot rebuild called exactly once; (3) DB equivalence (skipif MFS_TEST_DB_URL unset, runs against a scratch test database, never the live 'mfs' DB): seed 2 schemes × 10 NAVs incl. a zero-NAV row, apply a batch containing 1 appended day + 1 revised in-batch historical NAV, assert fund_log_returns is byte-identical to a from-scratch refresh_fund_log_returns_all rebuild.

**Acceptance:** `uv run pytest tests/test_log_returns_refresh.py -q` passes. After the next user-run daily pipeline: C1 timing shows 'ingest navs (today)' <40s, and `psql -d mfs -c "SELECT (SELECT MAX(date) FROM fund_log_returns) = (SELECT MAX(nav_date) FROM nav_daily)"` returns t.

*Audit findings:* NAV log-return full-history rewrite per scheme per file (dim 1c medium, writers.py:91); cross-references dim 11 'two-transaction refresh' finding | dim 8 fund_log_returns two-transaction window (citation fixed: dim 8, not 11)

### C5 [M] Holdings parse-skip parity: skip parse + DELETE/reinsert when provably no-op (decision: churn is the win, not CPU) — depends on: C2

*Files:* `src/mfs/ingest/holdings/_run.py:41-202, 205-242; src/mfs/ingest/_parse_cache.py; src/mfs/db/queries.py:365-382 (pattern); src/mfs/cli.py:1158`

**Change:** [REVIEW-NOTE] force flag shares B9's unified semantics (re-download AND re-parse AND re-write). DECISION: re-parse CPU is minor (audit: ~15ms/file, ~18s total), so do NOT build anything that skips discovery HTTP — discovery is what detects late-published schemes within a month (incremental-by-default invariant). The win is skipping the per-run DELETE+reinsert of ~96k holdings_monthly rows and restoring behavioral parity with the managers path so the two orchestrators stop drifting. Implementation in src/mfs/ingest/holdings/_run.py run_for_amc (:41-202): after winners are resolved (:96) and each winner Excel is fetched (fetch is already disk-cached unconditionally, holdings/_generic.py:322-326), compute an aggregate fingerprint = _parse_cache.fingerprint over sorted '{printed_name}:{sha256(excel)}' entries for all winners, combined with the universe fingerprint built exactly as managers/_run.py:125-127. Add q.has_holdings_rows(amc_slug, as_of_month) to src/mfs/db/queries.py mirroring has_factsheet_rows (:365-382) but checking only holdings_monthly with source_amc. Skip the parse loop AND the DELETE+upsert transaction (:103-182) iff marker matches AND has_holdings_rows — write the marker (per-(amc,ym), e.g. data/raw/holdings/<amc>/<ym>/.ingested.json via a thin dir-keyed wrapper in _parse_cache.py) only after the commit, like managers/_run.py:185-189. Thread `force: bool = False` through holdings run_for_amc/run_all and wire `--full`: change cli.py:1158 to `lambda: holdings.run_all(force=full)`. Side benefit (reference workstream B, do not own): unchanged-month runs no longer enter the DELETE window at all, shrinking exposure to B's 'partial per-scheme failures silently delete data' finding.

**Tests:** New tests/test_holdings_parse_skip.py modeled on tests/test_parse_cache.py + tests/test_adapter_isolation.py: fake adapter with 2 fixture Excels and stubbed queries/writers/connect. Assert: first run parses and writes the marker; second run skips (parse_excel not invoked, no DELETE executed, result has skipped=True); mutating one Excel's bytes, OR changing the candidate universe, OR force=True each trigger a full re-parse; a run with db_has_rows=False never skips.

**Acceptance:** `uv run pytest tests/test_holdings_parse_skip.py tests/test_adapter_isolation.py -q` passes. On a mid-month user-run pipeline, logs show `holdings.run.skip_unchanged` for unchanged AMCs and C1 timing shows the holdings stage ≤2 min (excluding kotak until B lands).

*Audit findings:* Holdings lacks managers-style parse-skip (dim 1c low, holdings/_run.py:103); dual-orchestrator drift (dim 8 low, _parse_cache imported only by managers/_run.py:24); DELETE+reinsert churn

### C6 [M] T-bill: eliminate warm-run double cache scan; raise cold-start workers 4→8 (low priority) — depends on: B (RBI shell-page absence-detection fix) — for the warm-run wall-clock target only; code changes are independent

*Files:* `src/mfs/ingest/fbil_tbill.py:410-476, 512-545, 548-648; src/mfs/cli.py:86-114`

**Change:** [REVIEW-TRIMMED] Keep cold-start workers at 4 (politeness against RBI; the 4->8 bump is dropped). Scope = warm-run double-cache-scan elimination only. Three changes in src/mfs/ingest/fbil_tbill.py. (1) Warm-run reparse skip: add module const _PARSER_VERSION (bump whenever _parse_tbill_html/_tbill_result_title/_YTM_BLOCK_RE change); persist it in the state file. In ingest_from_rbi_press_releases (:569-573), replace `existing = reparse_cache() or _load_scraped_csv()` with: if state's parser_version == _PARSER_VERSION and the scraped CSV exists → `_load_scraped_csv()`; else reparse_cache() (:512-545, measured 28.7s over 36k files/4.56GB) and stamp the version. (2) Gap-fill missing-set: refactor _walk_prid_range (:410-476) to accept an explicit list of prids; before the gap-fill pass (:600-612), enumerate cached prids with ONE scandir of the cache dir (parse 'prid_N.html' filenames + size>200 check via entry.stat — no file reads), compute missing = range(gap_lo, gap_hi+1) minus cached; if empty, skip the walk entirely (log fbil.rbi.gap_fill.no_holes); else walk only the missing list. This removes the second full-cache read every warm run. (3) Cold start: change max_workers default 4→8 at :414 and :553, keep rate_limit_s=0.25 per worker; measured 0.95 prid/s at 4 workers is latency-bound (~4s/req), so 8 workers ≈ ~1.9/s → 13.5y walk ~10.5h → ~3.5-5h. The existing max_tbill_scrape_failure_rate gate (:621-628) remains the politeness backstop — if RBI throttles, the run halts rather than ingesting partial RF data (fail-fast invariant preserved). Expose `--workers` (default 8) on `mfs ingest tbill` (src/mfs/cli.py:86-114). NOTE: the ~7 min/run dead tip-probing and the 793 poisoned shell-page cache entries are workstream B's RBI shell-page fix — do not duplicate; this task's warm-run target assumes it lands.

**Tests:** Extend tests/test_fbil_tbill.py / tests/test_fbil_discovery.py: (1) fully-cached tmp cache dir → gap-fill performs zero HTTP calls (monkeypatch _fetch_prid_html to raise if called) and logs the no-holes path; (2) cache dir with 3 holes → exactly those prids are walked; (3) parser_version match skips reparse_cache, mismatch (or missing CSV) triggers it and re-stamps state (count calls via monkeypatch); (4) _walk_prid_range over an explicit prid list returns identical hits/failure accounting as the old range form.

**Acceptance:** `uv run pytest tests/test_fbil_tbill.py tests/test_fbil_discovery.py -q` passes. After B's shell-page fix lands, a warm user-run `uv run mfs ingest tbill` completes in ≤2 min with `fbil.rbi.gap_fill.no_holes` in the log (audit warm cost ~6.5 min). Cold-start improvement (~10.5h → ~3.5-5h) is an estimate; verify opportunistically on the next from-scratch environment, not as a gating check.

*Audit findings:* T-bill cold start ~10.5h at 4@0.25s (dim 1c medium, fbil_tbill.py:440,447); warm runs re-parse the full 36,116-file cache twice; RBI shell-page probing referenced (owned by B)

### C7 [S] Cache invariant calendar/benchmark/RF series per compute run

*Files:* `src/mfs/compute/alignment.py:31-47; src/mfs/compute/orchestrator.py:137-162, 257-281, 48`

**Change:** In src/mfs/compute/alignment.py, apply functools.lru_cache(maxsize=None) to master_calendar() (:31-33), _benchmark_series(ticker) (:36-38), and _risk_free_series() (:41-42); add alignment.clear_caches() invoking .cache_clear() on all three. Call clear_caches() at the top of orchestrator.run_phase1 (src/mfs/compute/orchestrator.py:137-162, before the per-scheme loop at :166) and run_phase2 (:257-281), so a pipeline process that ingested fresh data earlier in the same run never serves pre-ingest frames. This collapses ≈2,000 redundant Postgres queries per Phase-1 run (665 schemes × calendar+bench+RF, queries.py:18-76 has no caching; orchestrator._data_quality_flag also re-fetches the calendar per scheme at orchestrator.py:48). Cached frames are only filtered/joined downstream (align_scheme is non-mutating), and the working set is small (~26 bench frames × ~3.3k rows + calendar + RF ≈ a few MB). Audit verdict: compute wall-clock is fine overall (85s) — keep this S; the value is removing pointless DB load during compute, not headline minutes.

**Tests:** New tests/test_alignment_caching.py: monkeypatch mfs.db.queries.benchmark_series/master_calendar/risk_free_series with counting stubs returning small frames; call align_scheme for two schemes sharing one ticker; assert one underlying call each for calendar/bench/RF; assert clear_caches() forces a re-fetch; assert align_scheme results are equal before/after caching for a fixture scheme (guards against accidental frame mutation).

**Acceptance:** `uv run pytest tests/test_alignment_caching.py tests/test_metrics_math.py -q` passes. C1 timing on the next user-run pipeline shows 'compute phase1' ≤85s (non-regression; expected ~60-70s).

*Audit findings:* Compute layer re-queries invariant series per scheme — ≈2,000 redundant queries (dim 7 low, alignment.py:55-64, queries.py:18-61 uncached)

**Workstream risks:**

- Monthly wall-clock target (≤20 min) is conditional on workstream B's kotak negative-cache fix: with C2 alone, kotak's 20-retry dead-month loop still pins one pool thread for ~2.6h, so first-10-days-of-month runs stay ~2.6h wall-clock (max over workers, not sum).
- NSE throttling under 3-way benchmark parallelism: all 26 tickers hit niftyindices.com; if NSE rate-limits harder than tenacity's 429 retry absorbs, the equity-failure aggregation raises IngestError and halts the run (fail-fast preserved, but a new halt mode). Mitigation: start at 3 workers, keep an env knob to force serial.
- Parallel per-AMC writes: holdings/PTR transactions are keyed by (source_amc, as_of_month) so row sets are disjoint, but a cross-AMC fuzzy mis-match writing the same scheme_code is theoretically possible (pre-existing wrongness mechanism, audit dim 1b); parallelism does not worsen correctness but can surface upsert contention as retries/lock waits — watch the first parallel runs' logs.
- Incremental log-return refresh is correctness-critical for every downstream metric: a bug silently corrupts fund_log_returns, which compute reads in preference to recomputing (alignment.py docstring). The DB-equivalence test (C4 test 3) is the guard; do not ship C4 without it.
- RBI politeness at 8 workers (~1.9 req/s) exceeds the measured ~0.95/s historical rate; if RBI degrades, the existing >5% failure-rate gate halts the walk — acceptable for a one-time cold start but means a cold start may need re-running.
- lru_cache in alignment outlives a single run in long-lived processes (tests, notebooks, audit-scheme CLI): anything calling align_scheme outside run_phase1/run_phase2 after fresh ingest could see stale series; clear_caches() is wired into both orchestrator entry points, and compute_for_scheme back-compat path should be documented as potentially cached.
- Interleaved parallel logs make per-AMC debugging harder; structlog already tags amc= on every event, but the human-readable pipeline narration loses strict ordering.

**Ordering notes:** Land C1 first — it is the measurement basis for every other acceptance check (the audit had to reconstruct timings from file mtimes). C2, C3, C4, and C7 are mutually independent and can proceed in parallel branches; C2 and C3 are the headline wins and should land next. C5 depends on C2 (both rewrite holdings/_run.py run_for_amc/run_all — land after to avoid conflicting edits) and is also the lowest-urgency of the M tasks; its churn-skip additionally narrows the DELETE-window exposure that workstream B tracks. C6 is low priority (cold start is one-time; warm-run waste is ~1 min plus the ~7 min probe that workstream B's RBI shell-page fix removes — C6's warm acceptance target assumes B has landed, though the code changes don't conflict). Kotak dead-month retries are explicitly OWNED BY WORKSTREAM B; C2's monthly target is conditional on it. Wall-clock targets to verify via C1 after all tasks + B land: monthly refresh ~45-75 min clean / ~3 h pathological → ≤20 min; weekly refresh ~25-40 min → ≤12 min; first full run ~13-16 h → ~5-7 h (T-bill ~3.5-5 h, NAV backfill ~0.75-1.5 h, benchmarks ≤15 min, holdings+factsheets ~10-15 min).


## Workstream D — Methodology validity & validation

Make the ranking methodology validatable and stop the silent optimistic tilt. Four thrusts: (1) stop destroying history — diff-based scheme_master rebuild (keep departed funds as is_active=false) plus per-run history tables for universe, category/benchmark assignments, filter survivors and z-scores/composites, so point-in-time analysis becomes possible starting now; (2) validate — a retro-IC backtest tool over quarterly NAV-only stage-1 recomputations 2016+, with pre-registered justify/refute thresholds and weight-perturbation sensitivity; (3) fix the benchmark layer — fit diagnostics, Value/Energy remapping, hybrid debt-sleeve fix-or-disclose, and a per-category passive-alternative row with an explicit 'index wins' verdict; (4) close data-validity gaps — real AMFI TER ingestion replacing the invariant-violating manual stub, an active-share activation contract so the 0.10 weight doesn't silently stay dead, and documented (not silent) ret_median/ret_p25 redundancy. DB history tables (not parquet) chosen because backtests join against nav_daily/computed_metrics in SQL and computed_metrics already proves the as_of-partition pattern.

### D1 [M] Survivorship containment: diff-based scheme_master rebuild, departed schemes become is_active=false

*Files:* `/Users/suryavamseeayyagari/mfs/src/mfs/db/writers.py:133-151, /Users/suryavamseeayyagari/mfs/src/mfs/master/scheme_master.py:205,247, /Users/suryavamseeayyagari/mfs/tests/test_scheme_master.py`

**Change:** Replace the TRUNCATE+COPY full reload with a transactional diff sync. Files: /Users/suryavamseeayyagari/mfs/src/mfs/db/writers.py:133-151 (upsert_scheme_master; TRUNCATE at :146) — add new writer sync_scheme_master(df) that, in ONE transaction: (a) INSERT ... ON CONFLICT (scheme_code) DO UPDATE for every row in today's AMFI snapshot (snapshot wins on name/category/benchmark; is_active=true, last_seen_date=today); (b) UPDATE scheme_master SET is_active=false for scheme_codes present in DB but absent from today's snapshot — preserve their existing last_seen_date and all other columns; (c) never DELETE. /Users/suryavamseeayyagari/mfs/src/mfs/master/scheme_master.py:247 — call w.sync_scheme_master(df) instead of upsert_scheme_master (the is_active=True hardcode at scheme_master.py:205 is fine; it applies only to snapshot rows). Add a fail-fast guard: if the sync would deactivate >20% of currently-active rows, raise RuntimeError (a partial NAVAll page must halt, not mass-deactivate — fail-fast invariant). Do NOT resurrect the 12,874 historical dead nav_daily codes (verified live: SELECT count(*) FROM (SELECT DISTINCT scheme_code FROM nav_daily EXCEPT SELECT scheme_code FROM scheme_master) = 12,874) — they have no AMFI metadata to classify; inactive rows accumulate only from the first post-change disappearance. Downstream is already safe: orchestrator.run_phase1 (orchestrator.py:151-156) and coverage._rankable_universe_df (coverage.py:311) filter is_active=True, and idx_scheme_master_active_direct_growth (schema.sql:49-51) has the is_active predicate. Keep upsert_scheme_master only if tests need it; otherwise delete to prevent regression to TRUNCATE.

**Tests:** Extend tests/test_scheme_master.py (mock connection or refactor sync logic into a pure diff function + thin writer): (1) scheme present yesterday, absent today -> row persists with is_active=false and frozen last_seen_date; (2) reappearing scheme flips back to is_active=true; (3) >20% deactivation raises; (4) brand-new scheme inserted active.

**Acceptance:** uv run pytest tests/test_scheme_master.py -q passes. After the next live pipeline run: psql -d mfs -c "SELECT is_active, count(*) FROM scheme_master GROUP BY 1" — total row count is >= the prior run's count (monotone non-decreasing) and an is_active=false bucket exists once any scheme departs.

*Audit findings:* Survivor-only universe (dim6 high, scheme_master.py:171,206 + writers.py:146); survivorship bias inflates beat rates (dim3 medium)

### D2 [L] Point-in-time per-run snapshots: scheme_master_history + rank_history tables — depends on: D1

*Files:* `/Users/suryavamseeayyagari/mfs/src/mfs/db/schema.sql, /Users/suryavamseeayyagari/mfs/src/mfs/db/writers.py, /Users/suryavamseeayyagari/mfs/src/mfs/master/scheme_master.py:247, /Users/suryavamseeayyagari/mfs/src/mfs/rank/shortlist.py:154-183 + rank_deep, /Users/suryavamseeayyagari/mfs/src/mfs/rank/filters.py, /Users/suryavamseeayyagari/mfs/src/mfs/coverage.py:81`

**Change:** Decision: run-keyed Postgres history tables, NOT parquet — backtests and run-diffs need SQL joins against nav_daily/computed_metrics, and computed_metrics already proves the as_of-partitioned pattern; existing CSV/parquet outputs under data/output/shortlist/<as_of>/ stay unchanged. Add to /Users/suryavamseeayyagari/mfs/src/mfs/db/schema.sql: (1) scheme_master_history — snapshot_date DATE NOT NULL + all 15 scheme_master columns (schema.sql:32-48), PK (snapshot_date, scheme_code). This captures universe, category assignments AND the benchmark map per run (benchmark_ticker is a column). (2) rank_history — as_of_date DATE, stage SMALLINT (1|2|3), scheme_code TEXT, canonical_category TEXT, composite_score DOUBLE PRECISION, rank_in_category INT, the z_* columns as flat DOUBLE PRECISION columns mirroring the stage CSVs, included BOOLEAN NOT NULL, exclusion_reason TEXT (e.g. 'hard_filter:r_squared', 'stage2_missing:ptr_latest', 'stage3_overlap:<code>:<pct>'), run_id TEXT (populated by workstream F's run-manifest; key remains (as_of_date, stage, scheme_code)). Writers: snapshot_scheme_master(df, snapshot_date) called at the end of scheme_master.build() (scheme_master.py:247, after D1's sync), and persist_rank_history(...) called from shortlist.rank_deep after each stage's disk write (_write_stage1_outputs at shortlist.py:154-183; stage2/stage3 writers) using the same frames written to CSV plus the dropped frames. To record filter survivors, make rank/filters.py apply_hard_filters optionally return a (scheme_code, first_failing_filter) frame for excluded funds — coordinate with workstream C's D2 hard-drop task which touches the same file. Both writers use INSERT ... ON CONFLICT DO UPDATE so same-day re-runs are idempotent. Add both tables to OUT_OF_CONTRACT in /Users/suryavamseeayyagari/mfs/src/mfs/coverage.py:81 (derived artifacts; the registry-completeness test in tests/test_coverage.py enforces this).

**Tests:** New tests/test_run_history.py: (1) filters reason-frame: synthetic metrics frame -> excluded funds get correct first_failing_filter; (2) monkeypatch writers and assert rank_deep persists stage 1/2/3 frames with included/exclusion_reason populated and shapes matching the CSVs; (3) tests/test_coverage.py registry-completeness passes with the two new tables.

**Acceptance:** uv run pytest tests/test_run_history.py tests/test_coverage.py tests/test_ranking.py -q passes. After the next live run: psql -d mfs -c "SELECT count(*) FROM scheme_master_history WHERE snapshot_date=(SELECT max(snapshot_date) FROM scheme_master_history)" returns ~14,200; psql -d mfs -c "SELECT stage, included, count(*) FROM rank_history WHERE as_of_date=(SELECT max(as_of_date) FROM rank_history) GROUP BY 1,2" shows rows for stages 1-3 including excluded rows with reasons.

*Audit findings:* No point-in-time backtest possible — schema keeps no history of universe/category/benchmark/constituents (dim6 medium/gap, writers.py:133-151); computed_metrics universe swings with no record (dim6)

### D3 [L] Retro-IC backtest tool: tools/backtest_ic.py (quarterly stage-1 recomputation 2016+, Spearman IC vs forward category-relative return, weight sensitivity) — depends on: A1-2, A1-4, D2

*Files:* `/Users/suryavamseeayyagari/mfs/tools/backtest_ic.py (new), /Users/suryavamseeayyagari/mfs/tests/test_backtest_ic.py (new)`

**Change:** New tool /Users/suryavamseeayyagari/mfs/tools/backtest_ic.py (read-only DB; never writes Postgres). For each quarter-end as_of from 2016-03-31 to the latest quarter with a full forward horizon: universe = current scheme_master DIRECT+GROWTH rankable-category funds with benchmark (SURVIVOR-BIASED — print prominently in every output header that measured IC is an upper bound on persistence; dead funds are unrecoverable, do not attempt resurrection). Per fund: aligned = alignment.align_scheme(code, ticker) (alignment.py:50) filtered to date <= as_of (tool-side filter; no core compute changes), require >=3y history at as_of; recompute the NAV-only stage-1 metrics exactly as orchestrator does (returns.rolling_distribution 3y/5y, alpha.rolling_alpha_beta_r2 — MUST be the post-D1-decision SIGNED alpha, median over ALL windows, so the backtested composite matches what ships; sortino, capture, info_ratio); z-score within category via rank/zscore.py; composite via composite_weights_stage1 from pipeline.yaml:51-62. No stage 2/3 (Phase-2 metrics have no pre-2026 history — by design not backtestable). Forward outcome: fund simple NAV return over [as_of, as_of+1y] and [+3y] minus the category median of the same. Report per (quarter, category, horizon): Spearman IC (rank-transform + Pearson; numpy/pandas only, no new deps) of composite vs forward category-relative return, skipping categories with n<8 measurable funds; pooled: mean IC, t = mean/std*sqrt(n_quarters) using non-overlapping windows for the 3y horizon (note autocorrelation caveat for the overlapping variant); top-5-vs-category-median forward spread. Sensitivity: each stage-1 weight +/-50% (renormalized) plus the p25-collapsed variant from D10 — report mean-IC delta and mean top-5 overlap vs baseline. Pre-registered verdict thresholds printed in the report and docstring: JUSTIFIED if pooled 1y mean IC >= 0.05 with |t| >= 2 AND top-5 spread > 0; REFUTED if mean IC <= 0 or |t| < 1 (then move to equal-weight/returns-only pending redesign); else INCONCLUSIVE (keep weights, label outputs 'unvalidated'). Outputs: data/output/backtest/<date>/ic_by_quarter.csv, ic_summary.csv, sensitivity.csv + stdout verdict. Performance: cache per-(scheme, as_of) metric rows as parquet under data/output/backtest/cache/ so re-runs are incremental; flags --since, --categories, --horizons, --step. Data supports this: all 26 benchmark tickers and NAV history start 2013-01-01 (verified live), so 3y-window metrics exist from 2016.

**Tests:** tests/test_backtest_ic.py over pure helpers with synthetic frames, no DB: spearman_ic (golden value vs hand-computed ranks, ties handled), forward category-relative return, weight perturbation renormalization, verdict classification at the threshold boundaries, as_of truncation of an aligned frame.

**Acceptance:** uv run pytest tests/test_backtest_ic.py -q passes. uv run python tools/backtest_ic.py --since 2022-01-01 --horizons 1y completes read-only in minutes and writes ic_summary.csv with one row per category plus ALL; the full 2016+ run emits a single verdict line JUSTIFIED/REFUTED/INCONCLUSIVE per the pre-registered thresholds and a survivorship-caveat header.

*Audit findings:* No backtest or out-of-sample validation anywhere (dim3 high/gap); weights asserted without rationale, filters relaxed in-sample (2c medium, dim6 medium); value proposition rests on unvalidated persistence (dim3 structural)

### D4 [M] Benchmark-fit diagnostic: per-category median R²/beta report + candidate re-fit mode + low-confidence-alpha flag in outputs

*Files:* `/Users/suryavamseeayyagari/mfs/tools/benchmark_fit.py (new), /Users/suryavamseeayyagari/mfs/src/mfs/rank/shortlist.py (mf_report column join), /Users/suryavamseeayyagari/mfs/tests/test_benchmark_fit.py (new)`

**Change:** New tool /Users/suryavamseeayyagari/mfs/tools/benchmark_fit.py with two modes. (1) report: from computed_metrics at the latest as_of, per-category n, median r_squared_3y, median beta_3y, sorted ascending; classify median R² < 0.80 as LOW_CONFIDENCE_ALPHA (verified live at 2026-06-08: Energy 0.580/beta 0.696 n=4, Value 0.727/0.585 n=19, Infrastructure 0.758, MNC 0.777; Large Cap 0.947 for contrast). (2) refit --category X --candidates 'T1,T2,...': for every fund in the category, align vs each candidate ticker present in benchmark_daily and run the same rolling 3y regression (alpha.rolling_alpha_beta_r2) to report per-candidate median R²/beta/alpha — the evidence base for D5 mapping decisions. Additionally wire the flag into user-facing output: in rank/shortlist.py, join a boolean benchmark_fit_low_confidence column (category median r_squared_3y < 0.80 at this as_of) onto stage2/stage3 mf_report rows, so alpha in misfit categories is visibly low-confidence instead of silently absorbed fit residual.

**Tests:** tests/test_benchmark_fit.py: threshold classification pure-function test (0.79 -> flagged, 0.80 -> not); refit on a synthetic fund series constructed as 0.9*indexA returns + noise picks indexA over indexB by median R²; mf_report column join test in tests/test_ranking.py style with a synthetic metrics frame.

**Acceptance:** uv run python tools/benchmark_fit.py report (read-only) prints a table whose Value/Energy rows match psql medians (0.727/0.580 at as_of 2026-06-08); uv run pytest tests/test_benchmark_fit.py -q passes; stage3 mf_report gains benchmark_fit_low_confidence with true for Value/Energy/Infrastructure/MNC at current data.

*Audit findings:* Category-level benchmark assignment fits poorly; alpha there is unexplained return not skill (dim6 high/judgment, benchmarks.csv:10,21); r2<0.40 drops hide benchmark-mapping errors (2c low)

### D5 [M] Fix Value and Energy benchmark mappings in configs/benchmarks.csv using D4 refit evidence — depends on: D4

*Files:* `/Users/suryavamseeayyagari/mfs/configs/benchmarks.csv:10,21, /Users/suryavamseeayyagari/mfs/src/mfs/ingest/benchmarks.py:46-80, /Users/suryavamseeayyagari/mfs/tools/derive_constituents.py (_ETF_TRACKERS), /Users/suryavamseeayyagari/mfs/src/mfs/coverage.py:151-160`

**Change:** Run D4 refit and change /Users/suryavamseeayyagari/mfs/configs/benchmarks.csv:10 (Value) and :21 (Energy). Value candidates: NIFTY 500 TRI (already in benchmark_daily since 2013-01-01 and has derived constituents; SEBI SIDs for value funds overwhelmingly benchmark NIFTY 500/BSE 500 TRI, and the audit showed 0/19 funds beat the factor index with beta 0.58 — a mis-specification artifact) vs incumbent NIFTY 500 Value 50 TRI. Expected outcome: Value -> NIFTY 500 TRI. Energy candidates: incumbent NIFTY Energy TRI vs NIFTY Commodities TRI (must first be added to NSE_TRI_MAP in /Users/suryavamseeayyagari/mfs/src/mfs/ingest/benchmarks.py:46-80 and ingested 2013+ via the existing Backpage.aspx endpoint — engineer-run network step; confirm the entry exists in IndexMapping.json via _load_index_mapping, benchmarks.py:89) vs NIFTY Infrastructure TRI vs NIFTY 500 TRI. Selection rule: highest median R², adopt only if >= 0.75; if no candidate reaches 0.75, keep NIFTY Energy TRI and rely on D4's benchmark_fit_low_confidence flag. Side-effects to handle in the same PR: (a) constituents — if Energy's new index has no derivable tracker, add a tracker mapping in tools/derive_constituents.py _ETF_TRACKERS or add the ticker to the constituents contract's accepted_missing (coverage.py:151-160) with a comment, otherwise Gate B reports a phantom gap (entity set derives from scheme_master.benchmark_ticker, coverage.py:326-331); (b) commit the refit evidence as docs/audit/benchmark_refit_2026-06.md; (c) add a dated comment row in benchmarks.csv noting the remap (alpha/IR history for these categories has a discontinuity at the remap run).

**Tests:** Extend the benchmarks.csv/benchmark_map loading test (tests/test_scheme_master.py or config test) for the new tickers; if NIFTY Commodities TRI is added, extend the ingest ticker-map test. D4 refit output is the decision evidence, committed to docs/.

**Acceptance:** uv run python tools/benchmark_fit.py refit --category Value --candidates 'NIFTY 500 TRI,NIFTY 500 Value 50 TRI' (read-only) shows the chosen mapping's median R² > 0.727 (expect >= 0.85 for NIFTY 500 TRI); benchmarks.csv rows 10/21 updated; after the next live pipeline run, psql per-category median r_squared_3y for Value >= 0.85 and Energy improved or explicitly flagged low-confidence.

*Audit findings:* Value: 0/19 funds beat mapped NIFTY 500 Value 50 TRI yet all alphas positive (dim3 high); Energy R² 0.580 (dim6 high/judgment)

### D6 [M] Hybrid synthetic benchmarks: replace T-bill debt sleeve with a real debt index if scrapeable, else surface the alpha inflation in outputs

*Files:* `/Users/suryavamseeayyagari/mfs/src/mfs/ingest/synthetic_hybrid.py:42-46, /Users/suryavamseeayyagari/mfs/src/mfs/ingest/benchmarks.py, /Users/suryavamseeayyagari/mfs/src/mfs/rank/shortlist.py (disclosure column), docs/audit/hybrid_debt_sleeve_spike.md (new)`

**Change:** Time-boxed spike (~2h, engineer-run network): determine whether NIFTY Composite Debt Index (or fallback NIFTY 10 yr Benchmark G-Sec / NIFTY Short Duration Debt) daily values 2013+ are auto-fetchable from niftyindices.com — the equity endpoint is Backpage.aspx/getTotalReturnIndexString (benchmarks.py:43); check IndexMapping.json (benchmarks.py:89) for fixed-income entries and any analogous fixed-income endpoint. Must be a scrapeable clean series — manual CSVs are forbidden by invariant. PATH A (fetchable): add the debt ticker to the ingest map (new fixed-income fetcher if the endpoint differs), ingest 2013+; modify /Users/suryavamseeayyagari/mfs/src/mfs/ingest/synthetic_hybrid.py:42-46 (SYNTHETIC_HYBRID_SPECS) and synthesize_one to compose w_debt * debt_index_daily_log_return instead of rf_daily for the 65:35 and 50:50 hybrids (Equity Savings keeps its arbitrage sleeve T-bill-based per benchmarks.csv:15 recipe but uses the short-duration index for the 30% debt sleeve if available); keep is_synthetic=True; update the docstring tracking-error estimate. PATH B (not fetchable): (a) extend the synthetic_hybrid.py docstring with the one-sided bias statement (~35-140bp/yr benchmark-too-easy at 35-70% debt weight; Balanced Advantage 100%/Aggressive Hybrid 96.7%/Equity Savings 82.6% beat rates are inflated); (b) carry the disclosure into outputs: add benchmark_is_synthetic boolean to stage2/stage3 mf_report rows for hybrid categories and to D7's passive_alternative.csv with the note 'synthetic benchmark — hybrid alpha overstated'. Either path commits a decision memo at docs/audit/hybrid_debt_sleeve_spike.md with endpoint evidence.

**Tests:** New tests/test_synthetic_hybrid.py: composition math — given a constant-return synthetic debt series and a NIFTY 50 series, the synthesized close compounds w_eq*eq_logret + w_debt*debt_logret exactly (abs 1e-9); weights-sum guard; PATH B: mf_report rows for hybrid categories carry benchmark_is_synthetic=true.

**Acceptance:** Spike memo committed with a clear PATH A/B decision. PATH A: psql -d mfs -c "SELECT count(*) FROM benchmark_daily WHERE ticker='<debt ticker>'" >= 3000 and re-synthesized hybrid closes differ from the old series. PATH B: stage3 mf_report contains benchmark_is_synthetic=true for all Aggressive Hybrid/Balanced Advantage/Equity Savings rows. uv run pytest tests/test_synthetic_hybrid.py -q passes.

*Audit findings:* Synthetic hybrid T-bill debt sleeve inflates hybrid alpha/beat-rates for 84-97 funds (dim3 medium, dim6 medium; synthetic_hybrid.py:43)

### D7 [M] Passive-alternative row per category: benchmark TRI rolling 3y/5y CAGR vs median fund, with explicit 'index wins' marker

*Files:* `/Users/suryavamseeayyagari/mfs/src/mfs/rank/passive.py (new), /Users/suryavamseeayyagari/mfs/src/mfs/rank/shortlist.py (rank_deep wiring + mf_report column), /Users/suryavamseeayyagari/mfs/tests/test_passive_alternative.py (new)`

**Change:** New module /Users/suryavamseeayyagari/mfs/src/mfs/rank/passive.py with build_table(as_of) -> DataFrame: one row per rankable category with: benchmark_ticker; benchmark median rolling 3y/5y annualized return computed with the SAME method as funds — feed the benchmark close series (q.benchmark_series) into returns.rolling_distribution (returns.py:18) as the 'nav' column so windows/annualization are like-for-like; category median ret_3y_median/ret_5y_median and n_funds from computed_metrics at as_of; median_fund_excess_3y/5y; index_wins boolean (median fund excess < 0 on 3y, with 5y shown alongside); benchmark_is_synthetic (from D6). Wire into shortlist.rank_deep: write data/output/shortlist/<as_of>/passive_alternative.csv after stage3, and append pick_minus_benchmark_3y (pick ret_3y_median minus benchmark median 3y) to stage3 mf_report rows so every pick shows its margin vs the investable index. Note in the CSV header comment that this comparison is survivor-biased and the verdict uses corrected mappings only after D5 lands.

**Tests:** tests/test_passive_alternative.py with synthetic series: benchmark beating the median fund -> index_wins=true and correct excess values (golden, abs 1e-9); fund beating benchmark -> false; missing 5y history -> nulls without crashing; mf_report pick_minus_benchmark_3y column math.

**Acceptance:** uv run pytest tests/test_passive_alternative.py -q passes. Read-only sanity on live DB: python -c invocation of passive.build_table(date(2026,6,8)) shows Value with negative median_fund_excess_3y and index_wins=true (audit measured -14.2pp on the current mapping), and Small Cap with index_wins=false; next live run writes passive_alternative.csv with 26 category rows.

*Audit findings:* Pipeline never compares picks to the investable passive alternative; 8+ categories where the median fund trails the index still emit confident top-5 picks (dim3 medium/judgment); can never recommend the index (dim3 structural)

### D8 [L] TER: replace the manual-CSV stub with real AMFI TER ingestion (display column + tiebreaker only); delete the stub either way

*Files:* `/Users/suryavamseeayyagari/mfs/src/mfs/ingest/amfi_ter.py, /Users/suryavamseeayyagari/mfs/src/mfs/cli.py:122 + pipeline stage, /Users/suryavamseeayyagari/mfs/src/mfs/db/schema.sql, /Users/suryavamseeayyagari/mfs/src/mfs/coverage.py:101-167, /Users/suryavamseeayyagari/mfs/src/mfs/rank/stage2.py, /Users/suryavamseeayyagari/mfs/src/mfs/rank/shortlist.py`

**Change:** RECOMMENDATION: implement the real scrape — the stub (/Users/suryavamseeayyagari/mfs/src/mfs/ingest/amfi_ter.py:23-58, wired at cli.py:122) reads manual CSVs and violates the no-manual-entry invariant, and TER is the most evidence-backed forward predictor plus the missing cost side of D7's active-vs-passive framing. Scope strictly display+tiebreaker — NO score weight (weight changes are gated on D3's backtest). Steps: (1) short endpoint spike on AMFI's monthly 'TER of MF Schemes' disclosure (month/year+AMC form; historical months available) to find the machine-readable POST/export; (2) rewrite amfi_ter.py: fetch per (month, AMC), parse scheme-name -> Direct-plan TER tuples, resolve scheme_code via the managers _scheme_match path WITH its collision guards (the TER table prints names, not AMFI codes) — clean tuples or skip the scheme, never null-fill; (3) new table scheme_ter_monthly (scheme_code TEXT, as_of_month DATE, ter_direct_pct DOUBLE PRECISION NOT NULL, source TEXT NOT NULL DEFAULT 'amfi', computed_at TIMESTAMP NOT NULL, PK (scheme_code, as_of_month)) in schema.sql; (4) Gate B ADVISORY contract in coverage.py CONTRACTS (monthly cadence, rankable entity set) + registry test update; (5) pipeline stage after amfi-aaum (cli.py:1136-1139 area), required=False initially; (6) join latest ter_direct_pct into stage2/stage3 mf_report as display column ter_pct, and break stage-2 composite ties (delta < 0.005) by lower TER in rank/stage2.py ordering. FALLBACK (explicitly allowed): if the spike finds only a JS-walled/inaccessible endpoint, DELETE amfi_ter.py, the cli command, and paths.ter_daily_dataset — never keep the manual path.

**Tests:** tests/test_amfi_ter.py: parser fixture test against a saved AMFI TER response excerpt (managers-test convention); scheme-match collision test (subset-name poaching rejected); tie-break ordering test in tests/test_stage2.py; coverage registry test for the new contract.

**Acceptance:** uv run pytest tests/test_amfi_ter.py tests/test_stage2.py tests/test_coverage.py -q passes. After a live ingest: psql -d mfs -c "SELECT count(DISTINCT scheme_code), max(as_of_month) FROM scheme_ter_monthly" covers >= 80% of the 665-fund universe for the latest disclosed month, and stage3 mf_report shows ter_pct for >= 80% of picks. Fallback path acceptance: grep -r amfi_ter src/ returns nothing and `mfs ingest ter` is gone.

*Audit findings:* amfi_ter.py manual-CSV stub violates no-manual-entry invariant; TER entirely absent though it is the best-evidenced forward predictor and the cost side of active-vs-passive (dim3 medium/gap, 2c medium, 1a medium)

### D9 [M] Active-share activation path: pipeline-integrated constituents derivation, freshness contract, and activation banner

*Files:* `/Users/suryavamseeayyagari/mfs/src/mfs/ingest/constituents/derive.py (new, from tools/derive_constituents.py), /Users/suryavamseeayyagari/mfs/src/mfs/cli.py:1150-1158, /Users/suryavamseeayyagari/mfs/configs/pipeline.yaml:37, /Users/suryavamseeayyagari/mfs/src/mfs/rank/shortlist.py or stage2.py (banner), /Users/suryavamseeayyagari/mfs/.claude/commands/run-pipeline.md`

**Change:** Facts (verified live): index_constituents_monthly = 21 tickers x exactly one month (2026-04-01); active_share needs >= 3 MATCHED fund+benchmark monthly snapshots (MIN_SNAPSHOTS_FOR_MEDIAN=3, active_share.py:50) in a window ending the month before as_of (active_share.py:176-178). Calendar: May-2026 tracker holdings are published (AMFI 10th-of-next-month mandate) and derivable NOW; June-2026 lands ~2026-07-10 — so the 0.10 stage-2 weight (pipeline.yaml:77) goes live on the first pipeline run on/after ~2026-07-10, IF May and June derivations actually happen. Three changes so it cannot silently die again: (a) pipeline-integrate derivation — today tools/derive_constituents.py is operator-run and the pipeline only ingests pre-derived CSVs (cli.py:1150-1155, required=False); move the derivation core into src/mfs/ingest/constituents/derive.py (tool becomes a thin wrapper) and add a pipeline stage 'derive constituents (latest month)' between 'ingest holdings' (cli.py:1158) and Gate B, deriving the latest completed month from already-ingested tracker holdings; (b) freshness tooth — set max_constituents_lag_days: 75 in configs/pipeline.yaml:37 (two monthly cycles + slack; freshness.py:147-149 already wires it) so a stalled derivation halts the run per the fail-fast invariant, instead of Gate B's never-blocking advisory (coverage.py:276-277); (c) activation banner — in shortlist.rank_deep/stage2, count distinct matched (holdings, constituents) months and print 'active_share: n_matched_months=X/3, estimated activation <date>' until any fund has non-null active_share_median_1y, then print the non-null share of the stage-2 pool. Update .claude/commands/run-pipeline.md stage list accordingly. [REVIEW-EXTENDED] Add tracker-fund mappings for NIFTY 50 TRI and NIFTY Bank TRI to _ETF_TRACKERS (currently missing entirely): without them active_share activates unevenly ~2026-07 and Large Cap/BFSI stay null forever. Deadline ~2026-07.

**Tests:** Move/extend derivation tests for the relocated module (tracker auto-select + index-size sanity logic must keep its tests); freshness test asserting FreshnessError when constituents max month exceeds 75d; banner unit test (capsys) with synthetic month sets for 1/3, 2/3, 3/3 matched months.

**Acceptance:** uv run pytest tests/test_active_share.py tests/test_ingest_constituents.py tests/test_coverage.py -q passes; grep max_constituents_lag_days configs/pipeline.yaml shows 75. Next pipeline run derives+ingests 2026-05 (psql: 2 distinct as_of_month in index_constituents_monthly) and prints the 2/3 banner; first run on/after ~2026-07-10: psql -d mfs -c "SELECT count(*) FROM computed_metrics WHERE as_of_date=(SELECT max(as_of_date) FROM computed_metrics) AND active_share_median_1y IS NOT NULL" > 0.

*Audit findings:* Constituents single-month, active_share 0.10 weight dead universe-wide (1a low, 2c medium); monthly freshness gates all disabled, Gate B never halts (1a medium); constituent-derivation circularity note (dim6 low — record tracker source, kept in derive module docstring)

### D10 [S] ret_median/ret_p25 redundancy: document the two-block structure in pipeline.yaml, gate the collapse decision on the backtest — depends on: D3

*Files:* `/Users/suryavamseeayyagari/mfs/configs/pipeline.yaml:51-78, /Users/suryavamseeayyagari/mfs/tools/backtest_ic.py (variant entry)`

**Change:** Recommendation: explicitly ACCEPT for now and collapse only on D3 evidence — the pairs correlate 0.92-0.97 within category (audit 2c) so the 0.15/0.10 splits are false precision, but collapsing reorders every category without validation. Edit /Users/suryavamseeayyagari/mfs/configs/pipeline.yaml:51-78: (a) above composite_weights_stage1, add: 'ret_median/ret_p25 within-category corr ~0.92-0.97 (audit 2026-06-10): the four return weights act as two ~0.25 blocks and total trailing-performance load is ~0.75. Deliberately accepted; collapse decision deferred to tools/backtest_ic.py sensitivity variant p25_collapsed (ret_3y_median 0.25 / ret_5y_median 0.25 / p25 weights 0).'; (b) fix the two false comments in the same file while there: lines 52-54 claim 'all eight z-scores are available for every fund that survives the hard-filter floor' — false (54/435 stage-1 survivors have null alpha; reword to match workstream-C's D2 hard-drop behavior once it lands), and line 66 claims stage-2 positive weights 'sum to 0.95' — actual sum is 0.85 (verified: .10+.07+.10+.07+.20+.08+.08+.05+.10). (c) Register the p25_collapsed variant in D3's sensitivity suite (one entry in the tool's variant table).

**Tests:** No new tests for comments; the p25_collapsed variant is covered by D3's perturbation tests (assert the variant renormalizes to 1.0 and zeroes both p25 weights).

**Acceptance:** grep -A3 'p25_collapsed' configs/pipeline.yaml shows the rationale comment; grep '0.95' configs/pipeline.yaml returns nothing; D3's sensitivity.csv contains a variant=p25_collapsed row.

*Audit findings:* ret_median/ret_p25 near-duplicates corr 0.92-0.97, nominal splits false precision, trailing-performance load ~0.75 (2c medium/judgment); stage-2 weight-sum comment drift 0.95 vs 0.85 (2b low)

**Workstream risks:**

- History cannot be backfilled: every pipeline run that executes before D1+D2 land destroys another universe snapshot (TRUNCATE) — these two tasks are time-sensitive in the same sense as the audit's 'urgent' items; ship them before the next scheduled run.
- D2 (filters.py exclusion reasons) and workstream C's D2 hard-drop task touch rank/filters.py and stage2.py concurrently — sequence the PRs or merge conflicts will produce inconsistent exclusion_reason vocabularies; agree the reason-string format with C first.
- D3's IC is computed on a survivor-only universe (current scheme_master), so a JUSTIFIED verdict is only an upper bound on real persistence; a REFUTED verdict is, however, fully actionable. The report must carry this asymmetry, and D1 means the bias shrinks for future re-runs.
- D3 depends on workstream B implementing the signed-alpha decision (D1) first; backtesting the old censored alpha would validate a metric being deleted.
- D5 benchmark remaps create a one-time discontinuity in alpha/IR/capture history for Value/Energy; rank_history (D2) and the dated benchmarks.csv comment are the audit trail, but run-over-run diff tooling (workstream F) should expect it.
- D6 PATH A and D8 both hinge on endpoint spikes; if niftyindices fixed-income or AMFI TER endpoints are JS-walled, the fallback paths (document inflation / delete stub) must actually be taken — do not let either degrade into a manual-CSV workaround (invariant violation).
- D9's max_constituents_lag_days: 75 gives the freshness gate teeth: if constituents derivation breaks, the whole pipeline halts. That is the intended fail-fast behavior, but the operator runbook (run-pipeline.md) must document the remediation path so a halt is a 10-minute fix, not a disabled gate.
- scheme_master row count now grows monotonically (D1); queries that assume scheme_master ~= active universe must keep their is_active=true filters — audited the known call sites (orchestrator, coverage, shortlist join) but any new consumer must be reviewed.

**Ordering notes:** Land D1 then D2 immediately (before the next pipeline run) — snapshots and is_active=false history start accumulating only from the first run after they merge, and nothing can be backfilled. D9(a)+(b) is also near-term calendar-bound: May-2026 constituents are derivable now and June lands ~2026-07-10; the 0.10 active-share weight activates on the first run after that only if derivation runs monthly. D4 -> D5 is a strict pair (diagnostic evidence before mapping change); run D5 before D7 ships its first verdicts so the Value 'index wins' row reflects the corrected mapping rather than the factor-index artifact. D3 waits on workstream B's signed-alpha implementation (user decision D1) so the backtested composite matches production; D10's yaml edits can land any time but its variant hook needs D3's tool skeleton. D6, D7, D8 are mutually independent and parallelizable. Cross-workstream coordination: D2's run_id column is populated by workstream F's run-manifest task (history tables are keyed by as_of_date and remain valid without it); D2's filters.py reason-frame and D8's stage2 tiebreaker touch files workstream C is editing for the D2/D3 user decisions — agree exclusion_reason strings and merge order with C. Total: 2 S/M weeks of focused work plus three L tasks (D2, D3, D8) that can proceed in parallel once D1 merges.


## Workstream E — Retail-investor output & guidance

Puts a human-readable layer on top of the staged-ranking CSVs (currently a 44-column z-score CSV at data/output/shortlist/<as_of>/stage3/mf_report.csv with no ISIN, no interpretation, no flags). Shape: (1) enrich the existing frames with cheap display columns (ISIN, cohort size, liquidity flag, max-drawdown — display-only, never weighted); (2) a markdown report builder (REPORT.md per run, plus `mfs report` CLI) that renders picks per category with plain-language metric explanations and the D2/D3/D4 exception sections, degrading gracefully when those artifacts haven't landed yet; (3) static guidance content (switching costs/taxes, monitoring cadence, scope boundary, glossary); (4) `mfs shortlist diff` for run-to-run monitoring. Markdown chosen over HTML (greppable, diffable, no deps). Per-scheme exit loads are OUT: AMFI/SEBI publish them only in per-scheme SID/Scheme-Summary PDFs, no machine-readable feed — guidance stays static per the no-manual-entry invariant.

### E1 [S] Join ISIN and cohort-size columns into ranked outputs

*Files:* `src/mfs/rank/shortlist.py:23-51,57-65; src/mfs/rank/stage2.py:126-180`

**Change:** (a) In src/mfs/rank/shortlist.py:57-65 `_join_scheme_master`, add "isin_growth" to the `sm.select([...])` list (line 64) and add a matching `pl.lit(None, dtype=pl.Utf8).alias("isin_growth")` to the empty-master branch (lines 60-63). DB check confirms 665/665 rankable DIRECT+GROWTH schemes have isin_growth, so no null handling beyond pass-through. (b) Add "isin_growth" to STAGE1_OUTPUT_COLS (shortlist.py:23-51, after "scheme_name") so stage1 CSVs carry it; stage2/stage3 inherit all columns automatically. (c) In src/mfs/rank/stage2.py `apply_stage2` per-category block (lines 126-145), after `cat_survivors` is built, attach cohort context: `cat_survivors = cat_survivors.with_columns(pl.lit(pool.height).alias("n_considered"), pl.lit(int(cat_survivors.height)).alias("n_survived"))` (values already computed for coverage_rows at lines 154-180) so the final mf_report distinguishes 'best of 20' from 'only survivor of 1' (FMCG cohort-of-one case).

**Tests:** tests/test_stage2.py: new test asserting survivors carry n_considered/n_survived with correct values for a 2-category synthetic frame (extend pattern of test_coverage_row_per_category at line 227). New test (tests/test_ranking.py or tests/test_stage2.py) monkeypatching mfs.db.queries.scheme_master to return a frame with isin_growth and asserting `_join_scheme_master` output carries it, plus the empty-master branch returns the column as nulls.

**Acceptance:** `uv run pytest tests/test_stage2.py tests/test_ranking.py -q` green. Then `uv run mfs rank-deep --as-of 2026-06-08 --skip-phase2-compute` (DB read-only; regenerates the run dir) and `head -1 data/output/shortlist/2026-06-08/stage3/mf_report.csv | grep -o 'isin_growth\|n_considered\|n_survived'` prints all three.

*Audit findings:* dim4 'execution metadata absent (no ISIN)' (low); dim4 'degenerate single-fund cohort scores without context' (low)

### E2 [M] Max-drawdown + time-to-recover compute module (display-only)

*Files:* `src/mfs/compute/drawdown.py (new); src/mfs/rank/shortlist.py:343-351`

**Change:** New module src/mfs/compute/drawdown.py. Pure function `drawdown_stats(nav: pl.DataFrame, window_years: int, as_of: date) -> dict | None` over a (date, nav) frame (shape of q.nav_series, src/mfs/db/queries.py:18-28): restrict to the trailing window_years calendar window ending at min(last nav date, as_of); filter `nav > 0` first (defensive vs the audit's zero-NAV poisoning until workstream A lands); compute running cummax, max peak-to-trough decline as positive pct, peak/trough dates, and recovery_days = calendar days from trough until nav first regains the prior peak (None if not recovered by end of series); return None if <1y of data in window. Wrapper `attach_drawdown(df: pl.DataFrame, as_of: date) -> pl.DataFrame` that, for each scheme_code in df, loads q.nav_series and joins columns max_dd_3y_pct, max_dd_3y_recovery_days, max_dd_5y_pct, max_dd_5y_recovery_days. Integrate in src/mfs/rank/shortlist.py `rank_deep` between Stage 2 and Stage 3 (after line 343, before stage3_mod.run at line 346): `stage2_result["survivors"] = attach_drawdown(stage2_result["survivors"], as_of)` so stage2 survivors (~130 schemes, seconds of queries) flow into stage3 CSVs and mf_report. STRICTLY display-only: no edits to src/mfs/rank/score.py or configs/pipeline.yaml weights.

**Tests:** tests/test_drawdown.py (new): (1) synthetic series 100→80→120 gives max_dd_pct=20.0 and finite recovery_days; (2) monotonic rise gives 0.0 dd, recovery_days None-or-0 (pick and assert one convention); (3) drawdown at series end → recovery_days None; (4) crash before the 3y window start is excluded from the 3y stat but included in 5y; (5) zero-NAV rows ignored; (6) <1y data → None.

**Acceptance:** `uv run pytest tests/test_drawdown.py -q` green. After `uv run mfs rank-deep --as-of 2026-06-08 --skip-phase2-compute`, `head -1 data/output/shortlist/2026-06-08/stage3/mf_report.csv | grep -c max_dd` prints >=1, and a spot-check Small Cap row shows a plausible 3y drawdown (>15%).

*Audit findings:* dim4 'no behavioral risk framing: no drawdown metric anywhere' (medium)

### E3 [S] Liquidity flag column for days-to-exit (display-only)

*Files:* `src/mfs/rank/stage2.py:126-145 (+ new constants near line 51)`

**Change:** In src/mfs/rank/stage2.py, add module constants LIQUIDITY_FLAG_HIGH_DAYS = 30.0 and LIQUIDITY_FLAG_SEVERE_DAYS = 90.0, and in `apply_stage2` after `cat_survivors` is built (lines 135-143) derive `liquidity_flag`: null aum_impact_cost_days → null; <=30 → "OK"; >30 → "HIGH"; >90 → "SEVERE" (pl.when chain). This is a labeling column only — do NOT touch the saturating penalty in src/mfs/rank/score.py:124-138 (penalty re-shaping belongs to the scoring workstream; the audit's accepted minimum is an explicit flag). The audit's motivating case: final pick 118778 (Nippon Small Cap) has aum_impact_cost_days=924 and today carries no marker distinguishing it from a 10-day fund.

**Tests:** tests/test_stage2.py: new test feeding aum_impact_cost_days values [None, 5, 31, 924] through apply_stage2 and asserting flags [null, "OK", "HIGH", "SEVERE"]; assert the column lands in the written stage2 CSV in test_run_writes_artifacts (line 272).

**Acceptance:** `uv run pytest tests/test_stage2.py -q` green; after regenerating the 2026-06-08 run, `grep 118778 data/output/shortlist/2026-06-08/stage3/mf_report.csv | grep -c SEVERE` prints 1.

*Audit findings:* dim4 'liquidity penalty saturates at 10 days with no flag' (medium)

### E4 [L] Markdown investor report builder (REPORT.md + `mfs report` CLI) — depends on: E1, E2, E3

*Files:* `src/mfs/rank/report.py (new); src/mfs/cli.py (new command); src/mfs/rank/shortlist.py:353-379; .claude/commands/run-pipeline.md:121`

**Change:** New module src/mfs/rank/report.py with `build_report(run_dir: Path, as_of: date, young_funds: pl.DataFrame | None = None) -> Path` writing `<run_dir>/REPORT.md`. Data assembly is file-based for testability: primary input stage3/mf_report.csv, fallback stage2/mf_report.csv (with a note) when stage3 is absent; stage2/coverage.csv for cohort sizes. Renderer (keep assembly and rendering as separate functions so HTML could be added later): per-category section listing each pick with — scheme_name, scheme_code, ISIN (isin_growth from E1), composite_score, stage2_rank, and a plain-language metric block: ret_3y_median/ret_5y_median as '%/yr typical rolling return', alpha_3y_annualized as signed benchmark-relative excess (render alpha_confidence column when present, per decision D1 — do not describe alpha as 'significant-windows-only'), sortino_3y, info_ratio_3y, capture_efficiency one-liners, aum_crore, aum_impact_cost_days + liquidity_flag (E3) with a sentence ('at recent traded volumes, exiting the least-liquid large positions would take ~N trading days'), max_dd_3y_pct/max_dd_5y_pct + recovery days (E2) as the per-category risk note, and 'rank 1 of N considered' from n_considered (E1). Tolerate missing columns (render '—'); never KeyError on schema drift. CLI: `@app.command("report")` in src/mfs/cli.py (typer app defined at cli.py:12-20): `mfs report --as-of YYYY-MM-DD` (default = latest dir under data/output/shortlist via paths.shortlist_dir, src/mfs/paths.py:144-145), which queries young funds (E5 helper) and calls build_report. Also call build_report at the end of `rank_deep` (src/mfs/rank/shortlist.py:353-379, before the return) so every pipeline run emits it, and add one line to .claude/commands/run-pipeline.md '## Final report' section (line 121) telling the operator to surface the REPORT.md path.

**Tests:** tests/test_report.py (new): build a synthetic run dir in tmp_path (stage3/mf_report.csv with 2 categories incl. one missing-column case, stage2/coverage.csv) and assert: REPORT.md created; contains ISIN strings; contains the liquidity sentence and SEVERE flag for a 924-day row; contains drawdown lines; contains 'rank 1 of N'; falls back to stage2/mf_report.csv with a visible note when stage3 file is removed; missing columns render '—' without raising.

**Acceptance:** `uv run pytest tests/test_report.py -q` green; `uv run mfs report --as-of 2026-06-08` exits 0 and `test -s data/output/shortlist/2026-06-08/REPORT.md && grep -c '^## ' data/output/shortlist/2026-06-08/REPORT.md` shows one section per surviving category plus the fixed sections.

*Audit findings:* dim4 'raw 42-column z-score CSVs with no interpretation layer' (medium); dim4 'no behavioral risk framing' (medium)

### E5 [M] Report exception sections: excluded (D2), partial-disclosure (D3), overlap-breach (D4), index-wins (WS-D), young funds — depends on: E4, A2-2, A2-4, A2-7, D7

*Files:* `src/mfs/rank/report.py; src/mfs/db/queries.py (new helper near scheme_master, line 81)`

**Change:** Extend src/mfs/rank/report.py with five sections, each degrading gracefully (omit with a one-line 'artifact not present in this run' note) so this can merge before the scoring workstreams land. (1) 'Excluded — insufficient data': read the D2 exclusion artifact (coordinate the name with the Stage-1 workstream; proposed contract: stage1/excluded.csv with scheme_code, scheme_name, canonical_category, missing_metrics) and render per category. (2) 'Partial disclosure data': render funds flagged by D3's soft-neutral path (proposed contract: stage2 column missing_disclosure_metrics, non-empty string); fallback when the column is absent: derive from null ptr_latest/style_drift_3y/active_share_median_1y on report rows — identical semantics pre/post D3. (3) 'Overlap breaches': per D4 (restored 2026-05-24 decision: keep both, flag with overlap %) render each pick's breach partners (proposed contract: stage3 columns overlap_flag / overlap_with detail emitted by the WS implementing D4); fallback: recompute from stage3/overlap_pairs.csv (written at src/mfs/rank/stage3.py:196-204) restricted to pairs where both sides are in the final report. (4) 'Index wins this category': read workstream D's per-category verdict artifact (proposed contract: <run_dir>/category_verdicts.csv with canonical_category, verdict, median_excess_3y) and render a prominent marker above the picks for verdict=passive. (5) 'Young funds — not yet eligible': new read-only helper in src/mfs/db/queries.py `young_rankable_schemes(as_of: date, min_years: int = 3) -> pl.DataFrame` — scheme_master rows with is_active, plan_type=DIRECT, option_type=GROWTH, canonical_category in get_thresholds()['rankable_categories'], inception_date > as_of - 3y, excluding base_fund_id ending '_bonus'; render count + names + inception + ISIN per category (199 schemes as of 2026-06-08 — render grouped one-liners, not metric blocks). The `mfs report` CLI passes this frame in; build_report itself stays DB-free.

**Tests:** tests/test_report.py: synthetic run dirs exercising each section — (a) excluded.csv present → section lists the fund; absent → note rendered; (b) missing_disclosure_metrics column present and fallback-from-nulls path both produce the 'partial disclosure' tag on the right fund; (c) overlap flag column path and overlap_pairs.csv fallback both render breach with %; (d) category_verdicts.csv with verdict=passive renders the index-wins marker for that category only; (e) young_funds frame renders grouped by category. Plus a queries-level test is impractical without a test DB — cover young_rankable_schemes filtering logic by extracting the polars filter into a pure function and unit-testing it.

**Acceptance:** `uv run pytest tests/test_report.py -q` green. On the live run dir: `uv run mfs report --as-of 2026-06-08` produces REPORT.md containing a 'Young funds' section with ~199 entries (`grep -A2 'not yet eligible' ...`), partial-disclosure tags derived from nulls, and overlap breaches from overlap_pairs.csv; after D2/D3/D4 land, regenerating shows the artifact-driven versions.

*Audit findings:* Implements display side of locked decisions D2/D3/D4; dim4 'Stage 3 silently wipes categories' (visibility half — the keep-and-flag fix itself is the ranking workstream's); dim3 'pipeline never surfaces passive-preferred verdict' (display side)

### E6 [S] Static guidance content: switching costs, monitoring cadence, scope boundary, glossary — depends on: E4

*Files:* `src/mfs/rank/report.py`

**Change:** Module-level constant markdown blocks in src/mfs/rank/report.py, rendered as fixed sections of every REPORT.md (documentation, not code — per scope). (1) 'Taxes, lock-ins and exit loads': ELSS 3-year lock-in per unit; equity LTCG 12.5% on gains above ₹1.25L/yr; STCG 20% (<=12 months); typical exit load ~1% if redeemed within 1 year. State explicitly: per-scheme exit loads are published by AMFI only inside per-scheme SID/Scheme-Summary-Document PDFs (https://www.amfiindia.com/otherdata/scheme-details; portal.amfiindia.com/spages/SSD_*.pdf) — no machine-readable AMFI/SEBI feed exists, so the pipeline does not carry them (no-manual-entry invariant); check the AMC page before redeeming. (2) 'When to switch' hysteresis rule of thumb: only switch when the incumbent has fallen below its category's final cutoff (rank > final_size) for 2 consecutive runs AND the expected improvement plausibly clears tax + exit load; reference `mfs shortlist diff` (E7). (3) 'How often to re-run': quarterly is sufficient (holdings/PTR are monthly-cadence inputs; observed churn 1 fund of 34 over 17 days). (4) 'What this pipeline does NOT cover': equity + 3 hybrid categories only; 1,837 of 2,502 active Direct-Growth schemes (all debt, liquid, gold, international FoF, index funds, arbitrage, multi-asset) are out of scope by design — bring your own debt/gold/international allocation; index funds are never recommended except via the WS-D verdict marker. Reference the README rewrite (workstream F) for pipeline mechanics rather than duplicating it. (5) 'Glossary': plain-language entry for every metric column rendered in the report (ret_3y_median, ret_3y_p25, alpha_3y_annualized + alpha_confidence per D1 wording, sortino_3y, info_ratio_3y, capture_up/down/efficiency, r_squared_3y, beta_3y, style_drift_3y, active_share_median_1y, ptr_latest, aum_impact_cost_days, aum_crore, max_dd_*, composite_score, z_* convention, n_considered).

**Tests:** tests/test_report.py: assert REPORT.md contains the four key tax/lock-in facts ('3-year lock-in', '12.5%', '1.25', '20%'), the 2-consecutive-runs hysteresis sentence, the '1,837 of 2,502' scope sentence, and a glossary entry for every metric column the renderer displays (loop over the renderer's column list).

**Acceptance:** `uv run pytest tests/test_report.py -q` green; `grep -c '1,837' data/output/shortlist/2026-06-08/REPORT.md` prints >=1 after regeneration; glossary section covers all displayed metric columns (test enforces).

*Audit findings:* dim4 'no switching-cost model' (medium — documentation remedy per scope); dim4 'asset-class boundary unstated' (medium); dim4 'no cadence guidance' (part of monitoring gap)

### E7 [M] Run-to-run diff: `mfs shortlist diff <run1> <run2>`

*Files:* `src/mfs/rank/diff.py (new); src/mfs/cli.py:13-20 (+ new sub-app/command)`

**Change:** New module src/mfs/rank/diff.py with pure `diff_runs(run_a: Path, run_b: Path) -> dict` plus a markdown/text renderer, and a new typer sub-app in src/mfs/cli.py (`shortlist_app = typer.Typer(); app.add_typer(shortlist_app, name="shortlist")` alongside the existing sub-apps at cli.py:13-20) with command `diff(run1: str, run2: str)` taking run-date dir names under data/output/shortlist. Logic: load stage3/mf_report.csv from both runs (fallback stage2/mf_report.csv with a note), key by scheme_code; per category emit ENTERED, EXITED, and RANK-MOVED (stage2_rank delta) lists. Reason derivation for EXITED funds, checked in order against run2's artifacts: (1) in stage1 excluded artifact (D2) → 'excluded: insufficient data (<missing_metrics>)'; (2) in stage2/dropped.csv (header has missing_metrics column — verified on 2026-06-08 run) → 'dropped at stage 2: missing <metrics>'; (3) in stage3/dropped.csv → 'overlap-dropped vs <kept>' (pre-D4 runs only); (4) present in run2 stage2 category file at rank > final cutoff → 'score fell to rank N'; (5) absent from run2 stage1 entirely → 'no longer ranked (hard-filtered or no data)'. ENTERED reasons symmetric where derivable, else 'new to final list'. Print to stdout and write <run2_dir>/DIFF_vs_<run1>.md. No DB access — pure file diff, so it works on historical run dirs as-is.

**Tests:** tests/test_shortlist_diff.py (new): two synthetic run dirs in tmp_path crafted so each of the five reason paths fires exactly once; assert entered/exited/rank-moved membership, reason strings, the stage2-fallback note, and that DIFF_vs_*.md is written.

**Acceptance:** `uv run pytest tests/test_shortlist_diff.py -q` green; `uv run mfs shortlist diff 2026-05-22 2026-06-08` exits 0 and reports HSBC Equity Savings as EXITED (the audit-verified churn between those runs) with a derived reason, and writes data/output/shortlist/2026-06-08/DIFF_vs_2026-05-22.md.

*Audit findings:* dim4 'no monitoring story: no run-to-run diff, no deterioration signal' (medium)

**Workstream risks:**

- Cross-workstream artifact contract: E5 needs agreed names for the D2 exclusion file (proposed stage1/excluded.csv), D3 flag column (proposed missing_disclosure_metrics), D4 overlap-flag columns, and WS-D's verdict artifact (proposed category_verdicts.csv). The report degrades gracefully without them, but if names diverge the sections silently stay on fallback paths — agree the contract before E5 review.
- Acceptance commands regenerate data/output/shortlist/2026-06-08/ via `mfs rank-deep --skip-phase2-compute`; outputs are derived and reproducible from the DB, but the regenerated files will differ from the audit-evidence snapshots (e.g. once D-decision tasks land, stage3 no longer drops). Archive the current run dir first if audit traceability matters.
- Drawdown over nav_daily inherits the zero-NAV poisoning and the 2026-05-23..06-04 interior gap until workstream A's fixes land; drawdown.py filters nav>0 defensively, but numbers computed before the NAV backfill may understate/move slightly after it.
- No machine-readable AMFI/SEBI exit-load source exists (verified: SID/Scheme-Summary PDFs only), so per-scheme exit loads are permanently out under the no-manual-entry invariant — guidance stays generic and must say so to avoid implying precision.
- Glossary/plain-language text is coupled to D1: if E6 ships before the signed-alpha change merges, the alpha description would mislabel the censored metric. Write the D1 wording from day one and gate the report's alpha sentence on the alpha_confidence column's presence.
- Tax/lock-in figures (STCG 20%, LTCG 12.5% >1.25L, ELSS 3y) are baked as static text per the locked decision; a future Finance Act change makes the report wrong until the constant is edited — keep them in one constant block with a 'rates as of FY2025-26' caption.
- Report renderer reads CSV column names produced by other workstreams; renames there (e.g. composite column changes) degrade sections to '—'. The missing-column tolerance prevents crashes but a schema-drift test tying report.py's column list to stage2/stage3 writers would catch silent blanking.

**Ordering notes:** E1, E2, E3, and E7 are mutually independent and can start immediately in parallel (E7 touches no shared files; E1/E3 both edit stage2.py — land E1 first to avoid conflict churn). E4 (the report core) requires E1+E2+E3 columns to exist. E6 is pure content inside E4's renderer — do it immediately after E4's skeleton merges. E5 should land last: merge it with all fallback paths working (it is fully testable today), then re-verify after the other workstreams land the D2 exclusion artifact, D3 disclosure flag, D4 keep-and-flag stage3, and WS-D's index-verdict artifact — the depends_on entries prefixed 'WS-' are soft cross-workstream placeholders (their planners own the real task ids); the only hard coordination item is the artifact-name contract listed in risks. Markdown is the chosen format throughout (locked here to stop re-litigation). Note for the implementing engineer: never run `mfs pipeline` or network ingest for acceptance — `mfs rank-deep --skip-phase2-compute`, `mfs report`, and `mfs shortlist diff` are DB-read-only/file-only and are the sanctioned end-to-end checks. README rewrite is explicitly workstream F; E6 only inserts a pointer to it.


## Workstream F — Architecture, tests, ops hygiene

Makes the pipeline maintainable and survivable without touching ranking semantics (workstreams A-D own those). Five thrusts: (1) baseline-commit the phase-5 working tree the audit ran against, then back up the 11GB DB + 9.5GB data/raw currently on one laptop; (2) decompose the 1,203-line cli.py into a unit-testable pipeline module and move 15 raw-SQL diagnostics into db/queries; (3) cut adapter duplication via a shared parse_ptr_pages helper (3 adapters migrated as proof) and a shared ingest-runner core — extraction chosen over full orchestrator unification because the run_for_amc bodies differ too much to merge cheaply; (4) make the suite CI-runnable: local_data marker + minimal committed PDF-extract fixtures (full PDFs rejected on size and AMC-copyright grounds), GitHub Actions, mypy baseline (125 errors measured) on compute+rank; (5) provenance and trust: per-run manifest.json fixing the pipeline_version contradiction, external spot-check checklist, category-relabel halt guard, and resolving the Kotak prodtest-host risk.

### F-0 [S] Commit current working tree as the remediation baseline

*Files:* `/Users/suryavamseeayyagari/mfs/.gitignore, entire working tree`

**Change:** The audit ran against an uncommitted tree (39 modified + 77 untracked files: ~45 holdings adapters, src/mfs/coverage.py, src/mfs/ingest/_parse_cache.py, src/mfs/ingest/holdings/_generic.py, docs/phase5/, docs/audit/). Before any remediation task: (a) add `.claude/worktrees/` to .gitignore (639MB of untracked worktree junk); (b) run `git status data/` and extend .gitignore so nothing under data/ is committable (currently only data/raw|curated|metrics|output|staging are ignored and `?? data/` still shows); (c) decide tools/discover_whiteoak_*.py + tools/whiteoak_resources*.html (tracked scratch) — keep for F-5 to delete, don't block here; (d) `git add` everything else and commit on main as the phase-5 baseline (one commit is fine; all later plans diff against it).

**Tests:** None new — run the full existing suite as the commit gate.

**Acceptance:** `uv run pytest -q` green, then `git status --porcelain` is empty and `git log -1 --stat` shows the baseline commit including src/mfs/coverage.py and the new holdings adapters.

*Audit findings:* critic: run-provenance / uncommitted phase-5 work

### F-1 [M] Backups: pg_dump + restic for data/raw, scheduled, with a restore test — depends on: F-0

*Files:* `scripts/backup.sh (new), scripts/restore_check.sh (new), docs/ops/backups.md (new), ~/Library/LaunchAgents/com.mfs.backup.plist (new)`

**Change:** Create scripts/backup.sh: (1) `pg_dump -Fc -d mfs -f <backup_dir>/mfs_$(date +%F).dump` (DB is 11GB live; -Fc compresses); prune to last 7 daily + 4 weekly. (2) `restic backup data/raw` (9.5GB, mostly unrecoverable upstream — AMCs delist old Excels) to an external-drive repo, with an optional second cloud target (B2/S3); RESTIC_REPOSITORY/RESTIC_PASSWORD and any cloud keys read from .env (already gitignored; do NOT hardcode). Create scripts/restore_check.sh: `createdb mfs_restore_check && pg_restore -d mfs_restore_check <latest.dump>`, assert row counts of nav_daily, benchmark_daily, holdings_monthly within 1% of live, then dropdb; also `restic check` + restore one sample factsheet PDF to /tmp and compare sha256. Schedule daily backup via a launchd plist (~/Library/LaunchAgents/com.mfs.backup.plist) and document a monthly manual restore_check in docs/ops/backups.md.

**Tests:** None (shell + manual); restore_check.sh IS the test and must be run once at task completion.

**Acceptance:** `bash scripts/backup.sh && bash scripts/restore_check.sh` exits 0 printing matching row counts; `launchctl list | grep com.mfs.backup` shows the job loaded.

*Audit findings:* critic: disaster recovery — 9.6GB irreplaceable data, zero backups

### F-2 [M] Extract mfs/pipeline.py from cli.py; make stage orchestration unit-testable — depends on: F-0

*Files:* `src/mfs/pipeline.py (new), src/mfs/cli.py:986-1199`

**Change:** Create src/mfs/pipeline.py with `run(as_of: date, *, full: bool = False, skip_phase2: bool = False, allow_fallback: bool = False, echo: Callable[[str], None] = print) -> PipelineResult`. Move verbatim from cli.py: the pipeline body (cli.py:1019-1199) — _stage, _coverage_gate, the advisory-lock enter/exit (keep the finally-block summary-render + lock-release semantics identical), the hybrid-synthesis RuntimeError→IngestError translation (cli.py:1114-1123) — and _latest_amfi_quarter_label (cli.py:986-1016). pipeline.run raises PipelineError (never typer.Exit); return a small PipelineResult dataclass {as_of, out_dir, stage1_files, stage2_counts, stage3_counts, gate_reports}. cli.py `pipeline` command becomes ~25 lines: parse options, call pipeline.run, catch PipelineError → typer.Exit(2), print the result summary. Coordinate: workstreams changing stage behavior (NAV ingest_since, gate promotion) should land on top of this module — sequence F-2 early.

**Tests:** tests/test_pipeline_orchestration.py (new): monkeypatch every stage callable (amfi_nav.ingest_today, benchmarks.ingest_all_known, synthetic_hybrid.synthesize_all, fbil_tbill.ingest, scheme_master.build, coverage.run_gate, managers.run_all, holdings.run_all, orchestrator.run_phase1, shortlist.rank_deep, pipeline_lock) and assert: (1) stage call order; (2) required-stage IngestError halts before any later stage runs; (3) required=False stage failure continues; (4) Gate A blocking report halts before amfi_aum/managers; (5) lock __exit__ called on the failure path; (6) gate summary rendered on failure; (7) skip_phase2 skips exactly the 6 phase-2 stages; (8) _latest_amfi_quarter_label pinned for 2026-06-11→'Q4-2026', 2026-02-01→'Q3-2025', boundary d=quarter_end+45.

**Acceptance:** `uv run pytest tests/test_pipeline_orchestration.py -q` passes; `uv run mfs pipeline --help` output unchanged; `grep -c 'def ' src/mfs/cli.py` shows the pipeline command body shrunk to a thin shell (no _stage/_coverage_gate in cli.py).

*Audit findings:* dim5: cli.py monolith; dim7: CLI/pipeline has zero tests

### F-3 [M] Move diagnostics SQL out of cli.py into db/queries.py — depends on: F-2

*Files:* `src/mfs/cli.py:607-790,842-983, src/mfs/db/queries.py`

**Change:** Extract the 15 raw conn.execute calls into pure functions in src/mfs/db/queries.py returning structured rows (no typer/printing): from db_coverage (cli.py:607-790) → q.trading_calendar_bounds(since), q.benchmark_gap_report(since), q.risk_free_gap_report(since), q.nav_coverage_report(since, rankable_categories, min_inception_year); from missing_data (cli.py:842-947) → q.benchmark_inventory(since), q.nav_per_scheme_inventory(since); from validate_cmd (cli.py:949-983) → q.tri_cagr_sanity(start) and q.nav_table_counts(). CLI commands keep only formatting/echo. While moving, fix the validate docstring/threshold mismatch: docstring says 'CAGR > 12%' (cli.py:951) but code checks 0.10 (cli.py:975) — define one module constant TRI_SANITY_MIN_CAGR = 0.10 and make the docstring/output read from it.

**Tests:** tests/test_db_queries_diagnostics.py (new): for tri_cagr_sanity, monkeypatch the connection cursor to return fixed boundary closes and pin the CAGR math (e.g. 100→200 over 5y → 0.1487 ±1e-4) and the OK/SUSPECT classification both sides of TRI_SANITY_MIN_CAGR.

**Acceptance:** `grep -cE 'c\.execute|conn\.execute' src/mfs/cli.py` returns 0; `uv run mfs validate` against the live DB prints the same numbers as before the move (read-only command).

*Audit findings:* dim5: diagnostics SQL bypasses db/queries; dim5: stale 12%-vs-0.10 docstring

### F-4 [M] Shared ingest/_common.py + parse_ptr_pages() helper; migrate 3 manager adapters as proof — depends on: F-0

*Files:* `src/mfs/ingest/_common.py (new), src/mfs/ingest/managers/canara_robeco.py:343-362, src/mfs/ingest/managers/iti.py:344-371, src/mfs/ingest/managers/groww.py`

**Change:** Create src/mfs/ingest/_common.py with: (a) MONTH_NAMES/month-parse table (currently redefined in ~63 ingest files); (b) publish_ym(data_ym, offset_months) replacing the 22 private _publish_ym copies (17 managers + 5 holdings); (c) silence_pdfminer() called once (replaces the logging.getLogger('pdfminer').setLevel line in 41 manager files); (d) parse_ptr_pages(pdf_path, *, scheme_name_fn, ptr_extract_fn, amc_slug, skip_page_re=None) — a generator implementing the canonical loop: pdfplumber.open → per-page extract_text() or '' → scheme_name_fn(text) (skip None) → optional sentinel-regex skip → ptr_extract_fn(text) (skip None) → NaN/<=0 guard → yield ParsedPtrRecord(scheme_name_printed, ptr, source_amc). Note in its docstring that ptr_extract_fn must return a FRACTION (central place to kill the recurring percent-vs-fraction bug). Migrate exactly 3 text-regex-only adapters to it: canara_robeco.py (parse_ptr at :343-362), iti.py (:344-371, uses the sentinel hook), and groww.py (confirm text-regex-only at migration time; else substitute lic.py). Also write (docstring/spec only, no bulk migration) the declarative SimplePtrSpec the remaining ~16 text-regex-only adapters will eventually become: {url_template, scheme_name_re, ptr_re, ptr_unit, publish_offset}.

**Tests:** Existing pinned tests must pass unchanged: tests/test_managers_canara_robeco.py, tests/test_managers_iti.py, tests/test_managers_groww.py (they parse local 2026-04 PDFs — run on the dev machine). Add tests/test_ingest_common.py: parse_ptr_pages NaN/zero/negative-PTR rejection and sentinel-skip behavior with a stubbed pdfplumber object; publish_ym January/December rollovers.

**Acceptance:** `uv run pytest tests/test_managers_canara_robeco.py tests/test_managers_iti.py tests/test_managers_groww.py tests/test_ingest_common.py -q` passes on the dev machine; `grep -rln pdfminer src/mfs/ingest/managers | wc -l` drops from 41 to 38.

*Audit findings:* dim5: parse_ptr skeleton copy-pasted ~40x, pdfminer silencing 41x, _publish_ym duplicated

### F-5 [M] Unify ingest orchestrators by extracting the shared core (not full merge) — depends on: F-4

*Files:* `src/mfs/ingest/_common.py, src/mfs/ingest/managers/_run.py, src/mfs/ingest/holdings/_run.py, src/mfs/ingest/_scheme_match.py (moved), src/mfs/ingest/managers/_scheme_match.py (shim)`

**Change:** Assessment: full unification is NOT cheaper — run_for_amc bodies differ structurally (managers: one PDF, parse-skip cache, two record types; holdings: per-scheme Excel discovery/download loop + collision resolution). Extract the duplicated pieces instead: (a) move _default_data_month (managers/_run.py:62-69 and holdings/_run.py:34-38, identical) to ingest/_common.py; (b) move managers/_scheme_match.py to src/mfs/ingest/_scheme_match.py (holdings/_run.py:24 currently imports across the sibling package) leaving a one-line re-export shim at the old path; (c) add run_all_isolated(family, slugs, run_one) to ingest/_common.py implementing the per-AMC try/except + partial-warning + all-failed systemic IngestError currently duplicated at managers/_run.py:281-332 and holdings/_run.py:205-242 — preserve log event names (managers.amc_failed / holdings.amc_failed) and the exact all-failed message shape; (d) move _dedupe_by_keys (managers/_run.py:37) to _common and replace the inline holdings dedupe (holdings/_run.py:147-163) with it. Defer porting _parse_cache to holdings: that is incremental-ingestion work — if the pipeline-hardening workstream doesn't claim it, add it as a follow-on here.

**Tests:** tests/test_adapter_isolation.py must pass unchanged (it covers the isolation semantics being extracted). Add to it: parametrize the one-fails-continues and all-fail-raises cases over both families through run_all_isolated; add a January-rollover test for the single _default_data_month.

**Acceptance:** `uv run pytest tests/test_adapter_isolation.py -q` green; `grep -rn 'def _default_data_month' src/mfs | wc -l` returns 1; `grep -rn 'def _dedupe_by_keys' src/mfs | wc -l` returns 1.

*Audit findings:* dim5: two parallel ingest orchestrators duplicate fault-isolation/dedupe/txn logic

### F-6 [S] Dead code removal: io/duck.py, duckdb dep, parquet-era tooling, stale comments — depends on: F-0

*Files:* `src/mfs/io/duck.py, src/mfs/io/parquet.py, src/mfs/ingest/amfi_ter.py, src/mfs/db/migrate.py, src/mfs/db/verify.py, src/mfs/cli.py:121-128,578-604, pyproject.toml (dependencies), src/mfs/db/queries.py:277, src/mfs/db/writers.py:319, src/mfs/rank/filters.py:31-35, tools/`

**Change:** [REVIEW-RESCOPED] EXCLUDE amfi_ter.py and the `mfs ingest ter` command from deletion until D8's endpoint spike concludes (D8 rewrites that module; deletion is only D8's fallback). io/parquet.py deletion waits for D8 to drop its parquet import. ADD to the sweep: the dead unbounded forward_fill at alignment.py:70-74 and schemas.py's never-wired table-row models (except NavDaily, which A1-1 wires). (a) Delete src/mfs/io/duck.py (verified zero importers in src/tests/tools) and remove `duckdb~=1.1` from pyproject.toml dependencies; run `uv lock`. (b) io/parquet.py importers are amfi_ter.py, db/verify.py, db/migrate.py only. Delete the manual-TER path: src/mfs/ingest/amfi_ter.py + the `mfs ingest ter` command (cli.py:121-128) — it ingests manual CSVs (violates the no-manual-entry invariant) into a parquet store nothing reads (critic-confirmed). Delete the one-time parquet→Postgres migration tooling: src/mfs/db/migrate.py, src/mfs/db/verify.py + CLI commands `db migrate`/`db verify` (cli.py:578-604) — get explicit user confirmation that the legacy parquet store is fully retired first. Then delete src/mfs/io/parquet.py. (c) KEEP src/mfs/ingest/synthetic_hybrid.py — verified live at cli.py:78 and cli.py:1119 (pipeline hybrid-TRI stage). (d) Stress-test remnants are comments only: fix stale section headers at db/queries.py:277, db/writers.py:319, and the dead comment block at rank/filters.py:31-35; keep the schema.sql:101 historical note. (e) Delete tools/discover_whiteoak_*.py (8 scratch scripts) and tools/whiteoak_resources*.html after user confirmation.

**Tests:** No new tests; full suite is the gate. Remove any test imports of deleted modules (grep first — none found for duck/migrate/verify).

**Acceptance:** `grep -rn duckdb src tests tools pyproject.toml` → no hits; `uv sync && uv run pytest -q` green; `uv run mfs --help` no longer lists `ingest ter`, `db migrate`, `db verify`.

*Audit findings:* dim5: dead code + dead duckdb dep; critic: amfi_ter violates no-manual-entry and writes to a dead store

### F-7 [M] mypy configuration: compute + rank first, wired for CI — depends on: F-0

*Files:* `pyproject.toml ([tool.mypy] new), src/mfs/compute/orchestrator.py, src/mfs/compute/returns.py, src/mfs/compute/aum_impact.py, src/mfs/rank/shortlist.py`

**Change:** Add [tool.mypy] to pyproject.toml: python_version='3.11', files=['src/mfs/compute','src/mfs/rank'], plus strictness ramp: check_untyped_defs, disallow_untyped_defs, no_implicit_optional, warn_return_any, warn_unused_ignores; per-module `ignore_missing_imports` overrides for statsmodels/pdfplumber (polars and pydantic ship types). Measured baseline: 125 errors in 4 files (compute/orchestrator.py 86, compute/returns.py 43, compute/aum_impact.py 8, rank/shortlist.py 1). Fix them with type-only changes (annotations, casts, narrowing) — zero behavior changes; the existing metric-pin tests are the guard. Full `strict = true` is a later ratchet once these pass; do not expand files= beyond compute+rank in this task.

**Tests:** No new tests; `uv run pytest tests/test_metrics_math.py tests/test_aum_impact.py -q` must stay green to prove type fixes changed no math.

**Acceptance:** `uv run mypy` exits 0; `uv run pytest -q` green.

*Audit findings:* dim5: mypy installed but unconfigured

### F-8 [M] Fixture strategy: local_data marker + minimal committed PDF-extract fixtures (recommendation: hybrid) — depends on: F-0

*Files:* `pyproject.toml (markers, dev deps), tests/conftest.py, tools/make_fixture_pdf.py (new), tests/fixtures/factsheets/ (new), tests/test_managers_fixture_extracts.py (new)`

**Change:** Recommendation: do NOT commit full factsheet PDFs — they are 2-4MB each (~28 manager test PDFs ≈ 80-100MB) and AMC factsheets are copyrighted marketing documents, so committing them creates redistribution exposure if the repo ever becomes public (FLAG: acceptable in a private repo, but excerpts are safer and smaller). Implement hybrid: (a) register a `local_data` pytest marker in pyproject [tool.pytest.ini_options]; in tests/conftest.py add a collection hook that auto-applies it to any test module whose fixture path resolves under data/raw (the 27 test_managers_*.py files + any holdings tests reading data/raw); each existing pytest.skip guard stays as belt-and-braces. (b) Build tools/make_fixture_pdf.py (one-time; add pypdf to the dev dependency group) that extracts the 1-2 pages containing a pinned scheme from a cached factsheet; commit extracts for 3 representative adapters (hdfc — holds the strongest value pin PTR==0.0914, canara_robeco, iti) under tests/fixtures/factsheets/<amc>/2026-04_extract.pdf (~100-500KB each). (c) Add tests/test_managers_fixture_extracts.py: parse each committed extract via the adapter's parse_ptr and pin the known values (HDFC Flexi Cap PTR==0.0914; one pinned scheme each for canara_robeco/iti taken from the existing local tests) — NOT marked local_data, so CI executes real parse paths. (d) Visibility: a CI-side check (see F-9) pins the deselected-test count so silently growing skips fail the build.

**Tests:** test_managers_fixture_extracts.py as described; verify marker coverage by running with data/ renamed.

**Acceptance:** With data/raw temporarily renamed: `uv run pytest -m 'not local_data' -q` passes with 0 skips from missing PDFs, and `uv run pytest -m 'not local_data' --collect-only -q | tail -1` shows the deselected count matching the pinned number; restore data/raw and the full suite is green.

*Audit findings:* dim7: value-pin tests depend on gitignored data/raw, no CI; dim5: adapter tests pinned to local artifacts

### F-9 [S] GitHub Actions CI — depends on: F-8

*Files:* `.github/workflows/ci.yml (new)`

**Change:** Add .github/workflows/ci.yml: trigger on push + pull_request; ubuntu-latest; steps: checkout → astral-sh/setup-uv@v5 (with cache) → `uv sync --frozen` → `uv run ruff check src tests` → `uv run mypy` (add this step when F-7 lands; gate F-9 only on F-8) → `uv run pytest -m "not local_data" -q`. No Postgres or network services needed (audit dim 7: suite is fully isolated). Add the deselected-count tripwire: a final step runs `uv run pytest -m 'not local_data' --collect-only -q` and greps the deselected number against a pinned value stored in the workflow env, failing if it grew (makes new silent local-only tests visible). [REVIEW-EXTENDED] Add a pip-audit step to CI (3 lines) for dependency CVE checks.

**Tests:** The workflow itself; no repo test changes.

**Acceptance:** Push a branch; `gh run watch` shows the workflow green; deliberately add a dummy local_data test on a scratch branch and confirm the count tripwire fails.

*Audit findings:* dim7: no CI workflow at all; critic: dependency hygiene (CI is the prerequisite)

### F-10 [M] Run manifest: manifest.json per shortlist run; fix pipeline_version contradiction — depends on: F-0, F-2

*Files:* `src/mfs/provenance.py (new), src/mfs/rank/shortlist.py:272-365, src/mfs/db/queries.py, src/mfs/config.py:130, src/mfs/pipeline.py`

**Change:** (a) Add src/mfs/provenance.py with build_manifest(as_of, result) returning a dict: git_sha (`git rev-parse HEAD`), git_dirty (bool from `git status --porcelain`), config_sha256 (hash over configs/pipeline.yaml + configs/category_thresholds.yaml + configs/benchmarks.csv bytes), as_of, run timestamps, pipeline_version (from get_pipeline_config()), mfs package version, db_row_counts (nav_daily, benchmark_daily, holdings_monthly, portfolio_turnover_monthly, scheme_aum_monthly, index_constituents_monthly, computed_metrics WHERE as_of=as_of — add a single q.table_counts(as_of) in db/queries.py), and stage1/stage2/stage3 file+survivor counts from the rank_deep result. Write it atomically (io/atomic.py exists) to data/output/shortlist/<as_of>/manifest.json at the end of rank_deep (shortlist.py:272+) so standalone `mfs rank-deep` runs get one too, and have pipeline.run (F-2) enrich it with gate summaries. (b) Fix the version contradiction: configs/pipeline.yaml:96 says 'v2.3.0-phase2.3a' while config.py:130 defaults pipeline_version to 'v1.0.0' — make the field required (remove the default) so YAML is the single source and a missing key fails loudly; orchestrator.py:104 already reads the loaded config.

**Tests:** tests/test_provenance.py (new): build_manifest with injected git info + fake counts asserts all required keys and the dirty flag; config-loading test asserting PipelineConfig raises ValidationError when pipeline_version is absent from YAML.

**Acceptance:** `uv run pytest tests/test_provenance.py -q` passes; after the next sanctioned rank-deep run, `cat data/output/shortlist/<as_of>/manifest.json | python -m json.tool` shows git_sha + config hash + counts, and `psql -d mfs -c "SELECT DISTINCT pipeline_version FROM computed_metrics WHERE as_of='<as_of>'"` returns the YAML value.

*Audit findings:* critic: run provenance/reproducibility; pipeline_version contradiction (pipeline.yaml:96 vs config.py:130)

### F-11 [S] External cross-validation spot-check tool + one recorded run after the Stage-2 recompute — depends on: F-0

*Files:* `tools/spot_check.py (new), docs/ops/spot_check.md (new)`

**Change:** Add tools/spot_check.py (read-only DB access): given scheme codes, print point-to-point 3y and 5y CAGR from nav_daily (latest NAV vs NAV nearest 3y/5y prior) plus the stored ret_3y_median/ret_5y_median for context; given a ticker+date list, print benchmark_daily TRI closes. Add docs/ops/spot_check.md checklist: (1) 5 shortlisted funds across different AMCs — compare the tool's 3y/5y CAGR to the AMC factsheet or ValueResearch published trailing returns, tolerance ±0.3pp (date-mismatch allowance); (2) 2 TRI levels (NIFTY 50 TRI and NIFTY Midcap 150 TRI) on two dates vs niftyindices.com — exact match expected; (3) record results, deviations, and explanations in docs/ops/spot_check_results/<date>.md. Schedule: run once after workstreams A-D land and the Stage-2 recompute produces a fresh shortlist — any unexplained deviation blocks publishing that shortlist.

**Tests:** Unit test only for the CAGR helper (synthetic NAV series compounding exactly 10%/yr → 0.10 ±1e-6) in tests/test_spot_check.py.

**Acceptance:** `uv run python tools/spot_check.py --schemes <5 codes>` prints CAGRs against the live DB; after the recompute, docs/ops/spot_check_results/<date>.md exists with all checklist items filled and within tolerance.

*Audit findings:* critic: no computed number ever reconciled externally

### F-12 [S] Category-relabel guard in scheme_master.build() — depends on: F-0

*Files:* `src/mfs/master/scheme_master.py:146-155,168-210`

**Change:** _canonical_category (scheme_master.py:146-155) returns None silently for any unmatched AMFI category string; combined with TRUNCATE+rebuild, an AMFI/SEBI relabel silently evaporates whole categories. In build() (scheme_master.py:168-210): (1) collect distinct raw amfi_category strings where canon is None AND the closed/interval regex did not match, with scheme counts, and log them at warning (structlog event scheme_master.unmatched_categories); (2) before the upsert, compare against the existing DB scheme_master: if any canonical_category that currently has >=5 schemes would drop to 0 in the new build, raise PipelineError naming the category and the unmatched raw strings (fail-fast invariant: halt rather than rank a silently shrunken universe). Keep the threshold (5) as a module constant.

**Tests:** tests/test_scheme_master_guard.py (new): (1) _canonical_category pins — existing label maps, a plausibly relabeled string ('Equity Scheme - Flexicap Fund', no space) returns None; (2) build-guard unit: monkeypatch the AMFI fetch + DB reads so a previously-populated category yields 0 matches → assert PipelineError; same input with the category still matched → no raise.

**Acceptance:** `uv run pytest tests/test_scheme_master_guard.py -q` passes; simulation (temporarily remove one CATEGORY_RULES entry, run build against monkeypatched inputs in the test) raises PipelineError naming the category.

*Audit findings:* critic: regulatory/taxonomy drift — silent universe loss on relabel

### F-13 [S] Kotak prodtest host: switch to a production API host or document the provenance risk — depends on: F-11

*Files:* `src/mfs/ingest/holdings/kotak.py:111-118, docs/ops/spot_check.md`

**Change:** kotak.py:118 hardcodes _API='https://vlbapiprodtest.kotakmf.com/kotakapi/portfolio' — a TEST host with no freshness/correctness SLA feeding production rankings (critic ToS/provenance concern). During the next sanctioned ingest window (network work is out of scope for planning): (1) inspect the www.kotakmf.com SPA bundle's urlProxies config for the production kotakapi host (try the obvious vlbapiprod / api variants) and whether it serves without the Radware challenge; (2) if found: switch _API, then verify parity for one scheme-month (same folderlist response, byte-identical or row-identical parsed Excel vs the prodtest host) before keeping it; (3) if no production host is reachable: document the risk explicitly in the kotak.py module docstring (test endpoint, no SLA, data could silently diverge) and add a recurring item to the F-11 spot-check checklist: compare one Kotak scheme-month's top-10 holdings vs Kotak's public portfolio-disclosure page.

**Tests:** Existing kotak holdings parse behavior unchanged (no test file exists today; the F-8 marker work covers it if one is added). No new automated test — host parity is the manual acceptance check.

**Acceptance:** Either `grep prodtest src/mfs/ingest/holdings/kotak.py` returns nothing and a parity note (scheme, month, result) is recorded in the commit message/docs, or the docstring documents the risk and docs/ops/spot_check.md contains the Kotak parity item.

*Audit findings:* critic: legal/ToS exposure and provenance of reverse-engineered access paths (Kotak test host)

### F-14 [M] README rewrite to current architecture — depends on: F-2, F-5, F-6, F-10

*Files:* `README.md`

**Change:** README.md is parquet-era throughout ('writes a Parquet partition that the next stage reads', manual hybrid CSVs, v1 NAV-only scope). Rewrite after the refactors land: architecture (ingest adapters → coverage gates A/B → Postgres (schema at src/mfs/db/schema.sql) → compute phase1/phase2 → rank-deep stage1/2/3 → data/output/shortlist/<as_of>/ + manifest.json); CLI command map (pipeline, ingest *, build scheme-master, compute *, rank-deep, db coverage/status, validate, missing-data); project invariants verbatim (fail-fast ingestion, no manual entry, AUM is AMFI-AAUM-only, incremental-by-default with --full); dev workflow (uv sync, pytest markers incl. local_data, mypy, CI); ops (backup/restore from F-1, spot-check from F-11); known limitations (survivorship, point-in-time gaps) with a pointer to docs/audit/2026-06-10_pipeline_audit.md.

**Tests:** None.

**Acceptance:** `grep -in parquet README.md` returns no hits describing the live data path (a historical-notes mention is fine); every CLI command named in README exists in `uv run mfs --help` output; user reviews and approves.

*Audit findings:* dim5: doc-code divergence as a failure mode; scope: README rewrite

### F-15 [S] Scraping provenance & ToS inventory memo with per-adapter accept/replace decisions — depends on: F-13

*Files:* `docs/ops/scraping_provenance.md (new)`

**Change:** Inventory every non-public access path: ITI extracted AES key/IV (iti.py:112-113), Edelweiss TLS-fingerprint/Sec-Fetch bot-detection mimicry (edelweiss.py:204-210), Kotak prodtest host (F-13 owns the switch), jio_blackrock Next.js action-id. For each: what breaks if it rotates, ToS exposure judgment, decision = accept-risk / replace / drop-AMC. User signs off.

**Tests:** n/a (documentation)

**Acceptance:** Memo exists with an explicit decision per adapter.

*Audit findings:* critic ToS/provenance exposure beyond Kotak

**Workstream risks:**

- F-2 (pipeline.py extraction) refactors the only end-to-end path while workstreams A-D modify stage internals concurrently — land F-2 immediately after F-0 and have other workstreams rebase onto it, or merge conflicts in cli.py will be constant.
- Committed AMC factsheet page-extracts (F-8) carry copyright/redistribution exposure if the repo ever becomes public; mitigated by keeping the repo private, committing minimal excerpts only, and being ready to swap to local-only markers — flagged per the audit's ToS concern.
- F-6 deletes db/migrate.py and db/verify.py (the parquet↔Postgres reconciliation path); if the legacy parquet store is not fully retired this removes a safety net — requires explicit user confirmation before deletion.
- F-7's 125 mypy fixes touch compute/orchestrator.py and returns.py, files other workstreams (alpha rework, point-in-time fixes) are also editing — coordinate ordering or expect rebases; type fixes must remain behavior-neutral (metric-pin tests are the guard).
- F-5's shared run_all_isolated must preserve exact log event names and error-message shapes; the all-failed IngestError text is asserted nowhere but the partial/failed semantics are load-bearing for the fail-fast invariant — parity bugs here would weaken it silently.
- Backup task (F-1) moves ~12-13GB initially; cloud targets have cost/bandwidth implications — external-drive restic repo is the non-negotiable minimum, cloud second copy is recommended but user's call.
- F-12's halt-on-category-disappearance adds a new failure mode to scheme_master.build; a legitimate AMFI category retirement would block the pipeline until the rules table is updated — that is the intended fail-fast behavior, but the operator runbook (README, F-14) must say how to respond.
- The deselected-count tripwire in CI (F-9) is brittle to legitimate test additions; keep the pinned count in one obvious place in the workflow and update it in the same PR that adds local_data tests.

**Ordering notes:** F-0 (baseline commit) is strictly first — every other workstream's diffs assume it. F-1 (backups) immediately after: it protects irreplaceable data and depends on nothing else. Then F-2 (pipeline.py) early because workstreams A-E will edit stage behavior and should build on the extracted module; F-3 follows F-2 (same cli.py region). F-4 → F-5 are a sequence (helper module, then orchestrator-core extraction into the same _common.py). F-6 (dead code), F-7 (mypy), F-8 (fixtures) are independent and parallelizable after F-0; F-9 (CI) needs F-8 and gains a mypy step when F-7 lands. F-10 (manifest) needs F-2 for the pipeline hook but the rank_deep-side write can start anytime after F-0. F-11's tool can be built now, but its checklist run must wait for the Stage-2 recompute from workstreams A-D (external dependency); F-13 references F-11's checklist. F-14 (README) is deliberately last so it documents the post-refactor architecture. The registry-completeness test is owned by workstream B and is intentionally absent here (F-4/F-5 reference, not duplicate). Cross-workstream note: porting _parse_cache to holdings is deferred from F-5 to whichever workstream owns incremental ingestion; if none claims it, append it to F-5.
