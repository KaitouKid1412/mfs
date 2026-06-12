#!/usr/bin/env python
"""Retro-IC backtest of the Stage-1 composite weights (D3). READ-ONLY —
never writes Postgres (only parquet/CSV artifacts under data/output/backtest).

For each quarter-end as_of from --since (default 2016-03-31; all 26 benchmark
tickers and NAV history start 2013-01-01, so 3y-window metrics exist from
2016) to the latest quarter with a full forward horizon:

  * Universe: CURRENT scheme_master DIRECT+GROWTH rankable-category funds
    with a benchmark. *** SURVIVORSHIP-BIASED: dead/merged funds are
    unrecoverable and absent, so every IC reported here is an UPPER BOUND on
    real-time persistence. Printed in every output header. ***
  * Per fund: production alignment (alignment.align_scheme) truncated
    tool-side to date <= as_of; requires >= 3y of history at as_of. The
    NAV-only Stage-1 metrics are recomputed with the PRODUCTION compute
    functions exactly as the orchestrator does (returns.rolling_distribution
    3y/5y; alpha.rolling_alpha_beta_r2 — post-D1 SIGNED alpha, median over
    ALL windows; sortino; capture; info_ratio), then the production hard
    filters + D2 core-metric gate, z-scored within category
    (rank/zscore.py), composite via composite_weights_stage1 from
    pipeline.yaml. No stage 2/3 (Phase-2 metrics have no pre-2026 history —
    by design not backtestable).
  * Forward outcome: fund simple NAV return over [as_of, as_of+1y] / [+3y]
    minus the category median of the same.
  * Per (quarter, category, horizon): Spearman IC (rank-transform + Pearson;
    numpy/pandas only) of composite vs forward category-relative return,
    skipping categories with n < 8 measurable funds; plus the
    top-5-vs-category-median forward spread.
  * Pooled: mean IC, t = mean/std*sqrt(n_quarters). The 3y horizon's t uses
    NON-OVERLAPPING windows (every 12th quarter); the overlapping mean is
    reported alongside with an autocorrelation caveat.
  * Sensitivity: each stage-1 weight +/-50% (renormalized) plus the D10
    p25_collapsed variant (ret_3y_median 0.25 / ret_5y_median 0.25 / p25
    weights 0) — mean-IC delta and mean top-5 overlap vs baseline.

PRE-REGISTERED VERDICT THRESHOLDS (printed in the report):
  JUSTIFIED     pooled 1y mean IC >= 0.05 with |t| >= 2 AND top-5 spread > 0
  REFUTED       pooled 1y mean IC <= 0 or |t| < 1
                (=> move to equal-weight / returns-only pending redesign)
  INCONCLUSIVE  otherwise (keep weights, label outputs 'unvalidated')

Outputs: data/output/backtest/<run-date>/ic_by_quarter.csv, ic_summary.csv,
sensitivity.csv + a stdout verdict line. Per-(scheme, as_of) metric rows are
cached as parquet under data/output/backtest/cache/ so re-runs are
incremental.

Usage:
    uv run python tools/backtest_ic.py                       # full 2016+
    uv run python tools/backtest_ic.py --since 2022-01-01 --horizons 1y
    uv run python tools/backtest_ic.py --since 2024-12-01 \
        --categories 'Large Cap,Mid Cap' --horizons 1y       # smoke
"""

from __future__ import annotations

import argparse
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

# ---------------------------------------------------------------------------
# Pre-registered thresholds (see docstring; classify_verdict applies them)
# ---------------------------------------------------------------------------
JUSTIFIED_MIN_MEAN_IC = 0.05
JUSTIFIED_MIN_ABS_T = 2.0
REFUTED_MAX_ABS_T = 1.0

MIN_FUNDS_PER_CATEGORY = 8
MIN_HISTORY_YEARS = 3.0
#: Last NAV must be within this many days of as_of (dead-at-as_of guard).
MAX_NAV_STALENESS_DAYS = 30
#: Forward-return endpoints must have a NAV within this many days.
MAX_FORWARD_GAP_DAYS = 14
#: Quarters per non-overlapping 3y window.
NONOVERLAP_QUARTERS = {1: 4, 3: 12}

TOP_N = 5

SURVIVORSHIP_HEADER = (
    "=" * 76 + "\n"
    "RETRO-IC BACKTEST — SURVIVORSHIP-BIASED (UPPER BOUND).\n"
    "Universe = CURRENT scheme_master actives; funds that died or merged\n"
    "since 2016 are unrecoverable and absent, so measured IC is an UPPER\n"
    "BOUND on real-time weight persistence.\n"
    f"Pre-registered verdict (1y horizon): JUSTIFIED if mean IC >= "
    f"{JUSTIFIED_MIN_MEAN_IC} with |t| >= {JUSTIFIED_MIN_ABS_T} and top-5 "
    "spread > 0;\n"
    f"REFUTED if mean IC <= 0 or |t| < {REFUTED_MAX_ABS_T}; else "
    "INCONCLUSIVE.\n"
    + "=" * 76
)

#: Metric columns produced per (scheme, as_of) — mirrors the NAV-only subset
#: of orchestrator.compute_phase1_for_scheme.
METRIC_COLS = [
    "ret_3y_median", "ret_3y_p25", "ret_5y_median", "ret_5y_p25",
    "alpha_3y_annualized", "sortino_3y", "info_ratio_3y",
    "capture_up", "capture_down", "capture_efficiency",
    "r_squared_3y", "beta_3y",
]


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_backtest_ic.py — no DB)
# ---------------------------------------------------------------------------


def truncate_aligned(aligned: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """Point-in-time view of a production-aligned frame: rows on/before
    as_of only (tool-side filter; no core compute changes)."""
    if aligned.is_empty():
        return aligned
    return aligned.filter(pl.col("date") <= as_of)


def has_min_history(
    aligned: pl.DataFrame,
    as_of: date,
    min_years: float = MIN_HISTORY_YEARS,
    max_staleness_days: int = MAX_NAV_STALENESS_DAYS,
) -> bool:
    """>= min_years of NAV span at as_of AND a NAV within max_staleness_days
    of as_of (a fund whose series stops years before as_of was dead then)."""
    if aligned.is_empty() or "nav" not in aligned.columns:
        return False
    df = aligned.select(["date", "nav"]).drop_nulls()
    if df.is_empty():
        return False
    d_min, d_max = df["date"].min(), df["date"].max()
    if (as_of - d_max).days > max_staleness_days:
        return False
    return (d_max - d_min).days >= int(365.25 * min_years)


def spearman_ic(x: np.ndarray, y: np.ndarray) -> float | None:
    """Spearman rank IC: average-rank transform (ties handled) + Pearson.
    None when fewer than 3 pairs or either rank vector is constant."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if len(x) < 3:
        return None
    rx = pd.Series(x).rank(method="average").to_numpy()
    ry = pd.Series(y).rank(method="average").to_numpy()
    if np.std(rx) == 0 or np.std(ry) == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def forward_simple_return(
    nav: pl.DataFrame,
    as_of: date,
    horizon_years: int,
    max_gap_days: int = MAX_FORWARD_GAP_DAYS,
) -> float | None:
    """Simple NAV return over [as_of, as_of + horizon]. Endpoints are the
    last NAV on/before each target date, each required within max_gap_days
    of its target; None when either endpoint is missing/stale."""
    if nav.is_empty():
        return None
    end_target = as_of + timedelta(days=int(365.25 * horizon_years))
    start_rows = nav.filter(pl.col("date") <= as_of)
    end_rows = nav.filter(pl.col("date") <= end_target)
    if start_rows.is_empty() or end_rows.is_empty():
        return None
    d0, v0 = start_rows.row(-1)
    d1, v1 = end_rows.row(-1)
    if (as_of - d0).days > max_gap_days or (end_target - d1).days > max_gap_days:
        return None
    if d1 <= d0 or not v0 or v0 <= 0 or v1 is None:
        return None
    return float(v1) / float(v0) - 1.0


def category_relative(frame: pl.DataFrame, ret_col: str = "forward_ret") -> pl.DataFrame:
    """Subtract the per-category median of ``ret_col`` (over funds with a
    measurable value) — adds ``forward_rel``."""
    return frame.with_columns(
        (pl.col(ret_col) - pl.col(ret_col).median().over("canonical_category"))
        .alias("forward_rel")
    )


def perturb_weights(
    weights: dict[str, float], key: str, factor: float,
) -> dict[str, float]:
    """Scale one weight by ``factor`` then renormalize so the total equals
    the original total (1.0 for stage-1 weights)."""
    w = dict(weights)
    w[key] = w[key] * factor
    total, base = sum(w.values()), sum(weights.values())
    if total <= 0:
        raise ValueError("perturbation drove total weight to <= 0")
    return {k: v * base / total for k, v in w.items()}


def p25_collapsed_variant(weights: dict[str, float]) -> dict[str, float]:
    """D10's registered collapse candidate: ret_3y_median 0.25 /
    ret_5y_median 0.25 / both p25 weights 0, renormalized to the original
    total (a no-op at the shipped weights, a guard under drift)."""
    w = dict(weights)
    w["ret_3y_median"] = 0.25
    w["ret_5y_median"] = 0.25
    w["ret_3y_p25"] = 0.0
    w["ret_5y_p25"] = 0.0
    total, base = sum(w.values()), sum(weights.values())
    return {k: v * base / total for k, v in w.items()}


def build_variants(weights: dict[str, float]) -> dict[str, dict[str, float]]:
    """Sensitivity suite: each weight +/-50% (renormalized) + p25_collapsed."""
    variants: dict[str, dict[str, float]] = {}
    for key in weights:
        variants[f"{key}+50%"] = perturb_weights(weights, key, 1.5)
        variants[f"{key}-50%"] = perturb_weights(weights, key, 0.5)
    variants["p25_collapsed"] = p25_collapsed_variant(weights)
    return variants


def classify_verdict(
    mean_ic: float | None, t: float | None, top5_spread: float | None,
) -> str:
    """Apply the pre-registered thresholds (module docstring). REFUTED is
    checked first; JUSTIFIED requires all three conditions; anything else is
    INCONCLUSIVE."""
    if mean_ic is None or t is None:
        return "INCONCLUSIVE"
    if mean_ic <= 0 or abs(t) < REFUTED_MAX_ABS_T:
        return "REFUTED"
    if (
        mean_ic >= JUSTIFIED_MIN_MEAN_IC
        and abs(t) >= JUSTIFIED_MIN_ABS_T
        and top5_spread is not None
        and top5_spread > 0
    ):
        return "JUSTIFIED"
    return "INCONCLUSIVE"


def quarter_ends(since: date, until: date) -> list[date]:
    """Calendar quarter-end dates in [since, until]."""
    ends = []
    for yy in range(since.year, until.year + 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            qe = date(yy, m, d)
            if since <= qe <= until:
                ends.append(qe)
    return ends


def pooled_stats(ics: list[float]) -> tuple[float | None, float | None, int]:
    """(mean, t, n) with t = mean/std*sqrt(n) (sample std); t None if n<2 or
    std==0."""
    n = len(ics)
    if n == 0:
        return None, None, 0
    mean = float(np.mean(ics))
    if n < 2:
        return mean, None, n
    std = float(np.std(ics, ddof=1))
    if std == 0:
        return mean, None, n
    return mean, mean / std * math.sqrt(n), n


def top_n_codes(frame: pl.DataFrame, score_col: str, n: int = TOP_N) -> list[str]:
    """Deterministic top-N scheme codes: score desc, scheme_code asc."""
    return (
        frame.sort([score_col, "scheme_code"], descending=[True, False],
                   nulls_last=True)
        .head(n)["scheme_code"]
        .to_list()
    )


# ---------------------------------------------------------------------------
# DB-backed metric recomputation (production compute functions)
# ---------------------------------------------------------------------------


def compute_stage1_row(aligned_t: pl.DataFrame, step: str) -> dict:
    """NAV-only Stage-1 metrics on a truncated aligned frame — the exact
    production calls orchestrator.compute_phase1_for_scheme makes."""
    from mfs.compute import alpha, capture, info_ratio, returns, sortino

    r3 = returns.rolling_distribution(aligned_t, window_years=3, step=step)
    r5 = returns.rolling_distribution(aligned_t, window_years=5, step=step)
    abr = alpha.rolling_alpha_beta_r2(aligned_t, window_years=3, step=step)
    so = sortino.rolling_sortino(aligned_t, window_years=3, step=step)
    cap = capture.rolling_capture(aligned_t, window_years=3, step=step)
    ir = info_ratio.rolling_information_ratio(aligned_t, window_years=3, step=step)
    return {
        "ret_3y_median": r3["median"],
        "ret_3y_p25": r3["p25"],
        "ret_5y_median": r5["median"],
        "ret_5y_p25": r5["p25"],
        "alpha_3y_annualized": abr["alpha_ann"],
        "sortino_3y": so,
        "info_ratio_3y": ir,
        "capture_up": cap["capture_up"],
        "capture_down": cap["capture_down"],
        "capture_efficiency": cap["capture_efficiency"],
        "r_squared_3y": abr["r2"],
        "beta_3y": abr["beta"],
    }


class _Caches:
    """Per-process caches: full aligned frame + raw NAV per scheme (each is
    invariant across quarters), and the per-quarter metric parquet."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._aligned: dict[str, pl.DataFrame] = {}
        self._nav: dict[str, pl.DataFrame] = {}

    def aligned(self, code: str, ticker: str) -> pl.DataFrame:
        from mfs.compute import alignment

        if code not in self._aligned:
            self._aligned[code] = alignment.align_scheme(code, ticker)
        return self._aligned[code]

    def nav(self, code: str) -> pl.DataFrame:
        from mfs.db import queries as q

        if code not in self._nav:
            self._nav[code] = q.nav_series(code)
        return self._nav[code]

    def quarter_path(self, as_of: date) -> Path:
        return self.cache_dir / f"metrics_{as_of.isoformat()}.parquet"

    def load_quarter(self, as_of: date) -> pl.DataFrame:
        p = self.quarter_path(as_of)
        if p.exists():
            try:
                df = pl.read_parquet(p)
            except Exception:  # noqa: BLE001 — corrupt cache: recompute
                return pl.DataFrame()
            need = {"scheme_code", "insufficient", *METRIC_COLS}
            if need.issubset(df.columns):
                return df
        return pl.DataFrame()

    def save_quarter(self, as_of: date, frame: pl.DataFrame) -> None:
        frame.write_parquet(self.quarter_path(as_of), compression="zstd")


def metric_rows_for_quarter(
    as_of: date,
    universe: pl.DataFrame,
    caches: _Caches,
    step: str,
) -> pl.DataFrame:
    """One row per universe scheme at as_of (cached): identity columns +
    METRIC_COLS + ``insufficient`` (no 3y history / stale at as_of)."""
    cached = caches.load_quarter(as_of)
    have: set[str] = (
        set(cached["scheme_code"].to_list()) if not cached.is_empty() else set()
    )
    new_rows: list[dict] = []
    for r in universe.iter_rows(named=True):
        code = r["scheme_code"]
        if code in have:
            continue
        row: dict = {
            "scheme_code": code,
            "canonical_category": r["canonical_category"],
            "benchmark_ticker": r["benchmark_ticker"],
            "insufficient": False,
            **{c: None for c in METRIC_COLS},
        }
        aligned_t = truncate_aligned(
            caches.aligned(code, r["benchmark_ticker"]), as_of
        )
        if not has_min_history(aligned_t, as_of):
            row["insufficient"] = True
        else:
            try:
                row.update(compute_stage1_row(aligned_t, step))
            except Exception:  # noqa: BLE001 — per-scheme isolation
                row["insufficient"] = True
        new_rows.append(row)

    if new_rows:
        schema = {
            "scheme_code": pl.Utf8,
            "canonical_category": pl.Utf8,
            "benchmark_ticker": pl.Utf8,
            "insufficient": pl.Boolean,
            **{c: pl.Float64 for c in METRIC_COLS},
        }
        new_frame = pl.DataFrame(
            [[row[c] for c in schema] for row in new_rows],
            schema=schema, orient="row",
        )
        cached = (
            pl.concat([cached.select(list(schema)), new_frame])
            if not cached.is_empty() else new_frame
        )
        caches.save_quarter(as_of, cached)
    # Restrict to the requested universe (cache may hold more categories).
    want = set(universe["scheme_code"].to_list())
    return cached.filter(pl.col("scheme_code").is_in(list(want)))


def scored_frame_for_quarter(
    metric_rows: pl.DataFrame,
    weights: dict[str, float],
    variants: dict[str, dict[str, float]],
) -> pl.DataFrame:
    """Production rank path on recomputed metrics: hard filters (data quality
    pre-checked → GOOD) → D2 core-metric gate → z-score within category →
    baseline composite (production composite_score_stage1) + one composite
    column per sensitivity variant."""
    from mfs.rank import score
    from mfs.rank.filters import apply_hard_filters, split_core_complete
    from mfs.rank.zscore import zscore_within_category

    usable = metric_rows.filter(~pl.col("insufficient")).with_columns(
        pl.lit("GOOD").alias("data_quality_flag")
    )
    if usable.is_empty():
        return usable
    survivors = apply_hard_filters(usable)
    survivors, _ = split_core_complete(survivors)
    if survivors.is_empty():
        return survivors
    z = zscore_within_category(survivors)
    z = score.composite_score_stage1(z)  # baseline: the production composite
    proxied = score._apply_proxy_fallback(z)
    variant_exprs = [
        score._weighted_sum(proxied, w, score.STAGE1_WEIGHT_TO_COL)
        .alias(f"composite__{name}")
        for name, w in variants.items()
    ]
    return proxied.with_columns(variant_exprs)


# ---------------------------------------------------------------------------
# Main backtest loop
# ---------------------------------------------------------------------------


def run_backtest(
    since: date,
    categories: list[str] | None,
    horizons: list[int],
    step: str | None,
    out_root: Path | None = None,
) -> dict:
    from mfs.compute import alignment
    from mfs.config import get_pipeline_config, get_thresholds
    from mfs.db import queries as q

    print(SURVIVORSHIP_HEADER)
    cfg = get_pipeline_config()
    step = step or cfg.rolling.step
    weights = dict(cfg.composite_weights_stage1)
    variants = build_variants(weights)

    cal = alignment.master_calendar()
    if cal.is_empty():
        raise RuntimeError("benchmark_daily is empty — no trading calendar")
    max_date = cal["date"].max()
    min_h = min(horizons)
    until = max_date - timedelta(days=int(365.25 * min_h))
    quarters = quarter_ends(since, until)
    if not quarters:
        raise RuntimeError(
            f"No quarter-ends in [{since}, {until}] — nothing to backtest"
        )

    universe = q.scheme_master(
        plan_type=cfg.filters.plan_type,
        option_type=cfg.filters.option_type,
        is_active=True,
        require_benchmark=True,
    )
    rankable = set(get_thresholds().get("rankable_categories", []))
    universe = universe.filter(pl.col("canonical_category").is_in(list(rankable)))
    if categories:
        universe = universe.filter(pl.col("canonical_category").is_in(categories))
    if universe.is_empty():
        raise RuntimeError("Empty universe after category filters")
    universe = universe.select(
        ["scheme_code", "canonical_category", "benchmark_ticker"]
    ).sort("scheme_code")
    print(
        f"[backtest] {universe.height} funds, "
        f"{universe['canonical_category'].n_unique()} categories, "
        f"{len(quarters)} quarters ({quarters[0]} .. {quarters[-1]}), "
        f"horizons={[f'{h}y' for h in horizons]}, step={step}"
    )

    out_root = out_root or (Path("data") / "output" / "backtest")
    out_dir = out_root / date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    caches = _Caches(out_root / "cache")

    by_quarter_rows: list[dict] = []
    # variant -> horizon -> list of (ic, overlap) per category-quarter
    sens_acc: dict[str, list[tuple[float | None, float]]] = {
        name: [] for name in variants
    }

    for as_of in quarters:
        metric_rows = metric_rows_for_quarter(as_of, universe, caches, step)
        scored = scored_frame_for_quarter(metric_rows, weights, variants)
        if scored.is_empty():
            print(f"[backtest] {as_of}: no scoreable funds — skipped")
            continue
        for h in horizons:
            fwd = [
                forward_simple_return(caches.nav(c), as_of, h)
                for c in scored["scheme_code"].to_list()
            ]
            fh = scored.with_columns(
                pl.Series("forward_ret", fwd, dtype=pl.Float64)
            ).filter(pl.col("forward_ret").is_not_null())
            if fh.is_empty():
                continue
            fh = category_relative(fh)
            for cat_t, grp in fh.group_by("canonical_category"):
                cat = cat_t[0]
                if grp.height < MIN_FUNDS_PER_CATEGORY:
                    continue
                ic = spearman_ic(
                    grp["composite_score"].to_numpy(),
                    grp["forward_rel"].to_numpy(),
                )
                if ic is None:
                    continue
                base_top = top_n_codes(grp, "composite_score")
                spread = float(
                    grp.filter(pl.col("scheme_code").is_in(base_top))
                    ["forward_rel"].mean()
                )
                by_quarter_rows.append({
                    "as_of": as_of,
                    "canonical_category": cat,
                    "horizon": f"{h}y",
                    "n_funds": grp.height,
                    "ic": ic,
                    "top5_spread": spread,
                })
                if h == min_h:  # sensitivity judged on the shortest horizon
                    for name in variants:
                        col = f"composite__{name}"
                        v_ic = spearman_ic(
                            grp[col].to_numpy(), grp["forward_rel"].to_numpy()
                        )
                        v_top = top_n_codes(grp, col)
                        overlap = len(set(v_top) & set(base_top)) / max(
                            len(base_top), 1
                        )
                        sens_acc[name].append((v_ic, overlap))
        print(f"[backtest] {as_of}: done")

    if not by_quarter_rows:
        raise RuntimeError(
            "No (quarter, category, horizon) cell reached the n>=8 floor"
        )
    by_quarter = pl.DataFrame(by_quarter_rows).sort(
        ["horizon", "canonical_category", "as_of"]
    )
    by_quarter.write_csv(out_dir / "ic_by_quarter.csv")

    # ---- summary: per (category, horizon) + pooled ALL per horizon --------
    summary_rows: list[dict] = []
    verdict_inputs: dict[str, tuple] = {}
    for h in horizons:
        hz = f"{h}y"
        hq = by_quarter.filter(pl.col("horizon") == hz)
        if hq.is_empty():
            continue
        nonoverlap_step = NONOVERLAP_QUARTERS.get(h, 1)
        all_quarters = sorted(hq["as_of"].unique().to_list())
        nonoverlap = set(all_quarters[::nonoverlap_step])
        for cat_t, grp in sorted(
            hq.group_by("canonical_category"), key=lambda kv: kv[0][0]
        ):
            ics = grp["ic"].to_list()
            mean_ic, t_overlap, n = pooled_stats(ics)
            no_ics = grp.filter(pl.col("as_of").is_in(list(nonoverlap)))[
                "ic"
            ].to_list()
            _, t_no, n_no = pooled_stats(no_ics)
            summary_rows.append({
                "canonical_category": cat_t[0],
                "horizon": hz,
                "n_quarters": n,
                "mean_ic": mean_ic,
                "t_nonoverlapping": t_no,
                "n_quarters_nonoverlapping": n_no,
                "t_overlapping_autocorr_caveat": t_overlap,
                "mean_top5_spread": float(grp["top5_spread"].mean()),
            })
        # pooled ALL: equal-weight category ICs within each quarter first
        per_q = (
            hq.group_by("as_of")
            .agg(pl.col("ic").mean(), pl.col("top5_spread").mean())
            .sort("as_of")
        )
        ics_all = per_q["ic"].to_list()
        mean_all, t_all_overlap, n_all = pooled_stats(ics_all)
        no_all = per_q.filter(pl.col("as_of").is_in(list(nonoverlap)))[
            "ic"
        ].to_list()
        _, t_all_no, n_all_no = pooled_stats(no_all)
        spread_all = float(per_q["top5_spread"].mean())
        t_verdict = t_all_no if h > 1 else t_all_overlap
        summary_rows.append({
            "canonical_category": "ALL",
            "horizon": hz,
            "n_quarters": n_all,
            "mean_ic": mean_all,
            "t_nonoverlapping": t_all_no,
            "n_quarters_nonoverlapping": n_all_no,
            "t_overlapping_autocorr_caveat": t_all_overlap,
            "mean_top5_spread": spread_all,
        })
        verdict_inputs[hz] = (mean_all, t_verdict, spread_all)
    summary = pl.DataFrame(summary_rows)
    summary.write_csv(out_dir / "ic_summary.csv")

    # ---- sensitivity -------------------------------------------------------
    base_ics = [
        r["ic"] for r in by_quarter_rows if r["horizon"] == f"{min_h}y"
    ]
    base_mean = float(np.mean(base_ics)) if base_ics else None
    sens_rows = []
    for name, cells in sens_acc.items():
        ics = [ic for ic, _ in cells if ic is not None]
        overlaps = [ov for _, ov in cells]
        v_mean = float(np.mean(ics)) if ics else None
        sens_rows.append({
            "variant": name,
            "horizon": f"{min_h}y",
            "n_cells": len(cells),
            "mean_ic": v_mean,
            "mean_ic_delta_vs_baseline": (
                v_mean - base_mean
                if v_mean is not None and base_mean is not None else None
            ),
            "mean_top5_overlap_vs_baseline": (
                float(np.mean(overlaps)) if overlaps else None
            ),
        })
    sensitivity = pl.DataFrame(sens_rows)
    sensitivity.write_csv(out_dir / "sensitivity.csv")

    # ---- verdict (pooled 1y row per the pre-registered thresholds) ---------
    v1 = verdict_inputs.get("1y")
    if v1 is not None:
        verdict = classify_verdict(*v1)
        mean_s = f"{v1[0]:+.4f}" if v1[0] is not None else "n/a"
        t_s = f"{v1[1]:+.2f}" if v1[1] is not None else "n/a"
        sp_s = f"{v1[2]:+.4f}" if v1[2] is not None else "n/a"
        detail = f"pooled 1y mean IC {mean_s}, t {t_s}, top-5 spread {sp_s}"
    else:
        verdict = "INCONCLUSIVE"
        detail = "no 1y horizon in this run — verdict requires --horizons 1y"
    print(SURVIVORSHIP_HEADER)
    print(f"VERDICT: {verdict} ({detail}; survivorship-biased upper bound)")
    print(f"Artifacts: {out_dir}/ic_by_quarter.csv, ic_summary.csv, sensitivity.csv")
    return {
        "verdict": verdict,
        "out_dir": str(out_dir),
        "by_quarter": by_quarter,
        "summary": summary,
        "sensitivity": sensitivity,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--since", default="2016-01-01",
                   help="first quarter-end on/after this date (default 2016-01-01)")
    p.add_argument("--categories", default=None,
                   help="comma-separated canonical categories (default: all rankable)")
    p.add_argument("--horizons", default="1y,3y",
                   help="comma-separated forward horizons among 1y,3y (default both)")
    p.add_argument("--step", default=None,
                   help="rolling step 1w|1d (default: pipeline.yaml rolling.step)")
    args = p.parse_args(argv)

    since = datetime.strptime(args.since, "%Y-%m-%d").date()
    cats = (
        [c.strip() for c in args.categories.split(",") if c.strip()]
        if args.categories else None
    )
    horizons = []
    for tok in args.horizons.split(","):
        tok = tok.strip().lower()
        if not tok:
            continue
        if not tok.endswith("y") or not tok[:-1].isdigit():
            raise SystemExit(f"bad horizon {tok!r}; use e.g. 1y,3y")
        horizons.append(int(tok[:-1]))
    if not horizons:
        raise SystemExit("at least one horizon required")

    run_backtest(since, cats, horizons, args.step)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
