"""Within-category z-score normalization with winsorization at ±3σ."""

from __future__ import annotations

import numpy as np
import polars as pl

Z_METRICS = (
    "ret_3y_median",
    "ret_3y_p25",
    "alpha_3y_annualized",
    "sortino_3y",
    "info_ratio_3y",
    "capture_efficiency",
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
    result_cols: dict[str, list[float]] = {f"z_{m}": [0.0] * df.height for m in Z_METRICS}
    cats = df["canonical_category"].to_list()
    for metric in Z_METRICS:
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
