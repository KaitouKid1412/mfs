"""Stage 2 — combined Phase 1 + Phase 2 re-rank within each category pool.

Stage 1 (``mfs.rank.shortlist``) ranks every scheme on Phase 1 metrics
alone and emits one CSV per category. Stage 2 takes the top-N (default
20) from each category and **re-ranks** every pool fund using the Stage 2
weight vector (Phase 1 metrics with weights downshifted to make room for
active_share + style_drift + soft penalties from PTR and AUM Impact Cost).

D3 (A2-4): disclosure nulls are *soft-neutral* — no fund is dropped for a
missing Phase 2 metric. A fund missing any ``DISCLOSURE_METRICS`` entry is
flagged (``partial_disclosure_flag`` + ``missing_disclosures``), its missing
positive weight is renormalized across the present inputs, and a calibrated
fixed penalty applies once per missing metric that is *live* in its category
pool (see ``score._apply_soft_penalties``; active_share is renormalize-only
while null universe-wide and joins the penalty set when it activates).

Outputs:
  1. ``stage2/<category>.csv`` — top-N_final (default 5) per category,
     ordered by ``composite_score``, with ``stage2_rank``,
     ``partial_coverage_flag``, ``partial_disclosure_flag``,
     ``missing_disclosures``, ``liquidity_flag`` and the E1 cohort-context
     columns ``n_considered`` / ``n_survived``.
  2. ``stage2/dropped.csv`` — vestigial since D3 (header-only; kept for
     tooling compat).
  3. ``stage2/coverage.csv`` — per-category counts + AUM rollup
     (``n_dropped`` is structurally 0; ``n_partial_disclosure`` counts the
     flagged funds).
  4. ``stage2/mf_report.csv`` — top-3 per category consolidated view.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from mfs.rank.score import composite_score_stage2
from mfs.rank.zscore import zscore_within_category
from mfs.utils.logging import get_logger

log = get_logger(__name__)

# D3 (A2-4): Phase 2 disclosure metrics. A null here never drops a fund —
# it flags it (``partial_disclosure_flag`` / ``missing_disclosures``,
# regardless of pool liveness) and draws the calibrated fixed penalty for
# each metric that is live in the fund's category pool (see
# score.MISSING_DISCLOSURE_PENALTY_METRICS — kept in sync by a test).
#
# active_share_median_1y needs >=3 monthly snapshots of BOTH fund holdings
# and benchmark constituents (compute/active_share.py:
# MIN_SNAPSHOTS_FOR_MEDIAN) and is null universe-wide as of mid-2026, so its
# pool liveness is 0 and it is renormalize-only; it joins the penalty set
# automatically once it activates (~2026-07 via D9).
DISCLOSURE_METRICS = (
    "ptr_latest",
    "style_drift_3y",
    "aum_impact_cost_days",
    "active_share_median_1y",
)

DEFAULT_POOL_SIZE = 20
DEFAULT_FINAL_SIZE = 5
REPORT_TOP_N_PER_CATEGORY = 3

# A2-5: Phase 2 metrics exist only for the candidate pool (orchestrator
# run_phase2 is restricted to it), so they are z-scored within the
# per-category pool — the maximal available cohort. Phase 1 z-scores are
# carried from Stage 1's full universe and never recomputed here.
POOL_Z_METRICS = ("style_drift_3y", "active_share_median_1y")
# Pools smaller than this get neutral (0.0) Phase 2 z-scores instead of
# n<5 noise — nanstd over a 2-fund cohort once zeroed a measured fund's z
# and handed a category win to a null-metric fund.
MIN_COHORT_FOR_Z = 5

# A2-6 (E3 design, cross-workstream contract): liquidity tiering on
# days-to-exit (aum_impact_cost_days). Null days → null flag (the
# missing-disclosure machinery covers it).
LIQUIDITY_FLAG_HIGH_DAYS = 30.0
LIQUIDITY_FLAG_SEVERE_DAYS = 90.0


def disclosure_metrics_for_category(category: str) -> tuple[str, ...]:
    """Which Phase 2 disclosure metrics are flagged (and, when live in the
    pool, penalized) if null for a fund in this category."""
    return DISCLOSURE_METRICS


def _missing_metrics(row: dict, required: tuple[str, ...]) -> list[str]:
    return [m for m in required if row.get(m) is None]


def _attach_latest_aum(df: pl.DataFrame, aum_map: dict[str, float]) -> pl.DataFrame:
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("aum_crore"))
    if "aum_crore" in df.columns:
        return df
    aum_col = pl.col("scheme_code").map_elements(
        lambda c: aum_map.get(c), return_dtype=pl.Float64,
    )
    return df.with_columns(aum_col.alias("aum_crore"))


def _attach_latest_ter(df: pl.DataFrame, ter_map: dict[str, float]) -> pl.DataFrame:
    """Attach the latest direct-plan TER (%) as ``ter_pct`` — a display column
    and the Stage-2 tiebreaker key. Always present (null when unmatched/absent)
    so the tiebreak sort has a stable column; an all-null ``ter_pct`` is a
    no-op (the sort falls through to scheme_code, exactly as before TER)."""
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("ter_pct"))
    if "ter_pct" in df.columns:
        return df
    ter_col = pl.col("scheme_code").map_elements(
        lambda c: ter_map.get(c), return_dtype=pl.Float64,
    )
    return df.with_columns(ter_col.alias("ter_pct"))


def apply_stage2(
    scored: pl.DataFrame,
    aum_map: dict[str, float],
    pool_size: int | None = DEFAULT_POOL_SIZE,
    final_size: int | None = DEFAULT_FINAL_SIZE,
    ter_map: dict[str, float] | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Pure function: pass Stage 1's scored frame, return (survivors,
    dropped, coverage) as polars frames. Disk-free for unit-testability.

    D3: ``dropped`` is structurally empty — every pool fund proceeds;
    disclosure nulls are flagged + penalized instead (the return slot is
    kept for caller/tooling compat).

    The input frame is expected to carry ``composite_score`` (Stage 1's
    score). Survivors get a new ``composite_score`` overwritten by Stage 2
    plus a preserved ``composite_score_stage1`` column for traceability.
    """
    if scored.is_empty():
        return pl.DataFrame(), pl.DataFrame(), pl.DataFrame()

    scored = _attach_latest_aum(scored, aum_map)
    scored = _attach_latest_ter(scored, ter_map or {})
    # Preserve the Stage 1 number under a stable name before Stage 2 overwrites
    # ``composite_score``.
    if "composite_score" in scored.columns and "composite_score_stage1" not in scored.columns:
        scored = scored.with_columns(
            pl.col("composite_score").alias("composite_score_stage1")
        )

    survivors_chunks: list[pl.DataFrame] = []
    coverage_rows: list[dict] = []

    for cat_tuple, grp in scored.group_by("canonical_category"):
        cat = cat_tuple[0]
        disclosures = disclosure_metrics_for_category(cat)
        # Tag with Stage 1 rank within the pool. A2-11 determinism:
        # (composite desc, scheme_code asc) so tied composites don't inherit
        # arbitrary row order.
        pool = grp.sort(
            ["composite_score_stage1", "scheme_code"],
            descending=[True, False], nulls_last=True,
        )
        if pool_size is not None:  # None → every fund proceeds (full-universe)
            pool = pool.head(pool_size)
        pool = pool.with_row_index("stage1_rank", offset=1)
        # D3: every pool fund proceeds. Missing disclosures are flagged
        # (regardless of pool liveness) and soft-penalized in the composite.
        miss_lists: list[list[str]] = [
            _missing_metrics(r, disclosures) for r in pool.iter_rows(named=True)
        ]
        pool = pool.with_columns(
            pl.Series(
                "missing_disclosures",
                [";".join(m) if m else "" for m in miss_lists],
                dtype=pl.Utf8,
            ),
            pl.Series(
                "partial_disclosure_flag",
                [len(m) > 0 for m in miss_lists],
                dtype=pl.Boolean,
            ),
        )
        n_partial = sum(1 for m in miss_lists if m)

        # A2-6: liquidity tiering on days-to-exit (string enum OK|HIGH|SEVERE
        # per the cross-workstream contract; null days → null flag).
        if "aum_impact_cost_days" in pool.columns:
            days = pl.col("aum_impact_cost_days")
            liquidity = (
                pl.when(days.is_null())
                .then(pl.lit(None, dtype=pl.Utf8))
                .when(days >= LIQUIDITY_FLAG_SEVERE_DAYS)
                .then(pl.lit("SEVERE"))
                .when(days >= LIQUIDITY_FLAG_HIGH_DAYS)
                .then(pl.lit("HIGH"))
                .otherwise(pl.lit("OK"))
            )
        else:
            liquidity = pl.lit(None, dtype=pl.Utf8)
        pool = pool.with_columns(liquidity.alias("liquidity_flag"))

        # A2-5: Phase 1 z-scores are carried from Stage 1 (full-universe
        # statistics); only the pool-scoped Phase 2 metrics are z-scored
        # here, once, pre-selection. Tiny pools get neutral Phase 2 z.
        if pool.height >= MIN_COHORT_FOR_Z:
            pool_z = zscore_within_category(pool, metrics=POOL_Z_METRICS)
        else:
            pool_z = pool.with_columns(
                pl.lit(0.0).alias(f"z_{m}") for m in POOL_Z_METRICS
            )
        re_scored = composite_score_stage2(pool_z)
        # A2-11 determinism + C2 TER tiebreak: composite desc, then LOWER TER
        # wins an (exact) composite tie, then scheme_code asc as the final
        # backstop. ter_pct is null when TER is unmatched/absent and sorts last
        # within a tie group, so an all-null ter_pct reproduces the prior
        # (composite, scheme_code) order exactly.
        cat_survivors = re_scored.sort(
            ["composite_score", "ter_pct", "scheme_code"],
            descending=[True, False, False], nulls_last=True,
        )
        if final_size is not None:  # None → keep every re-scored fund
            cat_survivors = cat_survivors.head(final_size)
        cat_survivors = cat_survivors.with_row_index("stage2_rank", offset=1)
        # 'thin cohort' is meaningless when we deliberately keep every fund.
        partial = final_size is not None and cat_survivors.height < final_size
        n_survived = int(cat_survivors.height)
        cat_survivors = cat_survivors.with_columns(
            pl.lit(partial).alias("partial_coverage_flag"),
            # E1: cohort context so the final report distinguishes 'best of
            # 20' from 'only survivor of 1' (the FMCG cohort-of-one case).
            pl.lit(int(pool.height)).alias("n_considered"),
            pl.lit(n_survived).alias("n_survived"),
        )

        survivors_chunks.append(cat_survivors)

        aum_considered = float(pool["aum_crore"].fill_null(0).sum())
        aum_survived = (
            float(cat_survivors["aum_crore"].fill_null(0).sum())
            if not cat_survivors.is_empty() else 0.0
        )
        coverage_rows.append({
            "canonical_category": cat,
            "n_considered": pool.height,
            "n_survived": n_survived,
            # Structurally 0 since D3 (soft-neutral disclosure nulls); kept
            # for tooling compat.
            "n_dropped": 0,
            "n_partial_disclosure": n_partial,
            "pct_survived": (
                round(100.0 * n_survived / pool.height, 1) if pool.height else 0.0
            ),
            "aum_considered_crore": round(aum_considered, 2),
            "aum_survived_crore": round(aum_survived, 2),
            "aum_dropped_crore": round(aum_considered - aum_survived, 2),
            "pct_aum_survived": (
                round(100.0 * aum_survived / aum_considered, 1) if aum_considered else 0.0
            ),
            "top_dropped_amc": None,  # vestigial since D3
        })

    survivors_nonempty = [s for s in survivors_chunks if not s.is_empty()]
    survivors = (
        pl.concat(survivors_nonempty, how="diagonal_relaxed")
        if survivors_nonempty else pl.DataFrame()
    )
    # D3: structurally empty — kept so callers/tooling keep their shape.
    dropped = pl.DataFrame()
    coverage = pl.DataFrame(coverage_rows) if coverage_rows else pl.DataFrame()
    return survivors, dropped, coverage


def run(
    scored: pl.DataFrame,
    aum_map: dict[str, float],
    output_dir: Path,
    pool_size: int | None = DEFAULT_POOL_SIZE,
    final_size: int | None = DEFAULT_FINAL_SIZE,
    ter_map: dict[str, float] | None = None,
) -> dict:
    """End-to-end Stage 2: re-rank + write artifacts.

    Writes ``stage2/<category>.csv`` (+ ``.parquet``), ``stage2/dropped.csv``
    (vestigial header-only since D3), ``stage2/coverage.csv``, and a
    consolidated ``stage2/mf_report.csv`` (+ ``.parquet``) with the top-3
    per category.
    """
    survivors, dropped, coverage = apply_stage2(
        scored, aum_map, pool_size=pool_size, final_size=final_size,
        ter_map=ter_map,
    )
    stage2_dir = output_dir / "stage2"
    stage2_dir.mkdir(parents=True, exist_ok=True)

    cat_paths: dict[str, Path] = {}
    report_chunks: list[pl.DataFrame] = []
    if not survivors.is_empty() and "canonical_category" in survivors.columns:
        for cat_tuple, grp in survivors.group_by("canonical_category"):
            cat = cat_tuple[0]
            safe = "".join(c if c.isalnum() else "_" for c in cat)
            # stage2_rank is already deterministic (A2-11); scheme_code asc
            # is the backstop should it ever be null.
            sorted_grp = grp.sort(["stage2_rank", "scheme_code"], nulls_last=True)
            csv_path = stage2_dir / f"{safe}.csv"
            parquet_path = stage2_dir / f"{safe}.parquet"
            sorted_grp.write_csv(csv_path)
            sorted_grp.write_parquet(parquet_path, compression="zstd")
            cat_paths[cat] = csv_path
            report_chunks.append(sorted_grp.head(REPORT_TOP_N_PER_CATEGORY))

    # Vestigial since D3: apply_stage2 never drops, so this is always the
    # header-only schema — kept because run-pipeline tooling reads the path.
    dropped_path = stage2_dir / "dropped.csv"
    if not dropped.is_empty():
        dropped.sort(["canonical_category", "stage1_rank"]).write_csv(dropped_path)
    else:
        pl.DataFrame({
            "canonical_category": pl.Series([], dtype=pl.Utf8),
            "stage1_rank": pl.Series([], dtype=pl.Int64),
            "scheme_code": pl.Series([], dtype=pl.Utf8),
            "missing_metrics": pl.Series([], dtype=pl.Utf8),
        }).write_csv(dropped_path)

    coverage_path = stage2_dir / "coverage.csv"
    if not coverage.is_empty():
        coverage.sort("canonical_category").write_csv(coverage_path)
    else:
        pl.DataFrame({
            "canonical_category": pl.Series([], dtype=pl.Utf8),
            "n_considered": pl.Series([], dtype=pl.Int64),
            "n_survived": pl.Series([], dtype=pl.Int64),
        }).write_csv(coverage_path)

    if report_chunks:
        report = pl.concat(report_chunks, how="diagonal_relaxed").sort(
            ["canonical_category", "stage2_rank", "scheme_code"],
            nulls_last=True,
        )
        report.write_csv(stage2_dir / "mf_report.csv")
        report.write_parquet(stage2_dir / "mf_report.parquet", compression="zstd")

    log.info(
        "rank.stage2.done",
        n_categories=len(cat_paths),
        n_survivors=int(survivors.height),
        n_dropped=int(dropped.height),
        n_partial_disclosure=int(
            survivors.filter(pl.col("partial_disclosure_flag")).height
        ) if not survivors.is_empty() else 0,
        partial_coverage_categories=int(
            survivors.filter(pl.col("partial_coverage_flag")).select(
                pl.col("canonical_category").n_unique()
            ).item()
        ) if not survivors.is_empty() else 0,
    )
    return {
        "stage2_dir": str(stage2_dir),
        "category_files": {k: str(v) for k, v in cat_paths.items()},
        "dropped_file": str(dropped_path),
        "coverage_file": str(coverage_path),
        "n_survivors": int(survivors.height),
        "n_dropped": int(dropped.height),
        "survivors": survivors,
        "dropped": dropped,
        "coverage": coverage,
    }
