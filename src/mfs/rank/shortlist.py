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
        return metrics.with_columns(pl.lit(None, dtype=pl.Utf8).alias("scheme_name"))
    sm = pl.read_parquet(sm_path).select(["scheme_code", "scheme_name"])
    return metrics.join(sm, on="scheme_code", how="left")


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
