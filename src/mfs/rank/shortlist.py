"""Top-level ranker: filters → zscore → score → write shortlist per category."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from mfs import paths
from mfs.config import get_pipeline_config
from mfs.io.parquet import read_partition
from mfs.rank.filters import apply_hard_filters
from mfs.rank.score import composite_score
from mfs.rank.zscore import zscore_within_category
from mfs.utils.logging import get_logger

log = get_logger(__name__)

OUTPUT_COLS = [
    "as_of_date",
    "canonical_category",
    "rank",
    "scheme_code",
    "scheme_name",
    "composite_score",
    "z_ret_3y_median",
    "z_ret_3y_p25",
    "z_alpha_3y_annualized",
    "z_sortino_3y",
    "z_info_ratio_3y",
    "z_capture_efficiency",
    "ret_3y_median",
    "ret_3y_p25",
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


def _join_scheme_master(metrics: pl.DataFrame) -> pl.DataFrame:
    sm_path = paths.scheme_master_file()
    if not sm_path.exists():
        return metrics.with_columns(
            pl.lit(None, dtype=pl.Utf8).alias("scheme_name"),
            pl.lit(None, dtype=pl.Utf8).alias("base_fund_id"),
        )
    sm = pl.read_parquet(sm_path).select(["scheme_code", "scheme_name", "base_fund_id"])
    return metrics.join(sm, on="scheme_code", how="left")


def _dedupe_legacy_unit_classes(scored: pl.DataFrame) -> pl.DataFrame:
    """Collapse AMFI's legacy 'Bonus Option' duplicates within each category.

    AMFI lists Growth Option and Bonus Option of the same underlying portfolio under
    separate scheme_codes with distinct ISINs. Both pass our Direct+Growth filter
    (the scheme_name contains "Growth Plan"), produce identical NAV histories, and
    so produce identical metrics. An investor today only buys the Growth Option;
    the Bonus Option is a legacy listing. Detect by stripping a trailing `_bonus`
    from base_fund_id, and keep the row with the highest composite_score (ties
    broken by preferring the non-`_bonus` row).
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
    # Sort priority: non-legacy first (_is_legacy ascending), then highest score
    # within that class. This guarantees the Bonus Option is dropped whenever a
    # Growth Option sibling exists, regardless of float-noise rank inversions.
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


def rank(
    as_of: date | None = None,
    category: str | None = None,
    top_n: int | None = None,
    skip_filters: bool = False,
) -> dict[str, Path]:
    cfg = get_pipeline_config()
    as_of = as_of or _latest_metrics_date()
    if as_of is None:
        raise RuntimeError("No computed_metrics available; run `mfs compute metrics` first")
    top_n = top_n or cfg.output.top_n_per_category

    metrics = read_partition(paths.computed_metrics_dataset(), as_of.isoformat(), "as_of_date")
    if metrics.is_empty():
        raise RuntimeError(f"No metrics partition for as_of={as_of.isoformat()}")

    if skip_filters:
        # Debug path: keep all schemes with a canonical_category and required metrics non-null
        survivors = metrics.filter(pl.col("canonical_category").is_not_null())
        survivors = survivors.filter(pl.col("alpha_3y_annualized").is_not_null())
    else:
        survivors = apply_hard_filters(metrics)
    if category:
        survivors = survivors.filter(pl.col("canonical_category") == category)

    zscored = zscore_within_category(survivors)
    scored = composite_score(zscored)
    scored = _join_scheme_master(scored)
    scored = _dedupe_legacy_unit_classes(scored)
    scored = scored.with_columns(pl.lit(as_of).cast(pl.Date).alias("as_of_date"))

    out_dir = paths.shortlist_dir(as_of.isoformat())
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for cat, grp in scored.group_by("canonical_category"):
        cat_name = cat[0]
        ranked = grp.sort("composite_score", descending=True).with_row_index("rank", offset=1)
        ranked = ranked.head(top_n)
        # Ensure we only project the OUTPUT_COLS that actually exist
        cols = [c for c in OUTPUT_COLS if c in ranked.columns]
        slim = ranked.select(cols)
        safe = "".join(c if c.isalnum() else "_" for c in cat_name)
        parquet_path = out_dir / f"{safe}.parquet"
        csv_path = out_dir / f"{safe}.csv"
        slim.write_parquet(parquet_path, compression="zstd")
        slim.write_csv(csv_path)
        written[cat_name] = csv_path
        log.info("rank.category_written", category=cat_name, n=slim.height, path=str(csv_path))

    return written


def _latest_metrics_date() -> date | None:
    root = paths.computed_metrics_dataset()
    if not root.exists():
        return None
    parts = sorted(root.glob("as_of_date=*"))
    if not parts:
        return None
    last = parts[-1].name.split("=", 1)[1]
    try:
        return date.fromisoformat(last)
    except ValueError:
        return None
