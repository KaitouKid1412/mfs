# Two-Stage Pipeline Plan

**Status**: design locked, implementation pending.
**Locked decisions** (from conversation 2026-05-24):
- Pool size: top-20 per category into Stage 2
- Survivors output: whatever survives, flag the gap (no padding, no strict empty)
- Overlap action: keep both funds when overlap > 30%, flag the breach (no auto-drop)

---

## Why two stages

The existing pipeline produces a single ranked output per category mixing
Phase 1 metrics (returns, alpha, Sortino, IR, capture, R², β) and Phase 2
metrics (Style Drift, Manager Tenure, PTR, Active Share, AUM Impact,
Stress Test). Coverage is uneven: Phase 1 metrics are universal (NAV-
derived), Phase 2 metrics are partial (~50-66% AUM-weighted), and a
fund's composite_score is well-defined even when Phase 2 metrics are
NULL (soft penalties don't fire on NULL).

Result: a fund with NO Phase 2 data competes against a fund with
MEASURED Phase 2 data on uneven footing. The data-rich fund can take soft
penalties; the data-poor fund is implicitly "innocent until measured".

The two-stage pipeline separates these:

- **Stage 1** answers "what's high-quality on the universal Phase 1
  metrics?" — same as today.
- **Stage 2** answers "of the top-20 in each category, which ones can we
  fully audit using Phase 2 manager-skill + liquidity-scale metrics?"
- **Stage 3** answers "across our final picks, are any pair of funds
  too overlapping in holdings to justify holding both?"

Each stage's output is preserved separately so the user sees exactly
where data gaps cut.

---

## Stage 1 (unchanged)

Existing `mfs rank` command. Outputs to
`data/output/shortlist/<as_of>/<category>.csv` (current behavior). This
plan does not touch Phase 1 logic.

---

## Stage 2 — Phase 2 data-completeness filter

### Input
Top-20 schemes per category from Stage 1, ordered by composite_score.

### Required Phase 2 metrics (per category)

| Metric | Source | Required for which categories? |
|---|---|---|
| Style Drift (R² + β realignment) | NAV-derived, computed_metrics.beta_3y_std + r_squared_3y_mean | ALL categories |
| Active Share | holdings_monthly ∩ index_constituents_monthly | ALL categories |
| PTR (Portfolio Turnover Ratio) | portfolio_turnover.ptr_latest | ALL categories |
| AUM Impact Cost | holdings_monthly (ISIN) + scheme_aum + stock_adv_daily | ALL categories |
| Manager Tenure | managers.manager_tenure_years (via tenure compute) | ALL categories |
| SEBI Stress Test | stress_test_quarterly.days_to_liquidate_50 | **Only Mid Cap + Small Cap** (per SEBI rule) |

**Category-aware eligibility**: a Large Cap fund with NULL Stress Test
days is NOT data-incomplete — that metric is by design only mandated for
Mid Cap and Small Cap. Stage 2's check honors this.

### Output

For each category, write three artifacts:

1. `data/output/shortlist/<as_of>/stage2/<category>.csv` — surviving funds
   (those with all-required metrics non-null), preserving Stage 1's
   composite_score-based order. **Top-5 per category** taken from the
   survivors. If fewer than 5 survive, output whatever count remains
   with a `partial_coverage_flag = True` column on each row.

2. `data/output/shortlist/<as_of>/stage2/dropped.csv` — one row per fund
   from the Stage 2 pool (top-20) that was dropped. Columns:
   - `canonical_category`
   - `stage1_rank`
   - `scheme_code`, `scheme_name`
   - `source_amc`
   - `composite_score`
   - `missing_metrics` — semicolon-delimited list of NULL metric names
   - `aum_crore` — fund AUM (so the user sees AUM-weighted gap)

3. `data/output/shortlist/<as_of>/stage2/coverage.csv` — per-category
   summary. Columns:
   - `canonical_category`
   - `n_considered` (size of Stage 2 pool, max 20)
   - `n_survived`
   - `n_dropped`
   - `pct_survived`
   - `aum_considered_crore`
   - `aum_survived_crore`
   - `aum_dropped_crore`
   - `pct_aum_survived`
   - `top_dropped_amc` — the AMC with the most dropped funds in this
     category (so the user can target ingest improvements)

### Ranking decision: filter-only, no Stage-2-specific re-rank

Stage 2 does **not** recompute composite_score over the surviving subset.
The existing composite_score already incorporates Phase 2 weights via the
configurable `composite_weights` config. Filtering preserves Stage 1's
ordering within the survivors. This is the simpler, more defensible
methodology — re-ranking a sub-cohort with sub-cohort z-scores would
quietly change the meaning of "rank 3".

(If you later want Stage-2-specific ranking with different weights, that's
a config change to composite_weights + a new Stage-2 pipeline call —
not a methodology change baked into this plan.)

---

## Stage 3 — Portfolio Overlap flagging

### Input
Stage 2 output across ALL categories — i.e., the union of top-5 (or
fewer) per category. With 25 rankable categories × ~5 funds = up to 125
funds. Pairwise comparisons = C(125, 2) ≈ 7,750 pairs. Fast.

### Overlap math

For each pair `(A, B)` from the Stage 2 final set:

```
overlap_pct(A, B) = sum_over_securities min(weight_A[s], weight_B[s])
```

where `s` ranges over the union of A's and B's holdings, joined on
`(isin OR normalized_security_name)`:

- Prefer ISIN match when both rows have ISIN (Phase 3.C-sourced rows).
- Fall back to `_normalize_security_name(name)` match for factsheet-
  sourced rows (lowercase, strip Ltd./Limited/punctuation/whitespace) —
  same normalization the existing Active Share code uses.

Use the most recent `as_of_month` for each fund (mismatched months are
fine for overlap math — they just measure stale-vs-fresh portfolios).

### Output

1. **No funds are dropped.** Stage 2 output is the final selection.
2. `data/output/shortlist/<as_of>/stage3/overlap_pairs.csv` — only pairs
   with overlap > 30%. Columns:
   - `scheme_code_a`, `scheme_name_a`, `category_a`
   - `scheme_code_b`, `scheme_name_b`, `category_b`
   - `overlap_pct`
   - `n_overlapping_securities`
   - `top_shared_holdings` — top-3 shared securities by min-weight, comma-
     separated (e.g., "HDFC Bank Ltd (5.2%), Reliance Industries (4.1%)")
3. `data/output/shortlist/<as_of>/stage3/overlap_summary.csv` — per
   category-pair summary (e.g., "Large Cap × Flexi Cap had 4 pairs > 30%
   overlap, max 47%").

The pair list goes into the final report so the user can decide whether
to pick fund A or fund B (the pipeline doesn't decide for them).

### Overlap scope: full cross-category

Compute overlap across ALL Stage 2 picks (all categories), not just
within a category. Rationale: within a category you'd pick one fund
anyway; the meaningful overlap concern is when you own e.g. one Large Cap
+ one Flexi Cap that share 40% of their portfolio.

(If you'd prefer to restrict to top-1 or top-3 per category for overlap
analysis, that's a CLI flag we can add later.)

---

## Output directory layout (full)

```
data/output/shortlist/<as_of>/
├── <category>.csv              # Stage 1 output (existing)
├── mf_report.csv               # Stage 1 consolidated (existing)
├── stage2/
│   ├── <category>.csv          # surviving top-5 per category
│   ├── dropped.csv             # all funds dropped from Stage 2 pool with reason
│   └── coverage.csv            # per-category AUM/count breakdown
└── stage3/
    ├── overlap_pairs.csv       # pairs > 30% overlap
    └── overlap_summary.csv     # per-category-pair summary
```

The existing Stage 1 outputs are NOT moved into a `stage1/` subdir —
backwards compatible with any downstream tooling.

---

## Implementation breakdown

| Module | Purpose | Effort |
|---|---|---|
| `src/mfs/rank/stage2.py` | filter logic, eligibility check, dropped/coverage reports | ~3h |
| `src/mfs/rank/overlap.py` | pairwise overlap math (pure function, no I/O) | ~3h |
| `src/mfs/rank/stage3.py` | apply overlap to Stage 2 output, write pair/summary CSVs | ~2h |
| `src/mfs/cli.py` | new `mfs rank-deep` command running all stages | ~1h |
| `tests/test_stage2.py` | filter logic, category-aware Stress Test, dropped/coverage shapes | ~1h |
| `tests/test_overlap.py` | overlap math: ISIN match, name fallback, mismatched months, empty intersection | ~1h |
| `tests/test_stage3.py` | end-to-end on synthetic Stage 2 input | ~0.5h |
| `docs/phase2/brittleness_audit.md` update | document Stage 2/3 design + ICICI Pru's expected dropout | ~0.5h |
| `.claude/commands/run-pipeline.md` update | add Stage 2/3 as Stage 11 + 12 | ~0.5h |

**Total**: ~12h. One focused session.

---

## How we handle lack of data (the explicit question)

### In Stage 2

For each fund in the top-20 pool of a category:

```python
required = REQUIRED_METRICS_BY_CATEGORY[fund.canonical_category]
missing = [m for m in required if fund[m] is None]

if not missing:
    fund passes → goes into stage2/<category>.csv
else:
    fund is dropped → goes into stage2/dropped.csv with missing_metrics = ";".join(missing)
```

If fewer than 5 funds pass in a category:
- `stage2/<category>.csv` has whatever count survived (could be 0, 1, 2, 3, 4)
- Every row gets `partial_coverage_flag = True` so downstream consumers know
- `stage2/coverage.csv` has `pct_survived < 100%` for that category

If 0 funds pass in a category, the file is still written (with header only), and
`coverage.csv` shows `n_survived = 0`. No exception is raised — silent gaps
become loud-but-non-fatal warnings via the coverage report.

### In Stage 3

Funds with INCOMPLETE holdings data (e.g. a Stage 2 survivor whose
holdings_monthly is empty for the most recent month) are still included
in the pair list, but pairs involving them get `overlap_pct = NULL` and a
`reason = "missing_holdings"` column. This is rare but possible (a Phase
2 metric got computed from older holdings while the most recent month
hasn't been ingested).

### Diagnostic surface

After every run, three numbers tell the user the state:

1. `coverage.csv` → "X of 25 categories had < 5 surviving funds in Stage 2"
2. `dropped.csv` group-by `source_amc` → "the AMC with the most drops"
3. `dropped.csv` group-by `missing_metrics` → "the metric most often
   missing" (e.g. AUM Impact dominates because of ICICI Pru's
   factsheet-only holdings)

These three roll up into the final report so the user can prioritize
which data gap to close next.

---

## Edge cases handled

1. **Empty Stage 1 category** → empty Stage 2, empty Stage 3, no errors.
2. **Stage 1 < 20 funds in category** → use all of them as Stage 2 pool.
3. **Stage 2 < 5 surviving** → output whatever count survived with
   `partial_coverage_flag = True`.
4. **Stage 2 = 1 surviving** → no pairs to overlap, that fund alone in Stage 3.
5. **Cross-month holdings** → most recent month per fund; small staleness
   is acceptable for overlap math.
6. **Factsheet holdings (no ISIN)** → fall back to normalized
   security_name match (existing helper).
7. **Stress Test category exception** → category-aware eligibility map.
8. **ICICI Pru funds in Stage 2 pool** → predicted to drop because AUM
   Impact NULL (no ISIN holdings); will surface clearly in `dropped.csv`.

---

## What this plan does NOT do

- **Does not re-rank within Stage 2 cohort.** Stage 1 order preserved.
  If you want a Stage-2-specific composite (e.g. emphasizing Active Share
  more), that's a follow-up — propose new composite_weights config.
- **Does not drop funds for overlap.** Per locked decision: flag, don't drop.
- **Does not introduce new metrics.** Uses existing computed_metrics
  columns; no compute pipeline changes.
- **Does not change Stage 1 output paths.** Existing `<category>.csv`
  files at the top level still work.
- **Does not validate cross-AMC scheme matching for overlap.** Trusts the
  `scheme_code` PK; if two share-class variants of the same fund got
  through Stage 1 dedup, they'd still appear in overlap pairs.

---

## Acceptance criteria

After implementation:
- `uv run mfs rank-deep` runs Stage 1 + 2 + 3 end-to-end.
- All three output directories are written with expected files.
- 235 + 32 + 71 + 29 + 32 = ~400 existing tests still pass.
- ~25-30 new tests added covering Stage 2 eligibility, Stage 3 overlap math,
  and Stage 2/3 reports.
- `dropped.csv` shows ICICI Pru funds dropping due to NULL AUM Impact (the
  predicted clean-signal outcome).
- Coverage report makes it obvious which AMC to fix next.

---

## Open question (small)

I have one small choice to confirm before coding:

**CLI surface**: single `mfs rank-deep` command that runs all three
stages, OR separate `mfs rank-stage2` and `mfs rank-stage3` commands that
read from Stage 1 / Stage 2 output respectively?

Recommendation: **single `mfs rank-deep`** for simplicity. Stages 2 and 3
are fast (<10s); no need to re-run them separately. If you ever want to
re-run Stage 3 with different overlap thresholds, that becomes a flag on
`rank-deep`, not a separate command.

If you confirm "single command" (or override), I'll start coding.
