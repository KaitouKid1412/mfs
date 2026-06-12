"""Composite weighted scores for Stage 1 and Stage 2.

Stage 1 (``composite_score_stage1``) is pure Performance & Consistency over
the eight Phase 1 z-scores. No active_share, no style_drift, no soft
penalties. Every Phase 1 fund (post hard-filter) is in scope.

Stage 2 (``composite_score_stage2``) re-ranks the Stage 2 candidate pool
(top-20 per category from Stage 1; D3 — funds missing disclosure metrics
stay in the pool, soft-neutral). It layers active_share (positive) +
style_drift (signed negative) onto the Phase 1 base with per-row
positive-weight renormalization (a missing positive z-input redistributes
its weight across the row's present positive inputs), then applies PTR,
AUM-Impact and missing-disclosure soft penalties.

Per-row 5y→3y proxy substitution still applies in both stages: a scheme
with no 5y NAV history reuses its 3y z-score in the 5y slot so it isn't
structurally penalized vs older peers.
"""

from __future__ import annotations

import math

import polars as pl

from mfs.config import PipelineConfig, get_pipeline_config

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

# D3 (A2-4): disclosure metrics whose absence draws the fixed
# missing-disclosure penalty — but only when the metric is *live* in the
# row's category pool (non-null for >= LIVENESS_MIN of the pool), so a
# universe-dead metric (active_share today) doesn't uniformly penalize
# everyone. active_share is listed here so it joins the penalty set
# automatically the moment it activates (~2026-07 via D9); until then its
# pool liveness is 0 and only the weight renormalization applies. Mirrors
# stage2.DISCLOSURE_METRICS (kept separate to avoid a circular import; a
# test asserts they stay equal).
MISSING_DISCLOSURE_PENALTY_METRICS = (
    "ptr_latest",
    "style_drift_3y",
    "aum_impact_cost_days",
    "active_share_median_1y",
)
MISSING_DISCLOSURE_LIVENESS_MIN = 0.5


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
                  key_to_col: dict[str, str], *,
                  renormalize: bool = False) -> pl.Expr:
    """Weighted sum of z-score columns; null/NaN inputs contribute 0.

    ``renormalize=True`` (Stage 2 / D3): per-row positive-weight
    renormalization — each present positive-weight term is scaled by
    (sum of all positive weights / sum of the row's present positive
    weights), so a missing positive input (e.g. active_share) redistributes
    its weight instead of silently contributing a zero. Negative weights
    (style_drift) are never renormalized and contribute 0 when missing.
    Stage 1 keeps the default ``renormalize=False`` and is unchanged.
    """
    scale: pl.Expr = pl.lit(1.0)
    if renormalize:
        pos = [
            (float(w), key_to_col[key])
            for key, w in weights.items()
            if w > 0 and key_to_col.get(key) and key_to_col[key] in df.columns
        ]
        sum_all_pos = sum(w for w, _ in pos)
        present_pos = pl.lit(0.0)
        for w, col in pos:
            present_pos = present_pos + (
                pl.when(pl.col(col).is_null() | pl.col(col).is_nan())
                .then(0.0)
                .otherwise(w)
            )
        scale = (
            pl.when(present_pos > 0)
            .then(pl.lit(sum_all_pos) / present_pos)
            .otherwise(1.0)
        )

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
        term = base * float(w)
        if renormalize and w > 0:
            term = term * scale
        expr = expr + term
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
    """Stage-2 composite. Phase 1 + active_share + style_drift (positive
    weights renormalized per-row over the present inputs — D3), then PTR,
    AUM-Impact and missing-disclosure soft penalties. Output column
    ``composite_score`` (overwrites whatever Stage 1 wrote — Stage 2 callers
    should preserve the Stage 1 number under a different name if they need
    both)."""
    cfg = get_pipeline_config()
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("composite_score"))
    df = _apply_proxy_fallback(df)
    expr = _weighted_sum(
        df, cfg.composite_weights_stage2, STAGE2_WEIGHT_TO_COL, renormalize=True,
    )
    expr = _apply_soft_penalties(expr, df, cfg)
    return df.with_columns(expr.alias("composite_score"))


def _apply_soft_penalties(expr: pl.Expr, df: pl.DataFrame, cfg: PipelineConfig) -> pl.Expr:
    """Subtract configured soft-penalty terms from the composite expression."""
    pens = cfg.soft_penalties

    # PTR: linear ramp from 0 at threshold up to max_penalty, capped.
    # A2-10: structurally high-turnover hybrid categories
    # (ptr_penalty_exempt_categories — arbitrage mechanics, not churn) are
    # exempt from the ramp; a *missing* PTR there still draws the
    # missing-disclosure penalty below.
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
            exempt = list(pens.ptr_penalty_exempt_categories)
            if exempt and "canonical_category" in df.columns:
                penalty = (
                    pl.when(pl.col("canonical_category").is_in(exempt))
                    .then(0.0)
                    .otherwise(penalty)
                )
            expr = expr - penalty

    # AUM Impact Cost (A2-6): log-scale ramp — the old linear ramp saturated
    # at ~10 days while the Small Cap survivor median sat at 79, flattening
    # 63/115 funds onto the cap. penalty = max_penalty *
    # clip(ln(days/threshold)/ln(saturation/threshold), 0, 1) for
    # days > threshold, else 0; null days → 0 (D3's missing-disclosure
    # penalty covers the null).
    acfg = pens.aum_impact_cost
    if acfg.enabled and "aum_impact_cost_days" in df.columns:
        threshold = float(acfg.threshold_days)
        saturation = float(acfg.saturation_days)
        max_pen = float(acfg.max_penalty)
        if threshold > 0 and saturation > threshold and max_pen != 0:
            days = pl.col("aum_impact_cost_days").cast(pl.Float64)
            denom = math.log(saturation / threshold)
            ramp = ((days / pl.lit(threshold)).log() / pl.lit(denom)).clip(
                lower_bound=0.0, upper_bound=1.0,
            )
            penalty = (
                pl.when(days.is_null() | (days <= threshold))
                .then(0.0)
                .otherwise(ramp * pl.lit(max_pen))
            )
            expr = expr - penalty

    # D3 (A2-4): fixed missing-disclosure penalty, once per missing *live*
    # disclosure metric. Liveness is evaluated within the row's category pool
    # (the frame Stage 2 passes is the per-category pool; .over() handles
    # multi-category frames): a metric null for > half the pool is dead there
    # and penalizing everyone for it would be meaningless. Calibrated to the
    # median non-zero PTR penalty among stage-2 survivors (pipeline.yaml) so
    # missing scores like average-bad, never better than disclosed-bad.
    mcfg = pens.missing_disclosure
    if mcfg.enabled and float(mcfg.penalty) != 0:
        for metric in MISSING_DISCLOSURE_PENALTY_METRICS:
            if metric not in df.columns:
                continue
            val = pl.col(metric).cast(pl.Float64)
            missing = val.is_null() | val.is_nan()
            present_frac = (~missing).cast(pl.Float64).mean()
            if "canonical_category" in df.columns:
                present_frac = present_frac.over("canonical_category")
            live = present_frac >= MISSING_DISCLOSURE_LIVENESS_MIN
            expr = expr - (
                pl.when(missing & live)
                .then(float(mcfg.penalty))
                .otherwise(0.0)
            )

    return expr
