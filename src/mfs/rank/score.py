"""Composite weighted score over per-category z-scores."""

from __future__ import annotations

import polars as pl

from mfs.config import get_pipeline_config

WEIGHT_TO_COL = {
    "ret_3y_median": "z_ret_3y_median",
    "ret_3y_p25": "z_ret_3y_p25",
    "alpha_3y": "z_alpha_3y_annualized",
    "sortino_3y": "z_sortino_3y",
    "info_ratio_3y": "z_info_ratio_3y",
    "capture_efficiency": "z_capture_efficiency",
}


def composite_score(df: pl.DataFrame) -> pl.DataFrame:
    cfg = get_pipeline_config()
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("composite_score"))
    weights = cfg.composite_weights
    expr = pl.lit(0.0)
    for key, w in weights.items():
        col = WEIGHT_TO_COL.get(key)
        if not col or col not in df.columns:
            continue
        expr = expr + pl.col(col).fill_null(0.0) * w
    return df.with_columns(expr.alias("composite_score"))
