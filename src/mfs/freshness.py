"""Freshness checks: assert every curated dataset is recent before compute/rank.

Compute and rank stages refuse to run if these checks don't pass. Thresholds are
configured under `freshness:` in pipeline.yaml.

Indian business days approximated by skipping Saturday/Sunday only — the
`max_*_lag_bdays` thresholds carry enough slack to cover NSE/AMFI holidays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

from mfs import paths
from mfs.config import get_pipeline_config
from mfs.errors import FreshnessError
from mfs.ingest.benchmarks import NSE_TRI_MAP, ticker_slug
from mfs.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class FreshnessReport:
    ok: bool
    as_of: date
    nav_latest: date | None = None
    benchmark_latest: dict[str, date] = field(default_factory=dict)
    risk_free_latest: date | None = None
    scheme_master_mtime: date | None = None
    issues: list[str] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        head = f"FreshnessReport(ok={self.ok}, as_of={self.as_of})"
        if self.issues:
            body = "\n  - " + "\n  - ".join(self.issues)
            return head + body
        return head


def last_business_day(d: date) -> date:
    """Return the most recent weekday on or before `d` (no holiday calendar)."""
    while d.weekday() >= 5:  # 5 = Sat, 6 = Sun
        d -= timedelta(days=1)
    return d


def business_days_between(start: date, end: date) -> int:
    """Count Mon-Fri days in (start, end] (exclusive of start, inclusive of end)."""
    if end <= start:
        return 0
    days = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


def _latest_nav_date() -> date | None:
    p = paths.nav_daily_dataset()
    if not p.exists():
        return None
    try:
        scan = pl.scan_parquet(str(p / "**" / "*.parquet"))
        result = scan.select(pl.col("nav_date").max()).collect()
        if result.is_empty():
            return None
        return result.item()
    except Exception as e:  # noqa: BLE001
        log.warning("freshness.nav_scan_failed", err=str(e))
        return None


def _latest_benchmark_dates() -> dict[str, date]:
    """Return {ticker_slug: latest close date} for each equity TRI on disk."""
    out: dict[str, date] = {}
    root = paths.benchmark_daily_dataset()
    if not root.exists():
        return out
    for ticker, (trad, _) in NSE_TRI_MAP.items():
        if not trad:
            continue  # hybrid/multi-asset — needs manual CSV, skip freshness
        slug = ticker_slug(ticker)
        part = root / f"ticker={slug}" / "data.parquet"
        if not part.exists():
            out[slug] = None  # type: ignore[assignment]
            continue
        try:
            df = pl.read_parquet(part, columns=["date"])
            out[slug] = df["date"].max()
        except Exception as e:  # noqa: BLE001
            log.warning("freshness.bench_scan_failed", ticker=ticker, err=str(e))
            out[slug] = None  # type: ignore[assignment]
    return out


def _latest_risk_free_date() -> date | None:
    p = paths.risk_free_daily_file()
    if not p.exists():
        return None
    try:
        df = pl.read_parquet(p, columns=["date"])
        return df["date"].max()
    except Exception as e:  # noqa: BLE001
        log.warning("freshness.rf_scan_failed", err=str(e))
        return None


def _scheme_master_mtime() -> date | None:
    p = paths.scheme_master_file()
    if not p.exists():
        return None
    return datetime.fromtimestamp(p.stat().st_mtime).date()


def check_freshness(as_of: date | None = None, *, raise_on_fail: bool = False) -> FreshnessReport:
    """Run every staleness check; return a FreshnessReport.

    If raise_on_fail=True and any check fails, raise FreshnessError after building
    the report (so the caller can include the full issue list in the error message).
    """
    cfg = get_pipeline_config().freshness
    as_of = as_of or date.today()
    last_bday = last_business_day(as_of)
    report = FreshnessReport(ok=True, as_of=as_of)

    # --- NAV ---
    nav_latest = _latest_nav_date()
    report.nav_latest = nav_latest
    if nav_latest is None:
        report.issues.append("NAV dataset missing (data/curated/nav_daily/).")
    else:
        lag = business_days_between(nav_latest, last_bday)
        if lag > cfg.max_nav_lag_bdays:
            report.issues.append(
                f"NAV stale: latest={nav_latest}, last_bday={last_bday}, "
                f"lag={lag} bdays > max={cfg.max_nav_lag_bdays}"
            )

    # --- Benchmarks ---
    bench = _latest_benchmark_dates()
    report.benchmark_latest = {k: v for k, v in bench.items() if v is not None}
    for slug, dt in bench.items():
        if dt is None:
            report.issues.append(f"Benchmark missing: ticker_slug={slug}")
            continue
        lag = business_days_between(dt, last_bday)
        if lag > cfg.max_bench_lag_bdays:
            report.issues.append(
                f"Benchmark stale: {slug} latest={dt}, "
                f"lag={lag} bdays > max={cfg.max_bench_lag_bdays}"
            )

    # --- Risk-free ---
    rf_latest = _latest_risk_free_date()
    report.risk_free_latest = rf_latest
    if rf_latest is None:
        report.issues.append("Risk-free series missing.")
    else:
        lag = (as_of - rf_latest).days
        if lag > cfg.max_rf_lag_days:
            report.issues.append(
                f"Risk-free stale: latest auction={rf_latest}, "
                f"lag={lag} days > max={cfg.max_rf_lag_days}"
            )

    # --- Scheme master ---
    sm_mtime = _scheme_master_mtime()
    report.scheme_master_mtime = sm_mtime
    if sm_mtime is None:
        report.issues.append("Scheme master not built.")
    else:
        lag = (as_of - sm_mtime).days
        if lag > cfg.max_scheme_master_lag_days:
            report.issues.append(
                f"Scheme master stale: built {sm_mtime}, "
                f"lag={lag} days > max={cfg.max_scheme_master_lag_days}"
            )

    report.ok = not report.issues
    if not report.ok:
        log.error("freshness.failed", issues=report.issues)
        if raise_on_fail:
            raise FreshnessError(
                "Freshness checks failed:\n  - " + "\n  - ".join(report.issues)
            )
    else:
        log.info(
            "freshness.passed",
            nav_latest=str(nav_latest),
            rf_latest=str(rf_latest),
            n_benchmarks=len(report.benchmark_latest),
        )
    return report
