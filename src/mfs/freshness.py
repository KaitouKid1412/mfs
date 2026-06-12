"""Freshness checks: assert every curated dataset is recent before compute/rank.

Compute and rank stages refuse to run if these checks don't pass. Thresholds are
configured under `freshness:` in pipeline.yaml.

Blocking-vs-advisory classification (B7): every check here gates the GLOBAL
MAX(date) of a table and is BLOCKING — a stale global max means the entire
source went dark for more than one publication cycle (systemic), so the
pipeline halts via ``raise_on_fail=True``. Per-fund / per-AMC gaps are NOT
this module's job: they stay in coverage Gate B (advisory, never halts), so
one AMC's missing PTR can't stop the run. That split is why e.g. quant's
deliberate annual-only PTR rows can never trip ``max_ptr_lag_days`` — the
global max is carried by every other AMC's monthly rows.

Indian business days approximated by skipping Saturday/Sunday only — the
`max_*_lag_bdays` thresholds carry enough slack to cover NSE/AMFI holidays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from mfs.config import get_pipeline_config
from mfs.db import queries as q
from mfs.errors import FreshnessError
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
    holdings_latest: date | None = None
    constituents_latest: date | None = None
    ptr_latest: date | None = None
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


def _gather_latest() -> dict:
    """Single Postgres round-trip for all freshness anchors."""
    try:
        return q.latest_dates()
    except Exception as e:  # noqa: BLE001
        log.warning("freshness.db_query_failed", err=str(e))
        return {"nav_latest": None, "rf_latest": None,
                "scheme_master_latest": None, "benchmarks": {}}


def check_freshness(as_of: date | None = None, *, raise_on_fail: bool = False) -> FreshnessReport:
    """Run every staleness check; return a FreshnessReport.

    If raise_on_fail=True and any check fails, raise FreshnessError after building
    the report (so the caller can include the full issue list in the error message).
    """
    cfg = get_pipeline_config().freshness
    as_of = as_of or date.today()
    last_bday = last_business_day(as_of)
    report = FreshnessReport(ok=True, as_of=as_of)
    latest = _gather_latest()

    # --- NAV ---
    nav_latest = latest["nav_latest"]
    report.nav_latest = nav_latest
    if nav_latest is None:
        report.issues.append("NAV dataset missing (nav_daily table empty).")
    else:
        lag = business_days_between(nav_latest, last_bday)
        if lag > cfg.max_nav_lag_bdays:
            report.issues.append(
                f"NAV stale: latest={nav_latest}, last_bday={last_bday}, "
                f"lag={lag} bdays > max={cfg.max_nav_lag_bdays}"
            )

    # --- Benchmarks ---
    bench = latest["benchmarks"]
    report.benchmark_latest = {t: d for t, d in bench.items() if d is not None}
    if not bench:
        report.issues.append("Benchmarks missing (benchmark_daily table empty).")
    for ticker, dt in bench.items():
        if dt is None:
            report.issues.append(f"Benchmark missing: {ticker}")
            continue
        lag = business_days_between(dt, last_bday)
        if lag > cfg.max_bench_lag_bdays:
            report.issues.append(
                f"Benchmark stale: {ticker} latest={dt}, "
                f"lag={lag} bdays > max={cfg.max_bench_lag_bdays}"
            )

    # --- Risk-free ---
    rf_latest = latest["rf_latest"]
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
    sm_latest = latest["scheme_master_latest"]
    report.scheme_master_mtime = sm_latest
    if sm_latest is None:
        report.issues.append("Scheme master not built (scheme_master table empty).")
    else:
        lag = (as_of - sm_latest).days
        if lag > cfg.max_scheme_master_lag_days:
            report.issues.append(
                f"Scheme master stale: last_seen={sm_latest}, "
                f"lag={lag} days > max={cfg.max_scheme_master_lag_days}"
            )

    # --- Holdings / constituents / PTR / ADV / AUM (Phase 2.2+ / 2.3+) ---
    # Each is independently togglable via its lag threshold in pipeline.yaml.
    # None = skip entirely.
    for label, threshold, getter, setter in (
        ("Holdings", cfg.max_holdings_lag_days, q.latest_holdings_date,
         lambda v: setattr(report, "holdings_latest", v)),
        ("Index constituents", cfg.max_constituents_lag_days,
         q.latest_constituents_date,
         lambda v: setattr(report, "constituents_latest", v)),
        ("Portfolio turnover", cfg.max_ptr_lag_days, q.latest_ptr_date,
         lambda v: setattr(report, "ptr_latest", v)),
        ("Stock ADV", cfg.max_stock_adv_lag_days, q.latest_stock_adv_date,
         lambda v: None),  # no separate field on report — query stays nominal
        ("Scheme AUM", cfg.max_aum_lag_days, q.latest_aum_date,
         lambda v: None),
    ):
        if threshold is None:
            continue
        try:
            latest = getter()
        except Exception as e:  # noqa: BLE001
            log.warning("freshness.query_failed", source=label, err=str(e))
            latest = None
        setter(latest)
        if latest is None:
            report.issues.append(
                f"{label} missing but max_{label.lower().replace(' ', '_')}"
                "_lag_days is set; ingest or set the threshold to null to skip."
            )
        else:
            lag = (as_of - latest).days
            if lag > threshold:
                report.issues.append(
                    f"{label} stale: latest={latest}, lag={lag} days > max={threshold}"
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
