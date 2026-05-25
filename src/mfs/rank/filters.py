"""Pre-rank filters — a floor, not a tight band.

Data-quality filters (mandatory): INSUFFICIENT_HISTORY and POOR schemes have
no usable metrics; schemes outside `rankable_categories` have no benchmark.

Metric filters (relaxed floors): drop only catastrophically broken funds. The
old strict thresholds (capture > 1.0, IR > 0.2, R² in [0.70, 0.98], tight
per-category beta bands) were dropping otherwise-interesting funds — e.g.
HDFC Flexi Cap with strong alpha (t=3.0) but capture 0.97 and IR 0.11. The
composite_score still uses capture_efficiency and info_ratio_3y as weighted
inputs, so surviving-but-weak funds rank lower naturally.

Thresholds live in code (not configurable) — these are deliberately permissive
defaults to keep schemes visible. Tighter post-hoc curation happens when the
user picks the top-5 per category by eye.
"""

from __future__ import annotations

import polars as pl

from mfs.config import get_thresholds


# Floor values — schemes failing any of these are too broken to be informative.
CAPTURE_EFFICIENCY_FLOOR = 0.50      # was 1.00
INFO_RATIO_FLOOR = -1.00             # was 0.20
R_SQUARED_BAND = (0.40, 1.00)        # was (0.70, 0.98); upper bound removed for index funds
BETA_BAND_GENERIC = (0.30, 1.70)     # was per-category tight bands

# Phase 2.3.F: SEBI stress test hard filter. Applies ONLY to Mid Cap and
# Small Cap funds (the categories where SEBI's March 2024 mandate applies);
# pass-through for everything else. Drop schemes where the most recent
# days-to-liquidate-50 disclosure exceeds the threshold.
# Stress test filter removed when manager-tenure and stress-test extraction
# were retired (user verifies these manually for Stage 2 survivors).


def apply_hard_filters(metrics: pl.DataFrame) -> pl.DataFrame:
    """Drop schemes that are unrankable or catastrophically broken on the
    quality metrics. Survivors still get differentiated by composite_score."""
    thr = get_thresholds()
    rankable = set(thr.get("rankable_categories", []))

    if metrics.is_empty():
        return metrics

    df = metrics
    df = df.filter(pl.col("data_quality_flag") != "INSUFFICIENT_HISTORY")
    df = df.filter(pl.col("data_quality_flag") != "POOR")
    df = df.filter(pl.col("canonical_category").is_in(list(rankable)))

    # Relaxed metric floors. Null-tolerant: a scheme with null capture (e.g.
    # benchmark went flat) is kept; the floor only drops measurably bad funds.
    df = df.filter(
        pl.col("capture_efficiency").is_null()
        | (pl.col("capture_efficiency") > CAPTURE_EFFICIENCY_FLOOR)
    )
    df = df.filter(
        pl.col("info_ratio_3y").is_null()
        | (pl.col("info_ratio_3y") > INFO_RATIO_FLOOR)
    )
    df = df.filter(
        pl.col("r_squared_3y").is_null()
        | (
            (pl.col("r_squared_3y") >= R_SQUARED_BAND[0])
            & (pl.col("r_squared_3y") <= R_SQUARED_BAND[1])
        )
    )
    df = df.filter(
        pl.col("beta_3y").is_null()
        | (
            (pl.col("beta_3y") >= BETA_BAND_GENERIC[0])
            & (pl.col("beta_3y") <= BETA_BAND_GENERIC[1])
        )
    )
    return df
