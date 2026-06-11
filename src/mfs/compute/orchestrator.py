"""Two-phase compute orchestrator.

**Phase 1** (`run_phase1`): for every eligible Direct+Growth scheme with a
benchmark, compute Performance & Consistency metrics (returns, alpha, beta,
R², Sortino, capture, info ratio) plus the cheap rolling-regression
diagnostics (``beta_3y_std``, ``r_squared_3y_mean``). Writes those columns
to ``computed_metrics``; Phase 2 columns stay NULL.

**Phase 2** (`run_phase2`): for a passed list of scheme codes (typically
the union of Stage-1 top-20 pools, ~520 schemes), compute the Phase 2
metrics: ``style_drift_3y`` (derived from beta_std + r2_mean stored in
Phase 1), ``active_share_median_1y``, ``ptr_latest``, ``aum_impact_cost_days``.
UPDATEs only those columns on the existing ``computed_metrics`` row.

A backward-compatible ``run`` wrapper sequences phase1 + phase2(all eligible).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl

from mfs.compute import (
    active_share, alignment, alpha, aum_impact, capture, info_ratio,
    returns, sortino,
)
from mfs.config import get_pipeline_config
from mfs.db import queries as q
from mfs.db import writers as w
from mfs.freshness import check_freshness
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


# ---------------------------------------------------------------------------
# Phase 1 — Performance & Consistency
# ---------------------------------------------------------------------------


def compute_phase1_for_scheme(
    scheme_code: str,
    benchmark_ticker: str,
    canonical_category: str | None,
    as_of: date,
    step: str = "1w",
) -> dict:
    """Compute Phase 1 metrics for one scheme. Returns the full row dict;
    Phase 2 columns are present but always NULL at this stage."""
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
        "info_ratio_3y": None,
        "capture_up": None,
        "capture_down": None,
        "capture_efficiency": None,
        "r_squared_3y": None,
        "beta_3y": None,
        # Cheap rolling-regression diagnostics; Phase 2 derives style_drift
        # from these without re-running the regression.
        "beta_3y_std": None,
        "r_squared_3y_mean": None,
        # Phase 2 outputs — always NULL after Phase 1.
        "style_drift_3y": None,
        "active_share_median_1y": None,
        "ptr_latest": None,
        "aum_impact_cost_days": None,
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
        beta_3y_std=abr.get("beta_std"),
        r_squared_3y_mean=abr.get("r2_mean"),
    )
    return row


def run_phase1(
    as_of: date | None = None,
    only_scheme: str | None = None,
    *,
    skip_freshness: bool = False,
) -> pl.DataFrame:
    """Compute Phase 1 metrics for every eligible Direct+Growth scheme with
    a benchmark mapping. UPSERTs into ``computed_metrics`` with Phase 2
    columns left NULL.
    """
    cfg = get_pipeline_config()
    as_of = as_of or date.today()
    if not skip_freshness:
        check_freshness(as_of=as_of, raise_on_fail=True)
    eligible = q.scheme_master(
        plan_type=cfg.filters.plan_type,
        option_type=cfg.filters.option_type,
        is_active=True,
        require_benchmark=True,
    )
    if eligible.is_empty():
        raise RuntimeError("No eligible Direct+Growth schemes in scheme_master; "
                           "run `mfs build scheme-master` first")
    if only_scheme:
        eligible = eligible.filter(pl.col("scheme_code") == only_scheme)
    log.info("metrics.phase1.start", n_eligible=eligible.height, as_of=as_of.isoformat())

    rows: list[dict] = []
    step = cfg.rolling.step
    for r in eligible.iter_rows(named=True):
        try:
            row = compute_phase1_for_scheme(
                r["scheme_code"], r["benchmark_ticker"], r["canonical_category"],
                as_of, step=step,
            )
            rows.append(row)
        except Exception as e:  # noqa: BLE001
            log.warning("metrics.phase1.scheme.failed",
                        scheme_code=r["scheme_code"], err=str(e))

    if not rows:
        log.warning("metrics.phase1.empty")
        return pl.DataFrame()

    df = pl.DataFrame(rows)
    df = df.with_columns(pl.lit(as_of).cast(pl.Date).alias("as_of_date"))
    w.upsert_computed_metrics_phase1(df, as_of=as_of)
    log.info("metrics.phase1.done", rows=df.height, as_of=as_of.isoformat())
    return df


# ---------------------------------------------------------------------------
# Phase 2 — Manager Skill & Liquidity
# ---------------------------------------------------------------------------


def compute_phase2_for_scheme(
    scheme_code: str,
    *,
    beta_3y_std: float | None,
    r_squared_3y_mean: float | None,
    benchmark_ticker: str | None = None,
    as_of: date | None = None,
) -> dict:
    """Compute Phase 2 metrics for one scheme. Reads holdings / PTR / AUM
    on demand. Returns a dict keyed by Phase 2 column names.

    ``beta_3y_std`` and ``r_squared_3y_mean`` are passed in by the caller
    from the Phase 1 row (avoids re-running the rolling regression).
    """
    row: dict = {
        "scheme_code": scheme_code,
        "style_drift_3y": None,
        "active_share_median_1y": None,
        "ptr_latest": None,
        "aum_impact_cost_days": None,
    }

    # Style drift = σ(β_rolling) + (1 − μ(R²_rolling)). Pure derived column.
    if beta_3y_std is not None and r_squared_3y_mean is not None:
        row["style_drift_3y"] = float(beta_3y_std) + (1.0 - float(r_squared_3y_mean))

    scheme_holdings = q.holdings_for_scheme(scheme_code)
    scheme_ptr = q.portfolio_turnover_for_scheme(scheme_code)
    bench_constituents = (
        q.constituents_for_ticker(benchmark_ticker)
        if benchmark_ticker else pl.DataFrame()
    )

    if (
        not scheme_holdings.is_empty()
        and not bench_constituents.is_empty()
        and as_of is not None
    ):
        row["active_share_median_1y"] = active_share.trailing_median_active_share(
            scheme_holdings, bench_constituents, as_of,
        )

    if not scheme_ptr.is_empty():
        latest = scheme_ptr.sort("as_of_month").row(-1, named=True)
        row["ptr_latest"] = float(latest["ptr"])

    # on_or_before=as_of so a quarter published after the metric date can't leak
    # future AUM into a historical metric (no-op today: only one quarter loaded).
    aum_info = q.latest_scheme_aum(scheme_code, on_or_before=as_of)
    if not scheme_holdings.is_empty() and aum_info is not None:
        _, aum_crore = aum_info
        last_month = scheme_holdings["as_of_month"].max()
        latest_holdings = scheme_holdings.filter(pl.col("as_of_month") == last_month)
        isins = [s for s in latest_holdings["isin"].to_list() if s]
        if isins:
            adv_history = q.stock_adv_history(isins)
            adv_by_isin = aum_impact._median_adv(adv_history)
            row["aum_impact_cost_days"] = aum_impact.aum_impact_cost(
                latest_holdings, adv_by_isin, aum_crore,
            )

    return row


def run_phase2(
    as_of: date,
    scheme_codes: list[str] | None = None,
) -> pl.DataFrame:
    """Compute Phase 2 metrics for the given scheme codes. If
    ``scheme_codes`` is None, runs over every row in the as_of partition
    (back-compat path used by the legacy ``run()`` wrapper).

    UPDATEs the four Phase 2 columns on the existing rows. The Phase 1
    row must already exist (``run_phase1`` must have been called first
    for that as_of).
    """
    existing = q.computed_metrics_at(as_of)
    if existing.is_empty():
        raise RuntimeError(
            f"Phase 2 requires Phase 1 to have run first for as_of={as_of.isoformat()}; "
            "computed_metrics is empty for that partition."
        )
    if scheme_codes is not None:
        existing = existing.filter(pl.col("scheme_code").is_in(scheme_codes))
    if existing.is_empty():
        log.warning("metrics.phase2.empty", n_requested=len(scheme_codes or []))
        return pl.DataFrame()

    log.info("metrics.phase2.start", n=existing.height, as_of=as_of.isoformat())
    rows: list[dict] = []
    for r in existing.iter_rows(named=True):
        try:
            row = compute_phase2_for_scheme(
                r["scheme_code"],
                beta_3y_std=r.get("beta_3y_std"),
                r_squared_3y_mean=r.get("r_squared_3y_mean"),
                benchmark_ticker=r.get("benchmark_ticker"),
                as_of=as_of,
            )
            rows.append(row)
        except Exception as e:  # noqa: BLE001
            log.warning("metrics.phase2.scheme.failed",
                        scheme_code=r["scheme_code"], err=str(e))

    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(rows)
    w.update_computed_metrics_phase2(df, as_of=as_of)
    log.info("metrics.phase2.done", rows=df.height, as_of=as_of.isoformat())
    return df


# ---------------------------------------------------------------------------
# Back-compat helpers
# ---------------------------------------------------------------------------


def compute_for_scheme(
    scheme_code: str,
    benchmark_ticker: str,
    canonical_category: str | None,
    as_of: date,
    step: str = "1w",
    **_ignored,
) -> dict:
    """Back-compat single-scheme compute that runs Phase 1 then Phase 2 in
    memory and returns one merged row dict. Used by the ``audit-scheme``
    CLI command and by tests that don't need the DB round-trip.
    """
    row = compute_phase1_for_scheme(
        scheme_code, benchmark_ticker, canonical_category, as_of, step=step,
    )
    phase2 = compute_phase2_for_scheme(
        scheme_code,
        beta_3y_std=row.get("beta_3y_std"),
        r_squared_3y_mean=row.get("r_squared_3y_mean"),
        benchmark_ticker=benchmark_ticker,
        as_of=as_of,
    )
    row.update({k: v for k, v in phase2.items() if k != "scheme_code"})
    return row


def run(
    as_of: date | None = None,
    only_scheme: str | None = None,
    *,
    skip_freshness: bool = False,
) -> pl.DataFrame:
    """Compute Phase 1 over all eligible schemes, then Phase 2 over the same
    set. Back-compat wrapper for callers that want both phases in one shot.
    """
    as_of = as_of or date.today()
    df1 = run_phase1(as_of=as_of, only_scheme=only_scheme, skip_freshness=skip_freshness)
    if df1.is_empty():
        return df1
    codes = df1["scheme_code"].to_list()
    run_phase2(as_of=as_of, scheme_codes=codes)
    return q.computed_metrics_at(as_of)
