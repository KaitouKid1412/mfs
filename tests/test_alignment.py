"""A1-1 non-finite guard in compute.alignment.

A zero (or negative) NAV row makes ln(nav) emit -inf/NaN, and the 1-day shift
in the log-return diff propagates the poison to the NEXT row too. Downstream
drop_nulls() in sortino/capture/info_ratio/alpha does NOT drop NaN/inf, so a
single poisoned row used to NaN every rolling metric (audit finding 2a:
clean 0.0485 sortino -> NaN). These tests pin the guard: zero-NAV rows produce
NULL log returns (never inf/NaN), and rolling metrics on the same frame stay
finite.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from mfs.compute import alignment
from mfs.compute.capture import rolling_capture
from mfs.compute.sortino import rolling_sortino

N_DAYS = 4 * 252  # ~4 years of trading days: enough for 3y rolling windows
ZERO_IDX = 600  # the poisoned NAV row


def _business_days(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@pytest.fixture()
def fake_db(monkeypatch):
    """Patch the queries module behind alignment with a synthetic universe.

    Fund NAV has exactly one nav=0.0 row at ZERO_IDX (an AMFI segregated-
    portfolio style zero), positive everywhere else.
    """
    rng = np.random.default_rng(42)
    dates = _business_days(date(2021, 1, 4), N_DAYS)

    fund_rets = rng.normal(0.0005, 0.01, N_DAYS)
    bench_rets = rng.normal(0.0004, 0.009, N_DAYS)
    nav = 100.0 * np.exp(np.cumsum(fund_rets))
    close = 10_000.0 * np.exp(np.cumsum(bench_rets))
    nav[ZERO_IDX] = 0.0  # the poison row

    cal = pl.DataFrame({"date": dates}).with_columns(pl.col("date").cast(pl.Date))
    nav_df = pl.DataFrame({"date": dates, "nav": nav}).with_columns(
        pl.col("date").cast(pl.Date)
    )
    bench_df = pl.DataFrame({"date": dates, "close": close}).with_columns(
        pl.col("date").cast(pl.Date)
    )
    rf_df = pl.DataFrame(
        {"date": dates, "rate_daily": [0.00025] * N_DAYS}
    ).with_columns(pl.col("date").cast(pl.Date))

    monkeypatch.setattr(alignment.q, "master_calendar", lambda: cal)
    monkeypatch.setattr(alignment.q, "nav_series", lambda scheme_code: nav_df)
    monkeypatch.setattr(alignment.q, "benchmark_series", lambda ticker: bench_df)
    monkeypatch.setattr(alignment.q, "risk_free_series", lambda: rf_df)
    return {"dates": dates}


def test_zero_nav_yields_null_log_returns_not_inf_nan(fake_db):
    aligned = alignment.align_scheme("TEST", "NIFTY 50 TRI")
    assert not aligned.is_empty()

    poison_date = fake_db["dates"][ZERO_IDX]
    next_date = fake_db["dates"][ZERO_IDX + 1]

    at = aligned.filter(pl.col("date") == poison_date)["fund_log_ret"].to_list()
    after = aligned.filter(pl.col("date") == next_date)["fund_log_ret"].to_list()
    # ln(0/prev) = -inf and ln(next/0) = +inf without the guard; both must be null.
    assert at == [None]
    assert after == [None]

    # No non-finite value may survive anywhere in any log-return column.
    for col in ("fund_log_ret", "bench_log_ret", "rate_daily_log"):
        vals = [v for v in aligned[col].to_list() if v is not None]
        assert all(math.isfinite(v) for v in vals), f"non-finite value in {col}"


def test_rolling_metrics_stay_finite_despite_zero_nav(fake_db):
    """Reproduces the audit's clean->NaN case and asserts it no longer NaNs."""
    aligned = alignment.align_scheme("TEST", "NIFTY 50 TRI")

    sortino = rolling_sortino(aligned)
    assert sortino is not None
    assert math.isfinite(sortino)

    cap = rolling_capture(aligned)
    for key in ("capture_up", "capture_down", "capture_efficiency"):
        assert cap[key] is not None
        assert math.isfinite(cap[key]), f"{key} is non-finite"


def test_clean_series_unaffected_by_guard(fake_db, monkeypatch):
    """A frame with no poison passes through the guard byte-identical."""
    dates = fake_db["dates"]
    rng = np.random.default_rng(7)
    nav = 50.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.008, N_DAYS)))
    nav_df = pl.DataFrame({"date": dates, "nav": nav}).with_columns(
        pl.col("date").cast(pl.Date)
    )
    monkeypatch.setattr(alignment.q, "nav_series", lambda scheme_code: nav_df)

    aligned = alignment.align_scheme("CLEAN", "NIFTY 50 TRI")
    fund = aligned["fund_log_ret"].to_list()
    # First row has no prior NAV -> null; everything else finite and non-null.
    assert fund[0] is None
    assert all(v is not None and math.isfinite(v) for v in fund[1:])
