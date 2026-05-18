"""Hard filters applied before z-scoring.

Filters that depend on optional data short-circuit to "pass with warning" rather than fail
— this is what keeps v1 useful while some inputs are missing.
"""

from __future__ import annotations

import polars as pl

from mfs.config import get_pipeline_config, get_thresholds


def apply_hard_filters(metrics: pl.DataFrame) -> pl.DataFrame:
    """Drop rows that fail any threshold; return survivors.

    Caller is responsible for joining scheme_master onto metrics first if plan_type/option_type
    filtering is needed (those are already enforced upstream when building the metrics set).
    """
    cfg = get_pipeline_config()
    thr = get_thresholds()
    beta_bands = thr.get("beta_bands", {})
    rankable = set(thr.get("rankable_categories", []))

    if metrics.is_empty():
        return metrics

    df = metrics
    df = df.filter(pl.col("data_quality_flag") != "INSUFFICIENT_HISTORY")
    df = df.filter(pl.col("data_quality_flag") != "POOR")
    df = df.filter(pl.col("canonical_category").is_in(list(rankable)))
    df = df.filter(pl.col("capture_efficiency").is_not_null())
    df = df.filter(pl.col("capture_efficiency") > cfg.filters.capture_efficiency_min)
    df = df.filter(pl.col("info_ratio_3y").is_not_null())
    df = df.filter(pl.col("info_ratio_3y") > cfg.filters.info_ratio_3y_min)
    df = df.filter(pl.col("r_squared_3y").is_not_null())
    df = df.filter(pl.col("r_squared_3y") >= cfg.filters.r_squared_band[0])
    df = df.filter(pl.col("r_squared_3y") <= cfg.filters.r_squared_band[1])

    if df.is_empty():
        return df

    # Beta band per category
    def _in_beta_band(cat: str | None, b: float | None) -> bool:
        if not cat or b is None:
            return False
        band = beta_bands.get(cat)
        if not band:
            return True
        lo, hi = band
        return lo <= b <= hi

    keep_idx = [
        _in_beta_band(r["canonical_category"], r["beta_3y"]) for r in df.iter_rows(named=True)
    ]
    if not keep_idx:
        return df.head(0)
    return df.filter(pl.Series("keep", keep_idx, dtype=pl.Boolean))
