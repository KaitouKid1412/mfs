"""Within-category z-score normalization with winsorization at ±3σ."""

from __future__ import annotations

import numpy as np
import polars as pl

Z_METRICS = (
    "ret_3y_median",
    "ret_3y_p25",
    "ret_5y_median",
    "ret_5y_p25",
    "alpha_3y_annualized",
    "sortino_3y",
    "info_ratio_3y",
    "capture_efficiency",
    # Phase 2: z-scored within category so the composite weight applies on the
    # same scale as the existing metrics. Schemes missing these emit NaN z-scores,
    # which score.py handles (active_share triggers pro-rata redistribution;
    # style_drift NaN contributes zero to the penalty).
    "active_share_median_1y",
    "style_drift_3y",
)


def _zscore_one(arr: np.ndarray) -> np.ndarray:
    mu = np.nanmean(arr)
    sd = np.nanstd(arr, ddof=1)
    if sd == 0 or np.isnan(sd):
        return np.full_like(arr, 0.0, dtype=float)
    z = (arr - mu) / sd
    # Winsorize at ±3σ
    return np.clip(z, -3.0, 3.0)


def zscore_within_category(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty():
        return df
    # Skip metrics that aren't materialized in the input frame. Phase 2 columns
    # (active_share_median_1y, style_drift_3y) may be absent on early test
    # fixtures or during Phase 2.0 when the ingestion stages haven't shipped yet.
    present = [m for m in Z_METRICS if m in df.columns]
    if not present:
        return df
    result_cols: dict[str, list[float]] = {f"z_{m}": [0.0] * df.height for m in present}
    cats = df["canonical_category"].to_list()
    for metric in present:
        vals = df[metric].to_numpy().astype(float)
        z_out = np.full(df.height, np.nan, dtype=float)
        # Group by category
        for cat in set(cats):
            idx = np.array([i for i, c in enumerate(cats) if c == cat])
            sub = vals[idx]
            z = _zscore_one(sub)
            z_out[idx] = z
        result_cols[f"z_{metric}"] = z_out.tolist()
    return df.with_columns([pl.Series(name=k, values=v) for k, v in result_cols.items()])
