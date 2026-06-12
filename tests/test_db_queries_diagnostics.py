"""F-3: diagnostics readers in mfs.db.queries (extracted from cli.py).

Pins the tri_cagr_sanity CAGR math and the OK/SUSPECT classification on both
sides of TRI_SANITY_MIN_CAGR, with the DB connection monkeypatched to return
fixed boundary closes.
"""

from __future__ import annotations

from datetime import date

import pytest

from mfs.db import queries as q


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._row


class _FakeConn:
    """Context-manager connection whose execute() replays scripted rows in
    call order — one queued row per expected execute()."""

    def __init__(self, rows):
        self._rows = list(rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        assert self._rows, f"unexpected extra query: {sql}"
        return _FakeResult(self._rows.pop(0))


def _patch_conn(monkeypatch, rows):
    monkeypatch.setattr(q, "connect", lambda: _FakeConn(rows))


def test_tri_sanity_threshold_constant_pinned():
    # The validate command's --help and classification both read this.
    assert q.TRI_SANITY_MIN_CAGR == 0.10


def test_tri_cagr_sanity_pins_cagr_math(monkeypatch):
    # 100 -> 200 over 2010-01-01..2015-01-01 (1826 days ~ 5y) -> ~0.1487.
    _patch_conn(monkeypatch, [
        (100.0, 200.0, date(2010, 1, 1), date(2015, 1, 1)),  # bounds row
        (100.0,),  # start close
        (200.0,),  # end close
    ])
    res = q.tri_cagr_sanity(date(2010, 1, 1))
    assert res is not None
    assert res.cagr == pytest.approx(0.1487, abs=1e-4)
    assert res.start_date == date(2010, 1, 1)
    assert res.end_date == date(2015, 1, 1)
    assert res.ok is True  # 0.1487 > TRI_SANITY_MIN_CAGR


def test_tri_cagr_sanity_ok_just_above_threshold(monkeypatch):
    # 100 -> 110.5 over ~1y -> ~0.105 > 0.10 -> OK.
    _patch_conn(monkeypatch, [
        (100.0, 110.5, date(2010, 1, 1), date(2011, 1, 1)),
        (100.0,),
        (110.5,),
    ])
    res = q.tri_cagr_sanity(date(2010, 1, 1))
    assert res is not None
    assert res.cagr > q.TRI_SANITY_MIN_CAGR
    assert res.ok is True


def test_tri_cagr_sanity_suspect_just_below_threshold(monkeypatch):
    # 100 -> 109 over ~1y -> ~0.090 < 0.10 -> SUSPECT (PR not TRI?).
    _patch_conn(monkeypatch, [
        (100.0, 109.0, date(2010, 1, 1), date(2011, 1, 1)),
        (100.0,),
        (109.0,),
    ])
    res = q.tri_cagr_sanity(date(2010, 1, 1))
    assert res is not None
    assert res.cagr < q.TRI_SANITY_MIN_CAGR
    assert res.ok is False


def test_tri_cagr_sanity_none_when_no_history(monkeypatch):
    _patch_conn(monkeypatch, [(None, None, None, None)])
    assert q.tri_cagr_sanity(date(2010, 1, 1)) is None


def test_nav_table_counts_passthrough(monkeypatch):
    _patch_conn(monkeypatch, [(28_374_292, 27_089)])
    assert q.nav_table_counts() == (28_374_292, 27_089)


def test_risk_free_gap_report_none_when_empty(monkeypatch):
    _patch_conn(monkeypatch, [(None, None)])
    assert q.risk_free_gap_report(date(2013, 1, 1)) is None


def test_risk_free_gap_report_unwraps_missing_dates(monkeypatch):
    _patch_conn(monkeypatch, [
        (date(2013, 4, 1), date(2026, 6, 1)),        # MIN/MAX rf
        [(date(2020, 3, 23),), (date(2020, 3, 24),)],  # missing date rows
        (61,),                                        # n_pre
    ])
    rep = q.risk_free_gap_report(date(2013, 1, 1))
    assert rep is not None
    assert rep.rf_min == date(2013, 4, 1)
    assert rep.rf_max == date(2026, 6, 1)
    assert rep.n_pre == 61
    assert rep.missing_dates == [date(2020, 3, 23), date(2020, 3, 24)]


def test_table_bounds_rejects_unknown_table():
    with pytest.raises(KeyError):
        q.table_bounds("evil; DROP TABLE nav_daily")


def test_table_count_rejects_unknown_table():
    with pytest.raises(KeyError):
        q.table_count("evil; DROP TABLE nav_daily")
