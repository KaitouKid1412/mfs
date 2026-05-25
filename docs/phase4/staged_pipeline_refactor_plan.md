# Phase 4 — Staged Pipeline Refactor Plan

**Status**: implemented 2026-05-25. End-to-end ``mfs rank-deep`` writes
stage1/stage2/stage3 subdirs; full test suite green (289 passed).

Shipped components:

- ``configs/pipeline.yaml`` — split into ``composite_weights_stage1`` /
  ``composite_weights_stage2``; added ``soft_penalties.aum_impact_cost``.
- ``src/mfs/config.py`` — ``PipelineConfig`` carries both weight blocks;
  ``AumImpactCostPenalty`` model added.
- ``src/mfs/compute/orchestrator.py`` — split into
  ``compute_phase1_for_scheme`` / ``compute_phase2_for_scheme`` and
  ``run_phase1`` / ``run_phase2``. Style drift now derived in Phase 2 from
  the cheap rolling-regression diagnostics persisted in Phase 1. Old
  ``run`` kept as a back-compat wrapper.
- ``src/mfs/db/writers.py`` — ``upsert_computed_metrics_phase1`` and
  ``update_computed_metrics_phase2``; legacy ``upsert_computed_metrics``
  retained.
- ``src/mfs/db/queries.py`` — ``computed_metrics_for_schemes`` for the
  Stage 1 → Stage 2 narrowed reload.
- ``src/mfs/rank/score.py`` — ``composite_score_stage1`` (pure Phase 1, no
  soft penalties, no pro-rata redistribution) and ``composite_score_stage2``
  (Phase 1 + Phase 2 + PTR + AUM Impact soft penalties).
- ``src/mfs/rank/stage2.py`` — rewritten as re-rank. Pool top-N per
  category, drop NULL Phase 2 rows, re-z-score the survivors, apply
  ``composite_score_stage2``, take top final_size. Writes per-category
  CSV+parquet, dropped, coverage, mf_report.
- ``src/mfs/rank/stage3.py`` — rewritten as iterative drop. Drops the
  worse-Stage-2-ranked fund of the highest-overlap remaining pair until
  no pair exceeds the threshold. Writes per-category CSV+parquet,
  dropped, overlap_pairs, mf_report.
- ``src/mfs/rank/shortlist.py`` — ``rank_deep`` now orchestrates Stage 1
  → Phase 2 compute (restricted to the candidate pool) → Stage 2 re-rank
  → Stage 3 iterative drop. ``build_scored`` renamed to
  ``build_scored_stage1``.
- ``src/mfs/cli.py`` — added ``mfs compute phase1`` and ``mfs compute
  phase2`` subcommands; ``mfs pipeline`` now runs phase1 then rank-deep
  (which handles phase2 internally). ``mfs rank-deep`` gains
  ``--skip-phase2-compute`` for re-running ranking without redoing Phase 2.
- Tests: ``tests/test_ranking.py`` and ``tests/test_stage2.py`` rewritten
  for the new architecture; ``tests/test_stage3.py`` rewritten for drop
  semantics; ``tests/test_ptr_penalty.py`` migrated to
  ``composite_score_stage2``.

## Goal

The current pipeline is **single-pass compute + output bucketing**. We want a
**true staged pipeline** where each stage has its own compute step and
ranking step, and only the survivors of the prior stage feed into the next:

- **Stage 1**: compute Performance & Consistency metrics for every fund;
  rank purely on those.
- **Stage 2**: take top-20 per category from Stage 1; compute Manager Skill
  + Liquidity & Scale metrics ONLY for those; re-rank using a combined
  Phase-1+Phase-2 composite.
- **Stage 3**: take Stage 2 final picks; compute pairwise overlap;
  iteratively drop the lower-ranked fund of any pair whose overlap > 30%.

Each stage writes to its own subdirectory:
```
data/output/shortlist/<as_of>/
├── stage1/   ← per-category ranked CSVs + mf_report
├── stage2/   ← re-ranked top-5 per category + dropped + coverage
└── stage3/   ← final clean shortlist after overlap drops + drop log
```

---

## What's wrong with the current implementation

1. **`compute metrics`** runs once over all 682 schemes and computes every
   metric (Phase 1 + Phase 2) up front — wasteful, and conflates the two
   conceptual stages.
2. **`composite_score`** in Stage 1's per-category ranking ALREADY includes
   Phase 2 metrics (Active Share +0.10, Style Drift −0.05). Stage 1 today
   is NOT pure Performance & Consistency — it's a hybrid with pro-rata
   redistribution for funds missing Phase 2 data.
3. **Stage 2 of rank-deep** is just a null-completeness filter. It doesn't
   re-rank anything. It picks the funds with all Phase 2 metrics non-null
   and preserves Stage 1's order.
4. **Stage 3** only flags pairs > 30% overlap; doesn't actually drop any
   fund. The "final" list is whatever Stage 2 produced.

---

## Target architecture

### Stage 1 — Pure Performance & Consistency ranking

**Compute step** (`compute_phase1`): for every active Direct+Growth scheme
in scheme_master (682 today), compute only:
- `ret_3y_median, ret_3y_p25, ret_5y_median, ret_5y_p25`
- `alpha_3y_annualized, alpha_3y_tstat, beta_3y, r_squared_3y`
- `sortino_3y`
- `capture_up, capture_down, capture_efficiency`
- `info_ratio_3y`
- `data_quality_flag`

Phase 2 columns (style_drift_3y, active_share_median_1y, ptr_latest,
aum_impact_cost_days) are NOT touched — they stay NULL for now.

Write to `computed_metrics` (existing table — Phase 2 cols stay NULL).

**Ranking step** (`rank_stage1`):
1. Apply hard filters (existing):
   - `beta_3y ∈ [0.30, 1.70]`
   - `r_squared_3y ∈ [0.40, 1.00]`
   - `info_ratio_3y > −1.00`
   - `capture_efficiency > 0.50`
   - `data_quality_flag == 'GOOD'`
2. z-score the 8 ranking metrics within `canonical_category`.
3. composite_score using **Stage 1 weights** (sum = 1.00):

   | Metric | Weight |
   |---|---|
   | ret_3y_median | 0.15 |
   | ret_3y_p25 | 0.10 |
   | ret_5y_median | 0.15 |
   | ret_5y_p25 | 0.10 |
   | alpha_3y | 0.25 |
   | sortino_3y | 0.10 |
   | info_ratio_3y | 0.10 |
   | capture_efficiency | 0.05 |

4. For schemes with <5y NAV history: substitute `ret_5y_median` and
   `ret_5y_p25` with the scheme's own `ret_3y_median` and `ret_3y_p25`.
   (Already implemented in `score.py` via `PROXY_FALLBACK`.)
5. Dedupe legacy "Bonus Option" unit classes (existing logic in shortlist.py).

**Outputs**:
- `stage1/<category>.csv` — every passing scheme in that category, sorted
  by composite_score desc, no top-N cap.
- `stage1/<category>.parquet` — same.
- `stage1/mf_report.csv` — consolidated top-5 per category.
- `stage1/mf_report.parquet` — same.

Schema for `stage1/<category>.csv`: all Stage 1 metrics + composite_score +
data_quality_flag + scheme_code + scheme_name + canonical_category + rank.
No Phase 2 columns (they don't exist yet at this stage).

### Stage 2 — Combined Phase 1 + Phase 2 re-ranking

**Input**: top-20 per category from Stage 1's ranked output.

**Compute step** (`compute_phase2`): for ONLY the schemes in the Stage 1
top-20 pools (≈26 categories × 20 = ≤520 schemes), compute:
- `style_drift_3y` (= σ(β_rolling) + (1 − μ(R²_rolling)) from the same 3y
  weekly-step rolling regression).
- `active_share_median_1y` (trailing 12-month median of monthly Active
  Share against the scheme's `benchmark_ticker`. `MIN_SNAPSHOTS_FOR_MEDIAN`
  stays at 3 — to be revisited when we have historical months on disk).
- `ptr_latest` (pass-through from `portfolio_turnover_monthly`).
- `aum_impact_cost_days` (computed from `holdings_monthly` × scheme AUM ×
  `stock_adv_daily` median ADV with 33% participation cap).

Write Phase 2 columns to the same `computed_metrics` row for those scheme
codes (Phase 2 cols on other rows stay NULL).

**Eligibility step**: a scheme survives Stage 2 only if ALL four Phase 2
metrics are non-null. Funds missing any get dropped with reason recorded.

**Ranking step** (`rank_stage2`):
1. Filter pool to surviving funds.
2. z-score Phase 2 metrics within category (Phase 1 z-scores from Stage 1
   are reused — same data).
3. composite_score_v2 using **Stage 2 weights**:

   | Metric | Stage 2 weight | Notes |
   |---|---|---|
   | ret_3y_median | 0.10 | |
   | ret_3y_p25 | 0.07 | |
   | ret_5y_median | 0.10 | |
   | ret_5y_p25 | 0.07 | |
   | alpha_3y | 0.20 | |
   | sortino_3y | 0.08 | |
   | info_ratio_3y | 0.08 | |
   | capture_efficiency | 0.05 | |
   | active_share | 0.10 | positive contribution: higher Active Share preferred |
   | style_drift | −0.05 | signed penalty |
   | ptr_latest | soft penalty | ramp above 1.50 PTR, max −0.05 |
   | aum_impact_cost_days | soft penalty | ramp above 5 days, max −0.05 |

   Positive weights sum to 0.95; style_drift & soft penalties subtract on top.
4. Re-rank within category by composite_score_v2.
5. Take top-5 per category as Stage 2 final picks.

**Outputs**:
- `stage2/<category>.csv` — top-5 survivors per category, with all Phase 1
  + Phase 2 metric columns + composite_score_v2 + stage2_rank +
  `partial_coverage_flag` (true if <5 survived).
- `stage2/dropped.csv` — every fund from a Stage 1 top-20 pool that
  didn't survive Stage 2. Columns: scheme_code, scheme_name, category,
  stage1_rank, composite_score (Stage 1), source_amc, aum_crore,
  missing_metrics (semicolon-delimited list of NULL Phase 2 cols).
- `stage2/coverage.csv` — per-category rollup: n_considered, n_survived,
  n_dropped, pct_survived, aum_considered_crore, aum_survived_crore,
  top_dropped_amc.
- `stage2/mf_report.csv` — top-3 per category for at-a-glance use.

### Stage 3 — Iterative portfolio-overlap drop

**Input**: Stage 2 final picks (≤5 per category × 26 categories ≈ 130 funds).

**Compute step**: for every pair of Stage 2 picks (cross-category), compute
pairwise overlap using `pairwise_overlap()` from `mfs.rank.overlap`. Pairs
where either fund has no holdings get `overlap_pct = NULL` and are logged
separately (can't be compared).

**Drop step** (`apply_stage3_drops`):
1. Build the candidate set = Stage 2 picks, sorted by `stage2_rank`
   within each category (rank-1 picks first).
2. Iterate: find the pair with the highest overlap > 30%. Drop the
   lower-stage2-ranked fund of that pair. Re-compute pair overlaps if
   needed (typically no recomputation since dropping a fund removes pairs
   involving it).
3. Repeat until no remaining pair has overlap > 30%.

**Outputs**:
- `stage3/<category>.csv` — final clean picks per category (Stage 2
  survivors minus drops from Stage 3). Stays sorted by Stage 2 rank.
- `stage3/dropped.csv` — funds dropped in Stage 3 due to overlap, with:
  scheme_code_dropped, scheme_name_dropped, category_dropped,
  scheme_code_kept, overlap_pct, top_shared_holdings.
- `stage3/overlap_pairs.csv` — every pair > 30% overlap encountered
  during iteration (informational, includes pairs whose lower-ranked side
  was already dropped earlier).
- `stage3/mf_report.csv` — consolidated final list across categories.

---

## File-by-file changes

### `configs/pipeline.yaml`

Replace single `composite_weights` block with **two named blocks**:

```yaml
composite_weights_stage1:
  ret_3y_median: 0.15
  ret_3y_p25:    0.10
  ret_5y_median: 0.15
  ret_5y_p25:    0.10
  alpha_3y:      0.25
  sortino_3y:    0.10
  info_ratio_3y: 0.10
  capture_efficiency: 0.05
  # NO Phase 2 weights here — Stage 1 is pure Phase 1.

composite_weights_stage2:
  ret_3y_median: 0.10
  ret_3y_p25:    0.07
  ret_5y_median: 0.10
  ret_5y_p25:    0.07
  alpha_3y:      0.20
  sortino_3y:    0.08
  info_ratio_3y: 0.08
  capture_efficiency: 0.05
  active_share:  0.10
  style_drift:   -0.05

soft_penalties:
  ptr:
    enabled: true
    max_penalty: 0.05
    threshold: 1.50
  aum_impact_cost:  # NEW
    enabled: true
    max_penalty: 0.05
    threshold_days: 5.0  # ramp above 5 days
```

### `src/mfs/config.py`

- Replace `composite_weights: dict[str, float]` field on `PipelineConfig`
  with `composite_weights_stage1` and `composite_weights_stage2`, both
  `dict[str, float]`.
- Add `AumImpactCostPenalty` model class mirroring `PtrPenalty`.
- Add `aum_impact_cost: AumImpactCostPenalty` to `SoftPenaltiesConfig`.

### `src/mfs/compute/orchestrator.py`

Split `compute_for_scheme` and `run` into Phase 1 / Phase 2:

- `compute_phase1_for_scheme(scheme_code, benchmark_ticker, canonical_category, as_of, step)` — returns a dict with ONLY Phase 1 metric keys.
- `compute_phase2_for_scheme(scheme_code, scheme_holdings, benchmark_constituents, scheme_ptr, scheme_aum, as_of, step, alignment_data)` — returns a dict with ONLY Phase 2 metric keys (`style_drift_3y`, `active_share_median_1y`, `ptr_latest`, `aum_impact_cost_days`).
- `run_phase1(as_of, only_scheme=None)` — iterates all eligible schemes,
  calls `compute_phase1_for_scheme`, writes to `computed_metrics` (Phase 2
  cols left NULL). Returns the per-row dicts.
- `run_phase2(as_of, scheme_codes: list[str])` — iterates only the passed
  scheme_codes, calls `compute_phase2_for_scheme`, UPDATEs the Phase 2
  columns on the existing rows.
- Style Drift moves from being computed inline with Phase 1 (currently in
  the same rolling-regression call as alpha/beta) to its own step in
  Phase 2. Implementation note: the rolling regression itself stays in
  Phase 1 (we still need beta_3y and r_squared_3y as filter inputs); just
  compute and store `beta_3y_std` and `r_squared_3y_mean` ALWAYS in Phase
  1 (cheap), then compute `style_drift_3y = beta_3y_std + (1 − r_squared_3y_mean)`
  in Phase 2 as a derived column.

### `src/mfs/db/schema.sql`

No schema change needed — `computed_metrics` already has all the columns.
The two-stage update model uses UPDATE on the same row.

### `src/mfs/db/writers.py`

- Add `upsert_computed_metrics_phase1(df, as_of)` — writes only the Phase 1
  columns, leaving Phase 2 columns NULL on insert.
- Add `update_computed_metrics_phase2(rows, as_of)` — UPDATEs only Phase 2
  columns on rows identified by `(as_of_date, scheme_code)`.
- Keep `upsert_computed_metrics` for backward compatibility but mark
  deprecated.

### `src/mfs/rank/score.py`

- Rename current `composite_score(df)` → `composite_score_stage1(df)`.
- Pull weights from `cfg.composite_weights_stage1`.
- **Remove pro-rata redistribution logic entirely** — Stage 1 weights
  don't include active_share, so no redistribution needed.
- Keep `_apply_soft_penalties` but make it a no-op for Stage 1 (PTR +
  AUM Impact only fire in Stage 2).
- Add `composite_score_stage2(df)` — uses `cfg.composite_weights_stage2`,
  applies PTR + AUM Impact soft penalties.

### `src/mfs/rank/stage2.py`

Rewrite from null-filter to re-rank:

- `apply_stage2(scored_stage1, aum_map, pool_size=20, final_size=5)`:
  1. Per category: take top `pool_size` by Stage 1 composite_score.
  2. Filter to funds with all four Phase 2 metrics non-null.
  3. z-score within category (Phase 2 metrics only — Phase 1 z-scores
     reused from Stage 1 frame).
  4. Apply `composite_score_stage2`.
  5. Sort by composite_score_stage2 desc, take top `final_size`.
  6. Return (survivors, dropped, coverage) as before.

- `run` writes the three artifacts as before, plus a new `mf_report.csv`.

### `src/mfs/rank/stage3.py`

Rewrite from flag-only to iterative drop:

- `apply_stage3(survivors, holdings_loader, threshold_pct=30.0)`:
  1. Build pair list with overlap_pct computed.
  2. While any pair has overlap_pct > threshold:
     - Find the pair with highest overlap_pct.
     - Drop the fund with the higher (worse) stage2_rank from
       `survivors`.
     - Re-compute remaining pairs (in practice just filter out pairs
       involving the dropped fund).
     - Log the drop with the kept fund's identity for `dropped.csv`.
  3. Return `(final_picks, drops, overlap_pairs_log)`.

- `run` writes:
  - `stage3/<category>.csv` per category (= Stage 2 survivors minus
    Stage 3 drops).
  - `stage3/dropped.csv`
  - `stage3/overlap_pairs.csv`
  - `stage3/mf_report.csv`

### `src/mfs/rank/shortlist.py`

Replace `rank_deep` with a true 3-stage orchestrator:

```python
def rank_deep(as_of: date | None = None, ...) -> dict:
    # 1. Compute Phase 1 metrics for all schemes (if not already done).
    orchestrator.run_phase1(as_of=as_of)

    # 2. Stage 1 ranking: load Phase 1 metrics, filter, z-score, composite,
    #    dedup, write stage1/ outputs.
    as_of, scored_stage1 = build_scored_stage1(as_of=as_of)
    stage1_paths = _write_stage1_outputs(scored_stage1, as_of)

    # 3. Identify Stage 2 candidate pool: top-20 per category from Stage 1.
    top_20_per_cat = _top_n_per_category(scored_stage1, n=20)
    candidate_codes = top_20_per_cat["scheme_code"].to_list()

    # 4. Compute Phase 2 metrics ONLY for those candidates.
    orchestrator.run_phase2(as_of=as_of, scheme_codes=candidate_codes)

    # 5. Reload computed_metrics for the candidates (now with Phase 2 filled).
    candidates_with_phase2 = q.computed_metrics_for_schemes(
        as_of, candidate_codes,
    )

    # 6. Stage 2 ranking: filter null, re-rank, write stage2/ outputs.
    aum_map = _build_aum_map(candidates_with_phase2)
    stage2_result = stage2_mod.run(
        candidates_with_phase2, aum_map, out_dir,
        pool_size=20, final_size=5,
    )

    # 7. Stage 3: iterative overlap drop, write stage3/ outputs.
    stage3_result = stage3_mod.run(
        stage2_result["survivors"],
        _latest_holdings_loader(),
        out_dir,
        threshold_pct=30.0,
    )

    return {...}
```

### `src/mfs/cli.py`

- `mfs compute metrics` → split into `mfs compute phase1` and
  `mfs compute phase2` subcommands. Keep `mfs compute metrics` as a thin
  wrapper that runs both for back-compat.
- `mfs rank-deep` — wire to the new orchestrator (above).
- `mfs pipeline` — re-order so compute_phase1 happens before rank_stage1,
  then compute_phase2 happens only on the Stage 1 narrowed set.

### `src/mfs/db/queries.py`

- Add `computed_metrics_for_schemes(as_of, scheme_codes: list[str]) -> pl.DataFrame` — loads only the rows for the passed scheme_codes.

### Tests

- `tests/test_ranking.py`:
  - Update existing tests to use `composite_weights_stage1` (8 Phase 1
    metrics only). Remove any reliance on pro-rata-redistribution.
  - Add a Stage 2 composite_score test using `composite_weights_stage2`.
- `tests/test_stage2.py`:
  - Add re-rank tests (e.g. a fund that was Stage 1 rank 5 with great
    Active Share moves up to Stage 2 rank 1).
  - Keep the null-filter tests (still relevant — funds without Phase 2
    data still drop).
- `tests/test_stage3.py`:
  - Replace flag-only assertions with drop-iteration assertions.
  - Add cascade tests: 3 funds A/B/C all pairwise > 30% → drop B and C,
    keep A.

---

## Output directory structure (final)

```
data/output/shortlist/<as_of>/
├── stage1/
│   ├── <category>.csv (×26)
│   ├── <category>.parquet (×26)
│   ├── mf_report.csv
│   └── mf_report.parquet
├── stage2/
│   ├── <category>.csv (×N, where N = categories with ≥1 survivor)
│   ├── <category>.parquet (×N)
│   ├── dropped.csv
│   ├── coverage.csv
│   ├── mf_report.csv
│   └── mf_report.parquet
└── stage3/
    ├── <category>.csv (×N')
    ├── <category>.parquet (×N')
    ├── dropped.csv
    ├── overlap_pairs.csv
    ├── mf_report.csv
    └── mf_report.parquet
```

---

## Acceptance criteria

1. `uv run mfs pipeline` runs end-to-end and writes all three stage
   subdirectories.
2. **Stage 1 output** ranks every fund purely on Phase 1 metrics. No
   `active_share` or `style_drift` column appears in the Stage 1 CSV (or
   if it does, it's a metadata column only — not used in
   composite_score).
3. **Stage 2 output** has both Phase 1 + Phase 2 columns. Composite_score
   uses Stage 2 weights. Funds with missing Phase 2 metrics appear in
   `stage2/dropped.csv`, not the per-category survivor CSV.
4. **Stage 3 output**: no pair of funds in the final per-category CSVs
   has > 30% pairwise portfolio overlap. Funds dropped due to overlap
   appear in `stage3/dropped.csv`.
5. Pipeline produces a single `mf_report.csv` at each stage giving the
   user a flat view of "here are my top picks at this stage".
6. Test suite green (target: existing 287 passing + ~15 new stage2 +
   stage3 tests).

---

## Effort estimate

- config + schema changes: 30 min
- score.py split: 30 min
- compute orchestrator split: 1h
- stage2 re-rank: 1h
- stage3 iterative drop: 1h
- shortlist.py + cli.py orchestration: 45 min
- tests update: 1h
- pipeline integration test: 30 min

**Total**: ~6 hours of focused work.

---

## How to resume in a post-compact session

1. Read this entire file (`docs/phase4/staged_pipeline_refactor_plan.md`).
2. Read `MEMORY.md` for project constraints (no manual override, no
   nullable strict fields, fail-fast).
3. Run `uv run pytest 2>&1 | tail -3` to confirm baseline (287 passing
   today).
4. Execute the refactor in the file-by-file order listed above. Run the
   targeted test file after each module change (e.g. after editing
   `score.py`, run `pytest tests/test_ranking.py`).
5. End-of-refactor:
   - `uv run pytest` — should pass with similar count
   - `uv run mfs compute phase1`
   - `uv run mfs rank-deep` — verifies the staged orchestrator end-to-end
   - Inspect `data/output/shortlist/<as_of>/stage{1,2,3}/` for the three
     output subdirs.
6. Update this doc with what shipped vs what's still pending.

---

## Open items deferred from this refactor

- **Active Share temporal coverage**: still requires ≥3 monthly snapshots
  of holdings + constituents. With only 1 month on disk, Stage 2 will
  still produce 0 survivors until prior months are backfilled.
  Workarounds (lower `MIN_SNAPSHOTS_FOR_MEDIAN` to 1, OR backfill
  2026-01/02/03 holdings + constituents) are NOT in scope of this
  refactor.
- **Constituent CSV coverage**: 22 of 27 benchmarks still missing.
  Sector/thematic categories will continue to produce NULL Active Share
  regardless of this refactor.
- **AUM Impact Cost coverage**: limited to HDFC + SBI + Nippon today
  (Phase 3.C). ICICI Pru / ABSL / Mirae funds will continue to drop at
  Stage 2 on missing `aum_impact_cost_days`.

These limit the SIZE of Stage 2 / Stage 3 output but do NOT affect the
correctness of the staged-pipeline architecture itself. The architecture
will work correctly; coverage will grow as those data sources land.
