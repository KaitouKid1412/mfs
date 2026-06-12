"""Top-level ranker: filters → zscore → composite_score_stage1 → write
shortlist per category, then orchestrate Stage 2 and Stage 3."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from mfs import paths
from mfs.db import queries as q
from mfs.db import writers as w
from mfs.rank.filters import apply_hard_filters
from mfs.rank.score import composite_score_stage1
from mfs.rank.zscore import zscore_within_category
from mfs.utils.logging import get_logger

log = get_logger(__name__)

# Stage 1 only carries Phase 1 metric columns. Phase 2 columns (active_share,
# style_drift, ptr, aum_impact) are intentionally omitted from Stage 1 output
# — the architecture says Stage 1 is pure Performance & Consistency.
STAGE1_OUTPUT_COLS = [
    "as_of_date",
    "canonical_category",
    "rank",
    "scheme_code",
    "scheme_name",
    "composite_score",
    "z_ret_3y_median",
    "z_ret_3y_p25",
    "z_ret_5y_median",
    "z_ret_5y_p25",
    "z_alpha_3y_annualized",
    "z_sortino_3y",
    "z_info_ratio_3y",
    "z_capture_efficiency",
    "ret_3y_median",
    "ret_3y_p25",
    "ret_5y_median",
    "ret_5y_p25",
    "alpha_3y_annualized",
    "sortino_3y",
    "info_ratio_3y",
    "capture_up",
    "capture_down",
    "capture_efficiency",
    "r_squared_3y",
    "beta_3y",
    "data_quality_flag",
]

REPORT_TOP_N_PER_CATEGORY = 5
REPORT_FILENAME = "mf_report"

# D2 run history: flat z-score columns mirrored from the stage CSVs into the
# rank_history table. The first eight are Stage 1's; the last two are the
# Phase 2 z-scores Stage 2 adds when it re-z-scores the survivor pool.
RANK_HISTORY_Z_COLS = [
    "z_ret_3y_median",
    "z_ret_3y_p25",
    "z_ret_5y_median",
    "z_ret_5y_p25",
    "z_alpha_3y_annualized",
    "z_sortino_3y",
    "z_info_ratio_3y",
    "z_capture_efficiency",
    "z_active_share_median_1y",
    "z_style_drift_3y",
]

# Full rank_history frame schema (column -> dtype, in table order). Must stay
# identical to writers._RANK_HISTORY_COLS / the rank_history DDL in schema.sql.
RANK_HISTORY_SCHEMA: dict[str, pl.DataType] = {
    "as_of_date": pl.Date,
    "stage": pl.Int32,
    "scheme_code": pl.Utf8,
    "canonical_category": pl.Utf8,
    "composite_score": pl.Float64,
    "composite_score_stage1": pl.Float64,
    "rank_in_category": pl.Int32,
    **{c: pl.Float64 for c in RANK_HISTORY_Z_COLS},
    "ptr_latest": pl.Float64,
    "aum_impact_cost_days": pl.Float64,
    "included": pl.Boolean,
    "exclusion_reason": pl.Utf8,
    "run_id": pl.Utf8,
}


def _join_scheme_master(metrics: pl.DataFrame) -> pl.DataFrame:
    sm = q.scheme_master()
    if sm.is_empty():
        return metrics.with_columns(
            pl.lit(None, dtype=pl.Utf8).alias("scheme_name"),
            pl.lit(None, dtype=pl.Utf8).alias("base_fund_id"),
        )
    sm = sm.select(["scheme_code", "scheme_name", "base_fund_id"])
    return metrics.join(sm, on="scheme_code", how="left")


def _dedupe_legacy_unit_classes(scored: pl.DataFrame) -> pl.DataFrame:
    """Collapse AMFI's legacy 'Bonus Option' duplicates within each category.

    AMFI lists Growth Option and Bonus Option of the same underlying portfolio
    under separate scheme_codes; both pass our Direct+Growth filter, produce
    identical NAV histories, and so produce identical metrics. Keep the
    Growth Option (non-`_bonus`); drop the Bonus Option.
    """
    if "base_fund_id" not in scored.columns or scored.is_empty():
        return scored
    with_key = scored.with_columns(
        pl.col("base_fund_id")
        .fill_null("")
        .str.replace(r"_bonus$", "")
        .alias("_dedup_key"),
        pl.col("base_fund_id")
        .fill_null("")
        .str.ends_with("_bonus")
        .alias("_is_legacy"),
    )
    winners = (
        with_key.sort(
            ["_is_legacy", "composite_score"], descending=[False, True], nulls_last=True
        )
        .unique(subset=["canonical_category", "_dedup_key"], keep="first")
        .select(["canonical_category", "_dedup_key", "scheme_code"])
        .rename({"scheme_code": "_winner_code"})
    )
    enriched = with_key.join(winners, on=["canonical_category", "_dedup_key"], how="left")
    losers = enriched.filter(pl.col("scheme_code") != pl.col("_winner_code"))
    for r in losers.iter_rows(named=True):
        log.info(
            "rank.dedup.dropped",
            category=r["canonical_category"],
            dropped_scheme_code=r["scheme_code"],
            dropped_scheme_name=r.get("scheme_name"),
            kept_scheme_code=r["_winner_code"],
            base_fund_id=r["base_fund_id"],
            reason="legacy_bonus_option",
        )
    if losers.height > 0:
        tally = losers.group_by("canonical_category").len().sort("canonical_category")
        log.info(
            "rank.dedup.tally",
            total_dropped=losers.height,
            by_category={
                r["canonical_category"]: r["len"] for r in tally.iter_rows(named=True)
            },
        )
    return (
        enriched.filter(pl.col("scheme_code") == pl.col("_winner_code"))
        .drop(["_dedup_key", "_is_legacy", "_winner_code"])
    )


def build_scored_stage1(
    as_of: date | None = None,
    category: str | None = None,
    skip_filters: bool = False,
) -> tuple[date, pl.DataFrame, pl.DataFrame]:
    """Stage 1 in-memory: load metrics, filter, z-score, composite_score_stage1,
    dedupe legacy bonus options. Returns ``(as_of, scored, excluded)`` where
    ``excluded`` is the hard-filter drops with an ``exclusion_reason`` column
    (contract vocabulary; empty when ``skip_filters=True``) — persisted into
    rank_history by ``rank_deep``.
    """
    as_of = as_of or q.latest_computed_metrics_date()
    if as_of is None:
        raise RuntimeError("No computed_metrics available; run `mfs compute phase1` first")

    metrics = q.computed_metrics_at(as_of)
    if metrics.is_empty():
        raise RuntimeError(f"No computed_metrics rows for as_of={as_of.isoformat()}")

    if skip_filters:
        survivors = metrics.filter(pl.col("canonical_category").is_not_null())
        excluded = pl.DataFrame()
    else:
        survivors, excluded = apply_hard_filters(metrics, with_reasons=True)
    if category:
        survivors = survivors.filter(pl.col("canonical_category") == category)
        if not excluded.is_empty():
            excluded = excluded.filter(pl.col("canonical_category") == category)

    zscored = zscore_within_category(survivors)
    scored = composite_score_stage1(zscored)
    scored = _join_scheme_master(scored)
    scored = _dedupe_legacy_unit_classes(scored)
    scored = scored.with_columns(pl.lit(as_of).cast(pl.Date).alias("as_of_date"))
    return as_of, scored, excluded


def _write_stage1_outputs(scored: pl.DataFrame, as_of: date) -> dict[str, Path]:
    """Disk-write side of Stage 1: per-category CSV + parquet plus the
    consolidated ``mf_report`` (top-5 per category)."""
    out_dir = paths.shortlist_dir(as_of.isoformat()) / "stage1"
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    report_chunks: list[pl.DataFrame] = []
    for cat, grp in scored.group_by("canonical_category"):
        cat_name = cat[0]
        ranked = grp.sort("composite_score", descending=True).with_row_index(
            "rank", offset=1,
        )
        cols = [c for c in STAGE1_OUTPUT_COLS if c in ranked.columns]
        slim = ranked.select(cols)
        safe = "".join(c if c.isalnum() else "_" for c in cat_name)
        parquet_path = out_dir / f"{safe}.parquet"
        csv_path = out_dir / f"{safe}.csv"
        slim.write_parquet(parquet_path, compression="zstd")
        slim.write_csv(csv_path)
        written[cat_name] = csv_path
        report_chunks.append(slim.head(REPORT_TOP_N_PER_CATEGORY))
    if report_chunks:
        report = pl.concat(report_chunks, how="diagonal_relaxed").sort(
            ["canonical_category", "rank"]
        )
        report.write_csv(out_dir / f"{REPORT_FILENAME}.csv")
        report.write_parquet(
            out_dir / f"{REPORT_FILENAME}.parquet", compression="zstd",
        )
    return written


def rank(
    as_of: date | None = None,
    category: str | None = None,
    top_n: int | None = None,
    skip_filters: bool = False,
) -> dict[str, Path]:
    """Stage 1 only. Apply hard filters + z-score + composite_score_stage1
    and write per-category CSVs under ``<as_of>/stage1/``."""
    as_of, scored, _excluded = build_scored_stage1(
        as_of=as_of, category=category, skip_filters=skip_filters,
    )
    out_dir = paths.shortlist_dir(as_of.isoformat()) / "stage1"
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    report_chunks: list[pl.DataFrame] = []
    for cat, grp in scored.group_by("canonical_category"):
        cat_name = cat[0]
        ranked = grp.sort("composite_score", descending=True).with_row_index("rank", offset=1)
        if top_n is not None:
            ranked = ranked.head(top_n)
        cols = [c for c in STAGE1_OUTPUT_COLS if c in ranked.columns]
        slim = ranked.select(cols)
        safe = "".join(c if c.isalnum() else "_" for c in cat_name)
        parquet_path = out_dir / f"{safe}.parquet"
        csv_path = out_dir / f"{safe}.csv"
        slim.write_parquet(parquet_path, compression="zstd")
        slim.write_csv(csv_path)
        written[cat_name] = csv_path
        log.info("rank.category_written", category=cat_name, n=slim.height, path=str(csv_path))
        report_chunks.append(slim.head(REPORT_TOP_N_PER_CATEGORY))

    if report_chunks:
        report = pl.concat(report_chunks, how="diagonal_relaxed").sort(
            ["canonical_category", "rank"]
        )
        report_csv = out_dir / f"{REPORT_FILENAME}.csv"
        report_parquet = out_dir / f"{REPORT_FILENAME}.parquet"
        report.write_csv(report_csv)
        report.write_parquet(report_parquet, compression="zstd")
        log.info(
            "rank.report_written",
            n_rows=report.height,
            n_categories=len(report_chunks),
            top_n_per_category=REPORT_TOP_N_PER_CATEGORY,
            path=str(report_csv),
        )

    return written


def _top_n_per_category(scored: pl.DataFrame, n: int) -> pl.DataFrame:
    """Return the top-N rows per ``canonical_category`` from a scored frame."""
    if scored.is_empty():
        return scored
    return (
        scored.sort(
            ["canonical_category", "composite_score"],
            descending=[False, True], nulls_last=True,
        )
        .group_by("canonical_category", maintain_order=True)
        .head(n)
    )


def _build_aum_map(scored: pl.DataFrame) -> dict[str, float]:
    if scored.is_empty():
        return {}
    codes = scored["scheme_code"].unique().to_list()
    aum_map: dict[str, float] = {}
    for code in codes:
        row = q.latest_scheme_aum(code)
        if row is not None:
            aum_map[code] = float(row[1])
    return aum_map


def _latest_holdings_loader():
    def loader(scheme_code: str) -> pl.DataFrame:
        h = q.holdings_for_scheme(scheme_code)
        if h.is_empty():
            return h
        last_month = h["as_of_month"].max()
        return h.filter(pl.col("as_of_month") == last_month)
    return loader


def _history_chunk(
    df: pl.DataFrame,
    *,
    as_of: date,
    stage: int,
    included: bool,
    reason_expr: pl.Expr | None = None,
) -> pl.DataFrame:
    """Normalize one stage's frame to the rank_history schema: stamp
    as_of/stage/included(/exclusion_reason), null-fill missing columns, cast,
    and project to ``RANK_HISTORY_SCHEMA`` order."""
    if df.is_empty():
        return pl.DataFrame(schema=RANK_HISTORY_SCHEMA)
    out = df.with_columns(
        pl.lit(as_of).cast(pl.Date).alias("as_of_date"),
        pl.lit(stage, dtype=pl.Int32).alias("stage"),
        pl.lit(included).alias("included"),
        (pl.lit(None, dtype=pl.Utf8) if reason_expr is None else reason_expr)
        .alias("exclusion_reason"),
    )
    missing = [c for c in RANK_HISTORY_SCHEMA if c not in out.columns]
    if missing:
        out = out.with_columns(
            pl.lit(None, dtype=RANK_HISTORY_SCHEMA[c]).alias(c) for c in missing
        )
    return out.select(
        pl.col(c).cast(t) for c, t in RANK_HISTORY_SCHEMA.items()
    )


def build_rank_history(
    as_of: date,
    *,
    stage1_scored: pl.DataFrame,
    stage1_excluded: pl.DataFrame,
    stage2_survivors: pl.DataFrame,
    stage2_dropped: pl.DataFrame,
    stage3_final: pl.DataFrame,
    stage3_dropped: pl.DataFrame,
) -> pl.DataFrame:
    """Pure function: assemble the per-run rank_history frame from the same
    frames the stage writers put on disk, plus the dropped frames.

    One row per (stage, scheme_code). Included rows carry z-scores, composite
    score(s) and rank_in_category; excluded rows carry included=false plus an
    exclusion_reason from the contract vocabulary:

      * stage 1 drops — first failing hard filter (``INSUFFICIENT_HISTORY``,
        ``STALE_NAV``, ``FILTER:<name>`` — see filters._exclusion_reason_expr);
      * stage 2 drops — ``MISSING_CORE_METRIC:<first missing Phase 2 metric>``;
      * stage 3 drops — ``FILTER:overlap`` (pairwise-overlap iterative drop).

    Stage 3 drops are looked up in the Stage 2 survivor frame so their scores
    travel into history (stage3's own drop log only carries identity columns).
    """
    chunks: list[pl.DataFrame] = []

    if not stage1_scored.is_empty():
        s1 = stage1_scored.with_columns(
            pl.col("composite_score")
            .rank(method="ordinal", descending=True)
            .over("canonical_category")
            .cast(pl.Int32)
            .alias("rank_in_category")
        )
        chunks.append(_history_chunk(s1, as_of=as_of, stage=1, included=True))
    if not stage1_excluded.is_empty():
        chunks.append(_history_chunk(
            stage1_excluded, as_of=as_of, stage=1, included=False,
            reason_expr=pl.col("exclusion_reason"),
        ))

    if not stage2_survivors.is_empty():
        s2 = stage2_survivors
        if "stage2_rank" in s2.columns:
            s2 = s2.with_columns(
                pl.col("stage2_rank").cast(pl.Int32).alias("rank_in_category")
            )
        chunks.append(_history_chunk(s2, as_of=as_of, stage=2, included=True))
    if not stage2_dropped.is_empty():
        chunks.append(_history_chunk(
            stage2_dropped, as_of=as_of, stage=2, included=False,
            reason_expr=pl.lit("MISSING_CORE_METRIC:")
            + pl.col("missing_metrics").str.split(";").list.first(),
        ))

    if not stage3_final.is_empty():
        s3 = stage3_final
        if "stage2_rank" in s3.columns:
            s3 = s3.with_columns(
                pl.col("stage2_rank").cast(pl.Int32).alias("rank_in_category")
            )
        chunks.append(_history_chunk(s3, as_of=as_of, stage=3, included=True))
    if not stage3_dropped.is_empty():
        dropped_codes = stage3_dropped["scheme_code_dropped"].to_list()
        if not stage2_survivors.is_empty():
            d3 = stage2_survivors.filter(
                pl.col("scheme_code").is_in(dropped_codes)
            )
            if "stage2_rank" in d3.columns:
                d3 = d3.with_columns(
                    pl.col("stage2_rank").cast(pl.Int32).alias("rank_in_category")
                )
        else:
            d3 = stage3_dropped.select(
                pl.col("scheme_code_dropped").alias("scheme_code"),
                pl.col("category_dropped").alias("canonical_category"),
            )
        chunks.append(_history_chunk(
            d3, as_of=as_of, stage=3, included=False,
            reason_expr=pl.lit("FILTER:overlap"),
        ))

    nonempty = [c for c in chunks if not c.is_empty()]
    if not nonempty:
        return pl.DataFrame(schema=RANK_HISTORY_SCHEMA)
    return pl.concat(nonempty, how="vertical")


def rank_deep(
    as_of: date | None = None,
    pool_size: int = 20,
    final_size: int = 5,
    overlap_threshold_pct: float = 30.0,
    *,
    skip_phase2_compute: bool = False,
) -> dict:
    """Three-stage staged pipeline.

    Stage 1: load computed_metrics, hard-filter, z-score, ``composite_score_stage1``,
    dedupe; write ``<as_of>/stage1/``.
    Stage 2: take top ``pool_size`` per category from Stage 1, run Phase 2
    compute on just those schemes (unless ``skip_phase2_compute=True``), then
    re-z-score and re-composite with Stage 2 weights; write ``<as_of>/stage2/``.
    Stage 3: iterative pairwise-overlap drop on Stage 2 survivors; write
    ``<as_of>/stage3/``.

    At the tail, the run's stage 1/2/3 outcomes (survivors AND exclusions,
    with contract-vocabulary reasons) are persisted into the rank_history
    table in one batch — re-running the same as_of replaces that partition
    (see ``writers.persist_rank_history``).
    """
    from mfs.compute import orchestrator
    from mfs.rank import stage2 as stage2_mod
    from mfs.rank import stage3 as stage3_mod

    # Stage 1.
    as_of, scored_stage1, excluded_stage1 = build_scored_stage1(as_of=as_of)
    stage1_paths = _write_stage1_outputs(scored_stage1, as_of)
    out_dir = paths.shortlist_dir(as_of.isoformat())

    # Identify Stage 2 candidate pool: top-N per category from Stage 1.
    pool_df = _top_n_per_category(scored_stage1, n=pool_size)
    candidate_codes = pool_df["scheme_code"].unique().to_list()

    # Phase 2 compute, restricted to the candidate set (~520 schemes).
    if not skip_phase2_compute and candidate_codes:
        orchestrator.run_phase2(as_of=as_of, scheme_codes=candidate_codes)

    # Reload computed_metrics for the candidate set so Stage 2 sees the
    # filled Phase 2 columns. Join scheme_master back on for scheme_name +
    # base_fund_id since the raw computed_metrics row doesn't carry those.
    candidates_with_phase2 = q.computed_metrics_for_schemes(as_of, candidate_codes)
    if candidates_with_phase2.is_empty():
        log.warning("rank.rank_deep.no_candidates_after_phase2")
        stage2_result = {
            "stage2_dir": str(out_dir / "stage2"),
            "category_files": {},
            "dropped_file": "",
            "coverage_file": "",
            "n_survivors": 0,
            "n_dropped": 0,
            "survivors": pl.DataFrame(),
            "dropped": pl.DataFrame(),
            "coverage": pl.DataFrame(),
        }
    else:
        candidates_with_phase2 = _join_scheme_master(candidates_with_phase2)
        # Carry source_amc from holdings if present in the original Stage 1
        # scored frame (Stage 2's coverage report uses it).
        if "source_amc" in scored_stage1.columns:
            sa = scored_stage1.select(["scheme_code", "source_amc"]).unique(
                subset=["scheme_code"], keep="first"
            )
            candidates_with_phase2 = candidates_with_phase2.join(
                sa, on="scheme_code", how="left",
            )
        # Re-score with Stage 1 weights so stage2.apply_stage2 has the
        # ``composite_score_stage1`` it needs for pool ranking.
        zscored = zscore_within_category(candidates_with_phase2)
        scored_pool = composite_score_stage1(zscored)
        aum_map = _build_aum_map(scored_pool)
        stage2_result = stage2_mod.run(
            scored_pool, aum_map, out_dir,
            pool_size=pool_size, final_size=final_size,
        )

    # Stage 3.
    stage3_result = stage3_mod.run(
        stage2_result["survivors"],
        _latest_holdings_loader(),
        out_dir,
        threshold_pct=overlap_threshold_pct,
    )

    # D2 point-in-time history: persist this run's stage 1/2/3 outcomes in
    # one batch; same-as_of re-runs replace the partition.
    history = build_rank_history(
        as_of,
        stage1_scored=scored_stage1,
        stage1_excluded=excluded_stage1,
        stage2_survivors=stage2_result["survivors"],
        stage2_dropped=stage2_result["dropped"],
        stage3_final=stage3_result["final_picks"],
        stage3_dropped=stage3_result["dropped"],
    )
    w.persist_rank_history(history, as_of)

    log.info(
        "rank.rank_deep.done",
        n_stage1_categories=len(stage1_paths),
        n_stage2_survivors=stage2_result["n_survivors"],
        n_stage2_dropped=stage2_result["n_dropped"],
        n_stage3_final=stage3_result["n_final"],
        n_stage3_dropped=stage3_result["n_dropped"],
    )
    return {
        "as_of": as_of.isoformat(),
        "out_dir": str(out_dir),
        "stage1": {cat: str(p) for cat, p in stage1_paths.items()},
        "stage2": {
            "dir": stage2_result["stage2_dir"],
            "n_survivors": stage2_result["n_survivors"],
            "n_dropped": stage2_result["n_dropped"],
            "coverage_file": stage2_result["coverage_file"],
            "dropped_file": stage2_result["dropped_file"],
        },
        "stage3": {
            "dir": stage3_result["stage3_dir"],
            "n_final": stage3_result["n_final"],
            "n_dropped": stage3_result["n_dropped"],
            "dropped_file": stage3_result["dropped_file"],
            "overlap_pairs_file": stage3_result["overlap_pairs_file"],
        },
    }
