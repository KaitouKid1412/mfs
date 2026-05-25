"""Composite weighted scores for Stage 1 and Stage 2.

Stage 1 (``composite_score_stage1``) is pure Performance & Consistency over
the eight Phase 1 z-scores. No active_share, no style_drift, no soft
penalties. Every Phase 1 fund (post hard-filter) is in scope.

Stage 2 (``composite_score_stage2``) re-ranks the Stage 2 candidate pool
(top-20 per category from Stage 1 with all Phase 2 metrics non-null). It
layers active_share (positive) + style_drift (signed negative) onto the
Phase 1 base, then applies PTR and AUM-Impact soft penalties.

Per-row 5y→3y proxy substitution still applies in both stages: a scheme
with no 5y NAV history reuses its 3y z-score in the 5y slot so it isn't
structurally penalized vs older peers.
"""

from __future__ import annotations

import polars as pl

from mfs.config import get_pipeline_config

# Stage-1 keys → z-score column names.
STAGE1_WEIGHT_TO_COL = {
    "ret_3y_median": "z_ret_3y_median",
    "ret_3y_p25": "z_ret_3y_p25",
    "ret_5y_median": "z_ret_5y_median",
    "ret_5y_p25": "z_ret_5y_p25",
    "alpha_3y": "z_alpha_3y_annualized",
    "sortino_3y": "z_sortino_3y",
    "info_ratio_3y": "z_info_ratio_3y",
    "capture_efficiency": "z_capture_efficiency",
}

# Stage-2 keys → z-score column names. Includes Phase 2 metrics.
STAGE2_WEIGHT_TO_COL = {
    **STAGE1_WEIGHT_TO_COL,
    "active_share": "z_active_share_median_1y",
    "style_drift": "z_style_drift_3y",
}

# 5y → 3y proxy fallback for schemes without 5y history.
PROXY_FALLBACK: dict[str, str] = {
    "z_ret_5y_median": "z_ret_3y_median",
    "z_ret_5y_p25": "z_ret_3y_p25",
}


def _apply_proxy_fallback(df: pl.DataFrame) -> pl.DataFrame:
    # zscore_within_category emits NaN (not null) when the source metric is
    # null, because the (x - mu) / sd subtraction propagates NaN. Catch both.
    for target_col, proxy_col in PROXY_FALLBACK.items():
        if target_col in df.columns and proxy_col in df.columns:
            df = df.with_columns(
                pl.when(pl.col(target_col).is_null() | pl.col(target_col).is_nan())
                .then(pl.col(proxy_col))
                .otherwise(pl.col(target_col))
                .alias(target_col)
            )
    return df


def _weighted_sum(df: pl.DataFrame, weights: dict[str, float],
                  key_to_col: dict[str, str]) -> pl.Expr:
    expr = pl.lit(0.0)
    for key, w in weights.items():
        col = key_to_col.get(key)
        if not col or col not in df.columns:
            continue
        base = (
            pl.when(pl.col(col).is_null() | pl.col(col).is_nan())
            .then(0.0)
            .otherwise(pl.col(col))
        )
        expr = expr + base * float(w)
    return expr


def composite_score_stage1(df: pl.DataFrame) -> pl.DataFrame:
    """Phase-1-only composite. Eight z-scores, no penalties, no
    redistribution. Output column ``composite_score``."""
    cfg = get_pipeline_config()
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("composite_score"))
    df = _apply_proxy_fallback(df)
    expr = _weighted_sum(df, cfg.composite_weights_stage1, STAGE1_WEIGHT_TO_COL)
    return df.with_columns(expr.alias("composite_score"))


def composite_score_stage2(df: pl.DataFrame) -> pl.DataFrame:
    """Stage-2 composite. Phase 1 + active_share + style_drift, then PTR
    and AUM-Impact soft penalties. Output column ``composite_score``
    (overwrites whatever Stage 1 wrote — Stage 2 callers should preserve
    the Stage 1 number under a different name if they need both)."""
    cfg = get_pipeline_config()
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("composite_score"))
    df = _apply_proxy_fallback(df)
    expr = _weighted_sum(df, cfg.composite_weights_stage2, STAGE2_WEIGHT_TO_COL)
    expr = _apply_soft_penalties(expr, df, cfg)
    return df.with_columns(expr.alias("composite_score"))


def _apply_soft_penalties(expr: pl.Expr, df: pl.DataFrame, cfg) -> pl.Expr:
    """Subtract configured soft-penalty terms from the composite expression."""
    pens = cfg.soft_penalties

    # PTR: linear ramp from 0 at threshold up to max_penalty, capped.
    pcfg = pens.ptr
    if pcfg.enabled and "ptr_latest" in df.columns:
        threshold = float(pcfg.threshold)
        max_pen = float(pcfg.max_penalty)
        if threshold > 0 and max_pen != 0:
            excess = (pl.col("ptr_latest") - pl.lit(threshold)).clip(lower_bound=0.0)
            ramp = excess / pl.lit(threshold)
            penalty = (ramp * pl.lit(max_pen)).clip(upper_bound=max_pen)
            penalty = (
                pl.when(pl.col("ptr_latest").is_null())
                .then(0.0)
                .otherwise(penalty)
            )
            expr = expr - penalty

    # AUM Impact Cost: same shape, days scale.
    acfg = pens.aum_impact_cost
    if acfg.enabled and "aum_impact_cost_days" in df.columns:
        threshold = float(acfg.threshold_days)
        max_pen = float(acfg.max_penalty)
        if threshold > 0 and max_pen != 0:
            excess = (pl.col("aum_impact_cost_days") - pl.lit(threshold)).clip(lower_bound=0.0)
            ramp = excess / pl.lit(threshold)
            penalty = (ramp * pl.lit(max_pen)).clip(upper_bound=max_pen)
            penalty = (
                pl.when(pl.col("aum_impact_cost_days").is_null())
                .then(0.0)
                .otherwise(penalty)
            )
            expr = expr - penalty

    return expr
