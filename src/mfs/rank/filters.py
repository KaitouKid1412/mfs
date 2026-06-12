"""Pre-rank filters — a floor, not a tight band.

Data-quality filters (mandatory): INSUFFICIENT_HISTORY and POOR schemes have
no usable metrics; STALE schemes (A1-3: last NAV lags the run's as_of beyond
quality.max_scheme_nav_lag_bdays trading days — dead/merged/suspended funds)
have metrics describing a portfolio that no longer exists, and are excluded
with exclusion_reason STALE_NAV; schemes outside `rankable_categories` have
no benchmark.

Metric filters (relaxed floors): drop only catastrophically broken funds. The
old strict thresholds (capture > 1.0, IR > 0.2, R² in [0.70, 0.98], tight
per-category beta bands) were dropping otherwise-interesting funds — e.g.
HDFC Flexi Cap with strong alpha (t=3.0) but capture 0.97 and IR 0.11. The
composite_score still uses capture_efficiency and info_ratio_3y as weighted
inputs, so surviving-but-weak funds rank lower naturally.

D2 core-metric gate (``split_core_complete``, applied after the hard
filters): funds missing any required core Stage-1 metric are hard-dropped
into the "excluded — insufficient data" output (stage1/excluded.csv) with
exclusion_reason ``MISSING_CORE_METRIC:<name>`` — a null must never score
as category-average.

Thresholds live in configs/pipeline.yaml under ``filters:`` (A2-1 single
source of truth, read via ``get_pipeline_config``) — deliberately permissive
floors to keep schemes visible. Tighter post-hoc curation happens when the
user picks the top-5 per category by eye.
"""

from __future__ import annotations

from typing import Literal, overload

import polars as pl

from mfs.config import FiltersConfig, get_pipeline_config, get_thresholds

# The Phase 2.3.F SEBI stress-test hard filter was removed when manager-tenure
# and stress-test extraction were retired (user verifies these manually for
# Stage 2 survivors).


def _exclusion_reason_expr(rankable: set[str], fcfg: FiltersConfig) -> pl.Expr:
    """First-failing-filter reason, in the exact order ``apply_hard_filters``
    applies its filters. Vocabulary (cross-workstream contract, consumed by
    rank_history / E5 report sections):

        INSUFFICIENT_HISTORY | MISSING_CORE_METRIC:<name> | STALE_NAV
        | FILTER:<name>

    STALE_NAV maps the (A1-3) per-scheme staleness data_quality_flag value
    ``STALE`` (set by the compute orchestrator when a scheme's last NAV lags
    the run's as_of beyond quality.max_scheme_nav_lag_bdays trading days).
    NULL means the row passed every filter.
    """
    conditions: list[tuple[pl.Expr, str]] = [
        (pl.col("data_quality_flag") == "INSUFFICIENT_HISTORY",
         "INSUFFICIENT_HISTORY"),
        (pl.col("data_quality_flag") == "STALE", "STALE_NAV"),
        (pl.col("data_quality_flag") == "POOR", "FILTER:data_quality"),
        (~pl.col("canonical_category").is_in(list(rankable)).fill_null(False),
         "FILTER:rankable_category"),
        (pl.col("capture_efficiency").is_not_null()
         & (pl.col("capture_efficiency") <= fcfg.capture_efficiency_min),
         "FILTER:capture_efficiency"),
        (pl.col("info_ratio_3y").is_not_null()
         & (pl.col("info_ratio_3y") <= fcfg.info_ratio_3y_min),
         "FILTER:info_ratio"),
        (pl.col("r_squared_3y").is_not_null()
         & ((pl.col("r_squared_3y") < fcfg.r_squared_band[0])
            | (pl.col("r_squared_3y") > fcfg.r_squared_band[1])),
         "FILTER:r_squared"),
        (pl.col("beta_3y").is_not_null()
         & ((pl.col("beta_3y") < fcfg.beta_band[0])
            | (pl.col("beta_3y") > fcfg.beta_band[1])),
         "FILTER:beta"),
    ]
    expr: pl.Expr = pl.lit(None, dtype=pl.Utf8)
    for cond, reason in reversed(conditions):
        expr = pl.when(cond).then(pl.lit(reason)).otherwise(expr)
    return expr


@overload
def apply_hard_filters(
    metrics: pl.DataFrame, *, with_reasons: Literal[True],
) -> tuple[pl.DataFrame, pl.DataFrame]: ...


@overload
def apply_hard_filters(
    metrics: pl.DataFrame, *, with_reasons: Literal[False] = ...,
) -> pl.DataFrame: ...


def apply_hard_filters(
    metrics: pl.DataFrame, *, with_reasons: bool = False,
) -> pl.DataFrame | tuple[pl.DataFrame, pl.DataFrame]:
    """Drop schemes that are unrankable or catastrophically broken on the
    quality metrics. Survivors still get differentiated by composite_score.

    With ``with_reasons=True`` returns ``(survivors, excluded)`` where
    ``excluded`` is the dropped rows plus an ``exclusion_reason`` column —
    the first failing filter in application order, using the contract
    vocabulary (see ``_exclusion_reason_expr``). Survivors are byte-identical
    to the default return."""
    thr = get_thresholds()
    rankable = set(thr.get("rankable_categories", []))
    fcfg = get_pipeline_config().filters

    if metrics.is_empty():
        return (metrics, metrics) if with_reasons else metrics

    df = metrics
    df = df.filter(pl.col("data_quality_flag") != "INSUFFICIENT_HISTORY")
    df = df.filter(pl.col("data_quality_flag") != "STALE")
    df = df.filter(pl.col("data_quality_flag") != "POOR")
    df = df.filter(pl.col("canonical_category").is_in(list(rankable)))

    # Relaxed metric floors. Null-tolerant: a scheme with null capture (e.g.
    # benchmark went flat) is kept; the floor only drops measurably bad funds.
    df = df.filter(
        pl.col("capture_efficiency").is_null()
        | (pl.col("capture_efficiency") > fcfg.capture_efficiency_min)
    )
    df = df.filter(
        pl.col("info_ratio_3y").is_null()
        | (pl.col("info_ratio_3y") > fcfg.info_ratio_3y_min)
    )
    df = df.filter(
        pl.col("r_squared_3y").is_null()
        | (
            (pl.col("r_squared_3y") >= fcfg.r_squared_band[0])
            & (pl.col("r_squared_3y") <= fcfg.r_squared_band[1])
        )
    )
    df = df.filter(
        pl.col("beta_3y").is_null()
        | (
            (pl.col("beta_3y") >= fcfg.beta_band[0])
            & (pl.col("beta_3y") <= fcfg.beta_band[1])
        )
    )
    if not with_reasons:
        return df
    excluded = metrics.join(
        df.select("scheme_code"), on="scheme_code", how="anti",
    ).with_columns(
        _exclusion_reason_expr(rankable, fcfg).alias("exclusion_reason")
    )
    return df, excluded


# ---------------------------------------------------------------------------
# D2 core-metric gate (A2-2)
# ---------------------------------------------------------------------------

# Core Stage-1 metrics: a fund missing ANY of these is hard-dropped before
# z-scoring (locked decision D2 — nulls must never score as category-average).
# The 5y pair (ret_5y_median / ret_5y_p25) is deliberately NOT core: the
# 5y→3y proxy fallback (score.PROXY_FALLBACK) is the designed path for
# sub-5y funds.
#
# ORDERING NOTE (baked-in interaction): this gate must run AFTER A1-2/D1
# (signed alpha, median over ALL windows). Pre-D1, the t-stat censor nulled
# alpha for ~54 otherwise-good funds and this gate would have excluded them
# all; post-D1, null alpha only means zero regressable windows (short-history
# or unregressable funds) — exactly what D2 intends to exclude.
CORE_STAGE1_METRICS = (
    "ret_3y_median",
    "ret_3y_p25",
    "alpha_3y_annualized",
    "sortino_3y",
    "info_ratio_3y",
    "capture_efficiency",
)


def _core_missing_expr(metric: str, df: pl.DataFrame) -> pl.Expr:
    """Missing = null OR NaN. NaN reaches the rank layer via the zero-NAV
    poisoning path (alignment.py); null-tolerant comparisons must not let it
    through. A column absent from the frame counts as missing for every row."""
    if metric not in df.columns:
        return pl.lit(True)
    return pl.col(metric).is_null() | pl.col(metric).cast(pl.Float64).is_nan()


def split_core_complete(
    df: pl.DataFrame,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Split a hard-filter-surviving frame into ``(complete, excluded)`` on
    the D2 core-metric gate.

    ``complete`` rows have every ``CORE_STAGE1_METRICS`` value present and
    finite; ``excluded`` rows carry two extra columns:

      * ``missing_core_metrics`` — ';'-joined names of every missing core
        metric (declaration order);
      * ``exclusion_reason`` — ``MISSING_CORE_METRIC:<first missing>``
        (contract vocabulary, consumed by rank_history / stage1/excluded.csv).
    """
    if df.is_empty():
        return df, df
    flagged = df.with_columns(
        pl.concat_list([
            pl.when(_core_missing_expr(m, df))
            .then(pl.lit(m))
            .otherwise(pl.lit(None, dtype=pl.Utf8))
            for m in CORE_STAGE1_METRICS
        ])
        .list.drop_nulls()
        .alias("_missing_core")
    )
    complete = (
        flagged.filter(pl.col("_missing_core").list.len() == 0)
        .drop("_missing_core")
    )
    excluded = (
        flagged.filter(pl.col("_missing_core").list.len() > 0)
        .with_columns(
            pl.col("_missing_core").list.join(";").alias("missing_core_metrics"),
            (pl.lit("MISSING_CORE_METRIC:") + pl.col("_missing_core").list.first())
            .alias("exclusion_reason"),
        )
        .drop("_missing_core")
    )
    return complete, excluded
