"""Stage 2 — combined Phase 1 + Phase 2 re-rank within each category pool.

Stage 1 (``mfs.rank.shortlist``) ranks every scheme on Phase 1 metrics
alone and emits one CSV per category. Stage 2 takes the top-N (default
20) from each category, drops funds missing any *required* Phase 2 metric
(see ``REQUIRED_METRICS_ALL`` — active_share is an optional soft signal, not
a survival gate), and **re-ranks** the survivors using the Stage 2 weight
vector (Phase 1
metrics with weights downshifted to make room for active_share +
style_drift + soft penalties from PTR and AUM Impact Cost).

Outputs:
  1. ``stage2/<category>.csv`` — top-N_final (default 5) per category,
     ordered by ``composite_score_v2``, with ``stage2_rank`` and
     ``partial_coverage_flag`` columns.
  2. ``stage2/dropped.csv`` — every fund from a Stage 1 top-N pool that
     was dropped because of a NULL *required* Phase 2 metric.
  3. ``stage2/coverage.csv`` — per-category counts + AUM rollup.
  4. ``stage2/mf_report.csv`` — top-3 per category consolidated view.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from mfs.rank.score import composite_score_stage2
from mfs.rank.zscore import zscore_within_category
from mfs.utils.logging import get_logger

log = get_logger(__name__)

# Required Phase 2 metrics. A scheme survives Stage 2 only if every metric
# below is non-null.
#
# active_share_median_1y is intentionally NOT required. It needs >=3 monthly
# snapshots of BOTH fund holdings and benchmark constituents (see
# compute/active_share.py: MIN_SNAPSHOTS_FOR_MEDIAN), and as of mid-2026 we have
# only ~1 month of constituent weights, so it is null universe-wide. Requiring
# it would drop every fund from Stage 2. It remains a *soft* signal — weighted
# into the Stage 2 composite when present (composite_score_stage2) and
# contributing 0 when null — and folds back in automatically once >=3 months of
# holdings/constituent history accumulate.
REQUIRED_METRICS_ALL = (
    "style_drift_3y",
    "ptr_latest",
    "aum_impact_cost_days",
)

DEFAULT_POOL_SIZE = 20
DEFAULT_FINAL_SIZE = 5
REPORT_TOP_N_PER_CATEGORY = 3


def required_metrics_for_category(category: str) -> tuple[str, ...]:
    """Which Phase 2 metrics must be non-null for a fund in this category
    to survive Stage 2."""
    return REQUIRED_METRICS_ALL


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


def apply_stage2(
    scored: pl.DataFrame,
    aum_map: dict[str, float],
    pool_size: int = DEFAULT_POOL_SIZE,
    final_size: int = DEFAULT_FINAL_SIZE,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Pure function: pass Stage 1's scored frame, return (survivors,
    dropped, coverage) as polars frames. Disk-free for unit-testability.

    The input frame is expected to carry ``composite_score`` (Stage 1's
    score). Survivors get a new ``composite_score`` overwritten by Stage 2
    plus a preserved ``composite_score_stage1`` column for traceability.
    """
    if scored.is_empty():
        return pl.DataFrame(), pl.DataFrame(), pl.DataFrame()

    scored = _attach_latest_aum(scored, aum_map)
    # Preserve the Stage 1 number under a stable name before Stage 2 overwrites
    # ``composite_score``.
    if "composite_score" in scored.columns and "composite_score_stage1" not in scored.columns:
        scored = scored.with_columns(
            pl.col("composite_score").alias("composite_score_stage1")
        )

    survivors_chunks: list[pl.DataFrame] = []
    dropped_chunks: list[pl.DataFrame] = []
    coverage_rows: list[dict] = []

    for cat_tuple, grp in scored.group_by("canonical_category"):
        cat = cat_tuple[0]
        required = required_metrics_for_category(cat)
        # Tag with Stage 1 rank within the pool.
        pool = (
            grp.sort("composite_score_stage1", descending=True, nulls_last=True)
            .head(pool_size)
            .with_row_index("stage1_rank", offset=1)
        )
        miss_lists: list[list[str]] = [
            _missing_metrics(r, required) for r in pool.iter_rows(named=True)
        ]
        survives = [len(m) == 0 for m in miss_lists]
        miss_strs = [";".join(m) if m else "" for m in miss_lists]
        pool = pool.with_columns(
            pl.Series("_missing", miss_strs),
            pl.Series("_survives", survives),
        )

        cat_pool_survivors = pool.filter(pl.col("_survives")).drop(["_missing", "_survives"])

        if not cat_pool_survivors.is_empty():
            # Re-z-score within the surviving cohort so Phase 2 z-scores
            # reflect the narrowed pool. Stage 1 z-scores from the input
            # are dropped and recomputed because the pool changed.
            zcols = [c for c in cat_pool_survivors.columns if c.startswith("z_")]
            if zcols:
                cat_pool_survivors = cat_pool_survivors.drop(zcols)
            re_z = zscore_within_category(cat_pool_survivors)
            re_scored = composite_score_stage2(re_z)
            cat_survivors = (
                re_scored.sort("composite_score", descending=True, nulls_last=True)
                .head(final_size)
                .with_row_index("stage2_rank", offset=1)
            )
            partial = cat_survivors.height < final_size
            cat_survivors = cat_survivors.with_columns(
                pl.lit(partial).alias("partial_coverage_flag")
            )
        else:
            cat_survivors = pl.DataFrame()

        survivors_chunks.append(cat_survivors)

        cat_dropped = pool.filter(~pl.col("_survives")).with_columns(
            pl.col("_missing").alias("missing_metrics"),
        ).drop(["_missing", "_survives"])
        dropped_chunks.append(cat_dropped)

        aum_considered = float(pool["aum_crore"].fill_null(0).sum())
        aum_survived = (
            float(cat_survivors["aum_crore"].fill_null(0).sum())
            if not cat_survivors.is_empty() else 0.0
        )
        top_dropped_amc = None
        if cat_dropped.height > 0 and "source_amc" in cat_dropped.columns:
            counts = cat_dropped.group_by("source_amc").len().sort("len", descending=True)
            if counts.height > 0:
                top_dropped_amc = counts.row(0, named=True).get("source_amc")
        n_survived = int(cat_survivors.height) if not cat_survivors.is_empty() else 0
        coverage_rows.append({
            "canonical_category": cat,
            "n_considered": pool.height,
            "n_survived": n_survived,
            "n_dropped": cat_dropped.height,
            "pct_survived": (
                round(100.0 * n_survived / pool.height, 1) if pool.height else 0.0
            ),
            "aum_considered_crore": round(aum_considered, 2),
            "aum_survived_crore": round(aum_survived, 2),
            "aum_dropped_crore": round(aum_considered - aum_survived, 2),
            "pct_aum_survived": (
                round(100.0 * aum_survived / aum_considered, 1) if aum_considered else 0.0
            ),
            "top_dropped_amc": top_dropped_amc,
        })

    survivors_nonempty = [s for s in survivors_chunks if not s.is_empty()]
    survivors = (
        pl.concat(survivors_nonempty, how="diagonal_relaxed")
        if survivors_nonempty else pl.DataFrame()
    )
    dropped_nonempty = [d for d in dropped_chunks if not d.is_empty()]
    dropped = (
        pl.concat(dropped_nonempty, how="diagonal_relaxed")
        if dropped_nonempty else pl.DataFrame()
    )
    coverage = pl.DataFrame(coverage_rows) if coverage_rows else pl.DataFrame()
    return survivors, dropped, coverage


def run(
    scored: pl.DataFrame,
    aum_map: dict[str, float],
    output_dir: Path,
    pool_size: int = DEFAULT_POOL_SIZE,
    final_size: int = DEFAULT_FINAL_SIZE,
) -> dict:
    """End-to-end Stage 2: re-rank + write artifacts.

    Writes ``stage2/<category>.csv`` (+ ``.parquet``), ``stage2/dropped.csv``,
    ``stage2/coverage.csv``, and a consolidated ``stage2/mf_report.csv``
    (+ ``.parquet``) with the top-3 per category.
    """
    survivors, dropped, coverage = apply_stage2(
        scored, aum_map, pool_size=pool_size, final_size=final_size,
    )
    stage2_dir = output_dir / "stage2"
    stage2_dir.mkdir(parents=True, exist_ok=True)

    cat_paths: dict[str, Path] = {}
    report_chunks: list[pl.DataFrame] = []
    if not survivors.is_empty() and "canonical_category" in survivors.columns:
        for cat_tuple, grp in survivors.group_by("canonical_category"):
            cat = cat_tuple[0]
            safe = "".join(c if c.isalnum() else "_" for c in cat)
            sorted_grp = grp.sort("stage2_rank")
            csv_path = stage2_dir / f"{safe}.csv"
            parquet_path = stage2_dir / f"{safe}.parquet"
            sorted_grp.write_csv(csv_path)
            sorted_grp.write_parquet(parquet_path, compression="zstd")
            cat_paths[cat] = csv_path
            report_chunks.append(sorted_grp.head(REPORT_TOP_N_PER_CATEGORY))

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
            ["canonical_category", "stage2_rank"]
        )
        report.write_csv(stage2_dir / "mf_report.csv")
        report.write_parquet(stage2_dir / "mf_report.parquet", compression="zstd")

    log.info(
        "rank.stage2.done",
        n_categories=len(cat_paths),
        n_survivors=int(survivors.height),
        n_dropped=int(dropped.height),
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
