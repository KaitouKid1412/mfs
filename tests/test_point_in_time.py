"""A1-12: point-in-time as_of bounds for PTR / holdings / AUM reads.

Three layers, all DB-free:
  1. the SQL itself carries the ``as_of_month <= %s`` bound (capturing fake
     connection);
  2. compute_phase2_for_scheme passes ``on_or_before=as_of`` through to the
     holdings/PTR/AUM readers, so a PTR row or holdings month dated AFTER
     as_of is provably ignored;
  3. shortlist._build_aum_map forwards ``on_or_before=as_of`` to
     latest_scheme_aum (a quarter published after the run's as_of cannot
     leak future AUM into a historical ranking).
"""

from __future__ import annotations

from datetime import date

import polars as pl

from mfs.compute import orchestrator
from mfs.db import queries as q
from mfs.rank import shortlist

AS_OF = date(2026, 6, 8)


# ---------------------------------------------------------------------------
# 1. SQL bound
# ---------------------------------------------------------------------------


class _CaptureResult:
    def fetchall(self):
        return []


class _CaptureConn:
    def __init__(self, captured: list):
        self._captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._captured.append((sql, params))
        return _CaptureResult()


def _capture_sql(monkeypatch) -> list:
    captured: list = []
    monkeypatch.setattr(q, "connect", lambda: _CaptureConn(captured))
    return captured


def test_holdings_for_scheme_sql_carries_on_or_before_bound(monkeypatch):
    captured = _capture_sql(monkeypatch)
    q.holdings_for_scheme("X", on_or_before=date(2026, 1, 31))
    sql, params = captured[0]
    assert "as_of_month <= %s" in sql
    assert params == ("X", date(2026, 1, 31))


def test_holdings_for_scheme_sql_unbounded_without_on_or_before(monkeypatch):
    captured = _capture_sql(monkeypatch)
    q.holdings_for_scheme("X")
    sql, params = captured[0]
    assert "as_of_month <=" not in sql
    assert params == ("X",)


def test_ptr_for_scheme_sql_carries_on_or_before_bound(monkeypatch):
    captured = _capture_sql(monkeypatch)
    q.portfolio_turnover_for_scheme("X", on_or_before=date(2026, 1, 31))
    sql, params = captured[0]
    assert "as_of_month <= %s" in sql
    assert params == ("X", date(2026, 1, 31))


def test_ptr_for_scheme_sql_unbounded_without_on_or_before(monkeypatch):
    captured = _capture_sql(monkeypatch)
    q.portfolio_turnover_for_scheme("X")
    sql, params = captured[0]
    assert "as_of_month <=" not in sql
    assert params == ("X",)


# ---------------------------------------------------------------------------
# 2. compute_phase2_for_scheme ignores data dated after as_of
# ---------------------------------------------------------------------------


def test_compute_phase2_ignores_ptr_and_holdings_after_as_of(monkeypatch):
    """Fixture has an April and a July month; with as_of in June, ptr_latest
    must be April's value and the aum_impact holdings month must be April
    (the July ISIN never reaches the ADV lookup)."""
    holdings_all = pl.DataFrame({
        "scheme_code": ["X", "X"],
        "security_name": ["April Co", "July Co"],
        "as_of_month": [date(2026, 4, 1), date(2026, 7, 1)],
        "weight_pct": [10.0, 10.0],
        "isin": ["INE_APRIL", "INE_JULY"],
        "instrument_type": ["Equity", "Equity"],
    })
    ptr_all = pl.DataFrame({
        "scheme_code": ["X", "X"],
        "as_of_month": [date(2026, 4, 1), date(2026, 7, 1)],
        "ptr": [1.0, 9.9],
    })
    captured: dict = {}

    def fake_holdings(code, on_or_before=None):
        captured["holdings_bound"] = on_or_before
        if on_or_before is None:
            return holdings_all
        return holdings_all.filter(pl.col("as_of_month") <= on_or_before)

    def fake_ptr(code, on_or_before=None):
        captured["ptr_bound"] = on_or_before
        if on_or_before is None:
            return ptr_all
        return ptr_all.filter(pl.col("as_of_month") <= on_or_before)

    def fake_aum(code, on_or_before=None):
        captured["aum_bound"] = on_or_before
        return (date(2026, 3, 31), 100.0)

    def fake_adv(isins, window_days=90):
        captured["adv_isins"] = list(isins)
        return pl.DataFrame()

    monkeypatch.setattr(q, "holdings_for_scheme", fake_holdings)
    monkeypatch.setattr(q, "portfolio_turnover_for_scheme", fake_ptr)
    monkeypatch.setattr(q, "latest_scheme_aum", fake_aum)
    monkeypatch.setattr(q, "stock_adv_history", fake_adv)

    row = orchestrator.compute_phase2_for_scheme(
        "X", beta_3y_std=None, r_squared_3y_mean=None,
        benchmark_ticker=None, as_of=AS_OF,
    )

    # The bound is threaded through to every reader...
    assert captured["holdings_bound"] == AS_OF
    assert captured["ptr_bound"] == AS_OF
    assert captured["aum_bound"] == AS_OF
    # ...and post-as_of data is ignored: July PTR (9.9) never wins,
    assert row["ptr_latest"] == 1.0
    # and the holdings month selected for aum_impact is April, not July.
    assert captured["adv_isins"] == ["INE_APRIL"]


# ---------------------------------------------------------------------------
# 3. shortlist._build_aum_map forwards the bound
# ---------------------------------------------------------------------------


def test_build_aum_map_ignores_quarters_after_as_of(monkeypatch):
    quarters = {date(2026, 3, 31): 50.0, date(2026, 6, 30): 999.0}
    calls: list = []

    def fake_aum(code, on_or_before=None):
        calls.append(on_or_before)
        eligible = {
            m: v for m, v in quarters.items()
            if on_or_before is None or m <= on_or_before
        }
        if not eligible:
            return None
        m = max(eligible)
        return (m, eligible[m])

    monkeypatch.setattr(shortlist.q, "latest_scheme_aum", fake_aum)
    scored = pl.DataFrame({"scheme_code": ["X"]})

    bounded = shortlist._build_aum_map(scored, as_of=date(2026, 4, 15))
    assert calls == [date(2026, 4, 15)]
    assert bounded == {"X": 50.0}  # the 2026-06-30 quarter is invisible

    unbounded = shortlist._build_aum_map(scored)
    assert unbounded == {"X": 999.0}  # None keeps current-run behavior
