"""Run every metric module over every active scheme; persist computed_metrics partition.

For each scheme with a benchmark mapping and ≥3 years of NAV history, we produce one
row in metrics/computed_metrics/as_of_date=YYYY-MM-DD/data.parquet.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl

from mfs import paths
from mfs.compute import alignment, alpha, capture, info_ratio, returns, sortino
from mfs.config import get_pipeline_config
from mfs.freshness import check_freshness
from mfs.io.parquet import read_dataset, write_partition
from mfs.utils.logging import get_logger

log = get_logger(__name__)


def _data_quality_flag(aligned: pl.DataFrame, min_years: float = 3.0) -> str:
    if aligned.is_empty():
        return "INSUFFICIENT_HISTORY"
    df = aligned.select(["date", "nav"]).drop_nulls()
    if df.is_empty():
        return "INSUFFICIENT_HISTORY"
    span_days = (df["date"].max() - df["date"].min()).days
    if span_days < int(365.25 * min_years):
        return "INSUFFICIENT_HISTORY"
    # Check missingness in trailing 3y window
    end = df["date"].max()
    start = end - timedelta(days=int(365.25 * 3))
    cal = alignment.master_calendar().filter(
        (pl.col("date") >= start) & (pl.col("date") <= end)
    )
    if cal.is_empty():
        return "POOR"
    present = df.filter((pl.col("date") >= start) & (pl.col("date") <= end))
    miss = 1.0 - (present.height / max(cal.height, 1))
    if miss > 0.05:
        return "POOR"
    return "GOOD"


def compute_for_scheme(scheme_code: str, benchmark_ticker: str, canonical_category: str | None,
                       as_of: date, step: str = "1w") -> dict:
    aligned = alignment.align_scheme(scheme_code, benchmark_ticker)
    flag = _data_quality_flag(aligned)
    row: dict = {
        "scheme_code": scheme_code,
        "canonical_category": canonical_category,
        "benchmark_ticker": benchmark_ticker,
        "ret_3y_median": None,
        "ret_3y_p25": None,
        "ret_5y_median": None,
        "ret_5y_p25": None,
        "alpha_3y_annualized": None,
        "alpha_3y_tstat": None,
        "sortino_3y": None,
        "sortino_3y_peer_pct": None,
        "info_ratio_3y": None,
        "capture_up": None,
        "capture_down": None,
        "capture_efficiency": None,
        "r_squared_3y": None,
        "beta_3y": None,
        "data_quality_flag": flag,
        "computed_at": datetime.utcnow(),
        "pipeline_version": get_pipeline_config().pipeline_version,
    }
    if flag == "INSUFFICIENT_HISTORY" or aligned.is_empty():
        return row
    r3 = returns.rolling_distribution(aligned, window_years=3, step=step)
    r5 = returns.rolling_distribution(aligned, window_years=5, step=step)
    abr = alpha.rolling_alpha_beta_r2(aligned, window_years=3, step=step)
    so = sortino.rolling_sortino(aligned, window_years=3, step=step)
    cap = capture.rolling_capture(aligned, window_years=3, step=step)
    ir = info_ratio.rolling_information_ratio(aligned, window_years=3, step=step)

    row.update(
        ret_3y_median=r3["median"],
        ret_3y_p25=r3["p25"],
        ret_5y_median=r5["median"],
        ret_5y_p25=r5["p25"],
        alpha_3y_annualized=abr["alpha_ann"],
        alpha_3y_tstat=abr["alpha_tstat"],
        sortino_3y=so,
        info_ratio_3y=ir,
        capture_up=cap["capture_up"],
        capture_down=cap["capture_down"],
        capture_efficiency=cap["capture_efficiency"],
        r_squared_3y=abr["r2"],
        beta_3y=abr["beta"],
    )
    return row


def run(
    as_of: date | None = None,
    only_scheme: str | None = None,
    *,
    skip_freshness: bool = False,
) -> pl.DataFrame:
    """Compute metrics for every active Direct+Growth scheme with a benchmark mapping.

    Refuses to run with stale inputs unless skip_freshness=True (debug only).
    """
    cfg = get_pipeline_config()
    as_of = as_of or date.today()
    if not skip_freshness:
        check_freshness(as_of=as_of, raise_on_fail=True)
    sm_path = paths.scheme_master_file()
    if not sm_path.exists():
        raise RuntimeError("scheme_master not built; run `mfs build scheme-master` first")
    sm = pl.read_parquet(sm_path)
    eligible = sm.filter(
        (pl.col("plan_type") == cfg.filters.plan_type)
        & (pl.col("option_type") == cfg.filters.option_type)
        & (pl.col("is_active"))
        & (pl.col("benchmark_ticker").is_not_null())
    )
    if only_scheme:
        eligible = eligible.filter(pl.col("scheme_code") == only_scheme)
    log.info("metrics.run.start", n_eligible=eligible.height, as_of=as_of.isoformat())

    rows: list[dict] = []
    step = cfg.rolling.step
    for r in eligible.iter_rows(named=True):
        try:
            row = compute_for_scheme(
                r["scheme_code"], r["benchmark_ticker"], r["canonical_category"], as_of, step=step
            )
            rows.append(row)
        except Exception as e:  # noqa: BLE001
            log.warning("metrics.scheme.failed", scheme_code=r["scheme_code"], err=str(e))

    if not rows:
        log.warning("metrics.empty")
        return pl.DataFrame()

    df = pl.DataFrame(rows)

    # Peer percentile for Sortino (within canonical_category)
    by_cat_pct: dict[tuple[str, str], float | None] = {}
    for cat, grp in df.group_by("canonical_category"):
        vals = {row["scheme_code"]: row["sortino_3y"] for row in grp.iter_rows(named=True)}
        from mfs.compute.sortino import peer_percentile

        pcts = peer_percentile(vals)
        for sc, p in pcts.items():
            by_cat_pct[(cat[0], sc)] = p
    df = df.with_columns(
        pl.struct(["canonical_category", "scheme_code"])
        .map_elements(
            lambda s: by_cat_pct.get((s["canonical_category"], s["scheme_code"])),
            return_dtype=pl.Float64,
        )
        .alias("sortino_3y_peer_pct")
    )

    df = df.with_columns(pl.lit(as_of).cast(pl.Date).alias("as_of_date"))
    write_partition(df, paths.computed_metrics_dataset(), as_of.isoformat(), "as_of_date")
    log.info("metrics.run.done", rows=df.height, as_of=as_of.isoformat())
    return df
