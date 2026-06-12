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
from typing import cast

import polars as pl

from mfs.compute import (
    active_share, alignment, alpha, aum_impact, capture, info_ratio,
    returns, sortino,
)
from mfs.config import get_pipeline_config
from mfs.db import queries as q
from mfs.db import writers as w
from mfs.errors import PipelineError
from mfs.freshness import check_freshness
from mfs.utils.logging import get_logger

log = get_logger(__name__)


def _data_quality_flag(
    aligned: pl.DataFrame,
    min_years: float = 3.0,
    *,
    as_of: date | None = None,
    max_nav_lag_bdays: int | None = None,
) -> str:
    if aligned.is_empty():
        return "INSUFFICIENT_HISTORY"
    df = aligned.select(["date", "nav"]).drop_nulls()
    if df.is_empty():
        return "INSUFFICIENT_HISTORY"
    span_days = (cast(date, df["date"].max()) - cast(date, df["date"].min())).days
    if span_days < int(365.25 * min_years):
        return "INSUFFICIENT_HISTORY"
    # A1-3 per-scheme staleness gate: a scheme whose last NAV predates the
    # max_nav_lag_bdays-th-from-last trading day on or before as_of is dead,
    # merged, or suspended — its metrics describe a portfolio that no longer
    # exists. Flag STALE before the GOOD/POOR checks (which only look at the
    # scheme's OWN trailing window and so can't see end-staleness). Metrics
    # are still computed and stored; rank/filters.py excludes the row with
    # exclusion_reason STALE_NAV.
    if as_of is not None and max_nav_lag_bdays is not None:
        cal_le = alignment.master_calendar().filter(pl.col("date") <= as_of)
        if cal_le.height >= max_nav_lag_bdays:
            threshold = cal_le["date"][-max_nav_lag_bdays]
            if df["date"].max() < threshold:
                return "STALE"
    end = cast(date, df["date"].max())
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


def _check_scheme_failures(
    phase: str,
    failures: list[tuple[str, str]],
    n_attempted: int,
    max_rate: float,
) -> None:
    """A1-9: escalate per-scheme compute failures. Each failure is a
    skip-and-report (no-half-data invariant: the scheme is skipped, never
    stored partially), but when the failure fraction exceeds ``max_rate``
    the breakage is systemic (DB type change, benchmark corruption) and the
    run must halt instead of silently shrinking the universe."""
    if not failures or n_attempted <= 0:
        return
    failed_codes = [code for code, _ in failures]
    log.error(
        f"metrics.{phase}.failures",
        n_failed=len(failures),
        n_attempted=n_attempted,
        failed_codes=failed_codes[:10],
    )
    rate = len(failures) / n_attempted
    if rate > max_rate:
        shown = failed_codes[:50]
        more = len(failed_codes) - len(shown)
        codes_str = ", ".join(shown) + (f" (+{more} more)" if more else "")
        errors_str = "; ".join(f"{c}: {err}" for c, err in failures[:10])
        raise PipelineError(
            f"compute {phase}: {len(failures)}/{n_attempted} schemes failed "
            f"({rate:.1%} > quality.max_scheme_compute_failure_rate="
            f"{max_rate:.1%}); halting instead of writing a silently shrunk "
            f"partition. Failed scheme_codes: {codes_str}. "
            f"First errors: {errors_str}"
        )


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
    quality_cfg = get_pipeline_config().quality
    flag = _data_quality_flag(
        aligned,
        min_years=quality_cfg.min_history_years_for_ranking,
        as_of=as_of,
        max_nav_lag_bdays=quality_cfg.max_scheme_nav_lag_bdays,
    )
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
        # Share of rolling windows with |t| >= 1; display-only (locked D1).
        "alpha_confidence": None,
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
        "adv_unresolved_pct": None,
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
        alpha_confidence=abr["alpha_confidence"],
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
    failures: list[tuple[str, str]] = []
    step = cfg.rolling.step
    for r in eligible.iter_rows(named=True):
        try:
            row = compute_phase1_for_scheme(
                r["scheme_code"], r["benchmark_ticker"], r["canonical_category"],
                as_of, step=step,
            )
            rows.append(row)
        except Exception as e:  # noqa: BLE001
            failures.append((r["scheme_code"], repr(e)))
            log.warning("metrics.phase1.scheme.failed",
                        scheme_code=r["scheme_code"], err=str(e))

    # A1-9: summarize + halt on systemic failure BEFORE any write, so a
    # broken run never persists an empty/partial partition.
    _check_scheme_failures(
        "phase1", failures, n_attempted=eligible.height,
        max_rate=cfg.quality.max_scheme_compute_failure_rate,
    )

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
        "adv_unresolved_pct": None,
    }

    # Style drift = σ(β_rolling) + (1 − μ(R²_rolling)). Pure derived column.
    if beta_3y_std is not None and r_squared_3y_mean is not None:
        row["style_drift_3y"] = float(beta_3y_std) + (1.0 - float(r_squared_3y_mean))

    # Point-in-time bound (A1-12), mirroring the AUM on_or_before guard below:
    # a holdings/PTR month published after the metric date must not leak into
    # a historical as_of (D2 history tables and the D3 backtest depend on it).
    scheme_holdings = q.holdings_for_scheme(scheme_code, on_or_before=as_of)
    scheme_ptr = q.portfolio_turnover_for_scheme(scheme_code, on_or_before=as_of)
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
        adv_history = q.stock_adv_history(isins) if isins else pl.DataFrame()
        adv_by_isin = aum_impact._median_adv(adv_history)
        # A1-11: always emit the ADV-coverage fraction; aum_impact_cost
        # returns None when the unresolved weight exceeds the guard (the
        # metric would describe a sliver of the book, e.g. international
        # FoF-style funds with no NSE EQ-series ADV).
        row["adv_unresolved_pct"] = aum_impact.adv_unresolved_fraction(
            latest_holdings, adv_by_isin,
        )
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
    failures: list[tuple[str, str]] = []
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
            failures.append((r["scheme_code"], repr(e)))
            log.warning("metrics.phase2.scheme.failed",
                        scheme_code=r["scheme_code"], err=str(e))

    # A1-9: summarize + halt on systemic failure BEFORE any write.
    _check_scheme_failures(
        "phase2", failures, n_attempted=existing.height,
        max_rate=get_pipeline_config().quality.max_scheme_compute_failure_rate,
    )

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
    **_ignored: object,
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
