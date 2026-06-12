"""Within-category z-score normalization with winsorization at ±3σ."""

from __future__ import annotations

from collections.abc import Sequence

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
    # same scale as the existing metrics. Schemes missing these emit NaN
    # z-scores, which score.composite_score_stage2 handles (A2-4/D3): a NaN
    # positive input (active_share) contributes zero and its weight is
    # renormalized per-row across the present positive inputs; a NaN
    # style_drift (negative weight) contributes zero, never renormalized.
    # Either null additionally draws the fixed missing-disclosure penalty
    # when the metric is live in the row's category pool.
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
    clipped: np.ndarray = np.clip(z, -3.0, 3.0)
    return clipped


def zscore_within_category(
    df: pl.DataFrame, metrics: Sequence[str] | None = None,
) -> pl.DataFrame:
    """Z-score ``metrics`` (default: all ``Z_METRICS``) within each category.

    ``metrics`` restricts which columns are (re)computed — A2-5: Stage 2
    carries Stage 1's full-universe Phase-1 z-scores and re-z-scores only the
    pool-scoped Phase 2 metrics, so it must not clobber the carried columns.
    """
    if df.is_empty():
        return df
    targets = Z_METRICS if metrics is None else tuple(metrics)
    # Skip metrics that aren't materialized in the input frame. Phase 2 columns
    # (active_share_median_1y, style_drift_3y) may be absent on early test
    # fixtures or during Phase 2.0 when the ingestion stages haven't shipped yet.
    present = [m for m in targets if m in df.columns]
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
