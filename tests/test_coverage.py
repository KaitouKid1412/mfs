"""Tests for the per-source data-coverage contracts and gates (mfs.coverage).

These cover the engine WITHOUT a live DB:
  * the pure decision logic (`_classify`, `_stale_cutoff`, `_need_from`,
    `_bday_shift_back`) — the heart of gap detection;
  * `evaluate` / `run_gate` with the DB-boundary helpers monkeypatched, so the
    classify→result→gate integration is exercised on synthetic data;
  * the registry-completeness invariant (every schema table is contracted or
    explicitly out-of-contract) — what makes "for all metrics" durable;
  * the accepted-exception declaration (NIFTY100 ESG TRI).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from mfs import coverage as cov
from mfs.errors import CoverageError

# --- pure: need_from -------------------------------------------------------

def test_need_from_years_months_days():
    as_of = date(2026, 6, 8)
    assert cov._need_from(("years", 5), as_of) < date(2021, 6, 9)
    assert cov._need_from(("months", 12), as_of) < date(2025, 6, 9)
    assert cov._need_from(("days", 90), as_of) == date(2026, 3, 10)


# --- pure: business-day shift ----------------------------------------------

def test_bday_shift_back_skips_weekends():
    # Mon 2026-06-08 shifted back 5 business days → Mon 2026-06-01.
    assert cov._bday_shift_back(date(2026, 6, 8), 5) == date(2026, 6, 1)
    # Shifting back 1 bday from a Monday lands on the previous Friday.
    assert cov._bday_shift_back(date(2026, 6, 8), 1) == date(2026, 6, 5)


# --- pure: stale cutoff (per-cadence) --------------------------------------

class _Cfg:
    max_nav_lag_bdays = 5
    max_rf_lag_days = 14
    missing_attr = None


def test_stale_cutoff_bdays_uses_business_days():
    c = next(c for c in cov.CONTRACTS if c.table == "nav_daily")
    cutoff = cov._stale_cutoff(c, date(2026, 6, 8), _Cfg())
    assert cutoff == date(2026, 6, 1)


def test_stale_cutoff_days_uses_calendar_days():
    c = next(c for c in cov.CONTRACTS if c.table == "risk_free_daily")
    cutoff = cov._stale_cutoff(c, date(2026, 6, 8), _Cfg())
    assert cutoff == date(2026, 5, 25)  # 14 calendar days back


def test_ter_contract_registered_advisory_monthly():
    """C2: scheme_ter_monthly is an ADVISORY Gate-B monthly contract over the
    rankable entity set, keyed to the max_ter_lag_days freshness lag."""
    c = next(c for c in cov.CONTRACTS if c.table == "scheme_ter_monthly")
    assert c.severity == cov.ADVISORY
    assert c.gate == "B"
    assert c.cadence == cov.MONTHLY
    assert c.date_col == "as_of_month"
    assert c.entity_set == "rankable"
    assert c.lag_attr == "max_ter_lag_days"


def test_stale_cutoff_falls_back_to_default_when_lag_none():
    # A monthly contract whose configured lag is None must fall back to the
    # cadence default (45d), not crash.
    c = next(c for c in cov.CONTRACTS if c.table == "holdings_monthly")

    class NullCfg:
        max_holdings_lag_days = None

    cutoff = cov._stale_cutoff(c, date(2026, 6, 8), NullCfg())
    assert cutoff == date(2026, 6, 8) - timedelta(
        days=cov._DEFAULT_LAG_DAYS[cov.MONTHLY]
    )


# --- pure: classify matrix -------------------------------------------------

def _classify(**kw):
    base = dict(
        severity=cov.BLOCKING, need_back_unit="years",
        need_from=date(2021, 1, 1), have_from=date(2013, 1, 1),
        have_till=date(2026, 6, 7), stale_cutoff=date(2026, 6, 1),
        n_expected=None, n_present=None,
    )
    base.update(kw)
    return cov._classify(**base)


def test_classify_empty():
    assert _classify(have_till=None) == (cov.EMPTY, False)


def test_classify_stale():
    assert _classify(have_till=date(2026, 5, 1)) == (cov.STALE, False)


def test_classify_short_history():
    # history starts well after need_from (beyond the 7-day tolerance)
    status, ok = _classify(have_from=date(2022, 1, 1))
    assert status == cov.SHORT_HISTORY and ok is False


def test_classify_ok_when_full():
    assert _classify(n_expected=100, n_present=100) == (cov.OK, True)


def test_classify_blocking_partial_gap_above_floor_passes():
    # 80% coverage > 50% floor → reported as GAP but does NOT halt.
    status, ok = _classify(n_expected=100, n_present=80)
    assert status == cov.GAP and ok is True


def test_classify_blocking_catastrophic_floor_halts():
    # 30% coverage < 50% floor → ingest is broken → halt.
    status, ok = _classify(n_expected=100, n_present=30)
    assert status == cov.GAP and ok is False


def test_classify_advisory_never_halts():
    # Even an empty advisory source returns ok=True (it must not halt the run).
    status, ok = _classify(severity=cov.ADVISORY, have_till=None)
    assert status == cov.EMPTY and ok is True
    status, ok = _classify(severity=cov.ADVISORY, n_expected=100, n_present=0)
    assert ok is True


def test_classify_short_history_only_for_long_series():
    # A monthly (need_back unit != 'years') source must not be flagged
    # SHORT_HISTORY just because it starts after need_from.
    status, _ = _classify(need_back_unit="months", have_from=date(2025, 1, 1))
    assert status != cov.SHORT_HISTORY


# --- evaluate / run_gate with DB boundary monkeypatched --------------------

def _patch_db(monkeypatch, *, bounds, latest=None, entities=None):
    monkeypatch.setattr(cov, "_bounds", lambda t, c: bounds)
    if latest is not None:
        monkeypatch.setattr(cov, "_entity_latest", lambda t, e, c: latest)
    if entities is not None:
        monkeypatch.setattr(cov, "_entity_set", lambda name: entities)


def test_evaluate_detects_entity_gap(monkeypatch):
    """A known hole in entity coverage is reported as GAP with the right counts."""
    c = next(c for c in cov.CONTRACTS if c.table == "nav_daily")
    fresh = date(2026, 6, 7)
    entities = {f"S{i}" for i in range(10)}
    # only 6 of 10 have fresh data
    latest = {f"S{i}": fresh for i in range(6)}
    latest.update({f"S{i}": date(2026, 1, 1) for i in range(6, 10)})
    _patch_db(monkeypatch, bounds=(date(2013, 1, 1), fresh, 1000),
              latest=latest, entities=entities)
    r = cov.evaluate(c, date(2026, 6, 8), _Cfg())
    assert r.n_expected == 10
    assert r.n_present == 6
    assert r.status == cov.GAP


def test_evaluate_accepted_missing_shrinks_denominator(monkeypatch):
    """accepted_missing entities are removed from the expected set, so a source
    that is 'complete except the accepted exception' shows as OK, not GAP."""
    c = next(c for c in cov.CONTRACTS if c.table == "index_constituents_monthly")
    assert "NIFTY100 ESG TRI" in c.accepted_missing
    fresh = date(2026, 6, 1)
    entities = {"NIFTY 50 TRI", "NIFTY 500 TRI", "NIFTY100 ESG TRI"}
    # every ticker fresh EXCEPT the accepted-missing one
    latest = {"NIFTY 50 TRI": fresh, "NIFTY 500 TRI": fresh}
    _patch_db(monkeypatch, bounds=(date(2025, 1, 1), fresh, 50),
              latest=latest, entities=entities)
    r = cov.evaluate(c, date(2026, 6, 8), _Cfg())
    # denominator excludes the accepted exception → 2 expected, 2 present → OK
    assert r.n_expected == 2
    assert r.n_present == 2
    assert r.status == cov.OK


def test_run_gate_raises_on_blocking_failure(monkeypatch):
    """A blocking source that is empty must raise CoverageError (halt)."""
    monkeypatch.setattr(cov, "_bounds", lambda t, c: (None, None, 0))
    monkeypatch.setattr(cov, "_entity_latest", lambda t, e, c: {})
    monkeypatch.setattr(cov, "_entity_set", lambda name: {"S1", "S2"})

    class Cfg:
        max_nav_lag_bdays = 5
        max_bench_lag_bdays = 5
        max_rf_lag_days = 14
        max_scheme_master_lag_days = 1

    monkeypatch.setattr(
        cov, "get_pipeline_config",
        lambda: type("P", (), {"freshness": Cfg()})(),
    )
    with pytest.raises(CoverageError):
        cov.run_gate("A", as_of=date(2026, 6, 8), raise_on_block=True)


def test_run_gate_advisory_never_raises(monkeypatch):
    """Gate B (all advisory) must never raise even when every source is empty."""
    monkeypatch.setattr(cov, "_bounds", lambda t, c: (None, None, 0))
    monkeypatch.setattr(cov, "_entity_latest", lambda t, e, c: {})
    monkeypatch.setattr(cov, "_entity_set", lambda name: {"S1"})

    class Cfg:
        max_holdings_lag_days = 45
        max_ptr_lag_days = 45
        max_aum_lag_days = 45
        max_constituents_lag_days = 45
        max_stock_adv_lag_days = 7

    monkeypatch.setattr(
        cov, "get_pipeline_config",
        lambda: type("P", (), {"freshness": Cfg()})(),
    )
    report = cov.run_gate("B", as_of=date(2026, 6, 8), raise_on_block=True)
    assert report.blocking_failures == []
    assert len(report.advisory_gaps) == len(report.results)


# --- B15: contract-query failure → status=ERROR, never silent EMPTY ok=True --

def _patch_evaluate_raises(monkeypatch):
    def _boom(contract, as_of, cfg):
        raise RuntimeError("relation does not exist (broken gate SQL)")

    monkeypatch.setattr(cov, "evaluate", _boom)
    monkeypatch.setattr(
        cov, "get_pipeline_config",
        lambda: type("P", (), {"freshness": object()})(),
    )


def test_run_gate_query_error_blocking_is_error_and_fails_gate(monkeypatch):
    """An evaluation exception on a BLOCKING source is status=ERROR with
    ok=False — the gate fails instead of swallowing the broken query."""
    _patch_evaluate_raises(monkeypatch)
    report = cov.run_gate("A", as_of=date(2026, 6, 8), raise_on_block=False)
    assert report.results, "gate A must still produce one row per contract"
    assert all(r.status == cov.ERROR for r in report.results)
    assert all(not r.ok for r in report.results)
    assert len(report.blocking_failures) == len(report.results)
    with pytest.raises(CoverageError):
        report.raise_if_blocking()
    rendered = cov.render(report)
    assert "[ERROR]" in rendered
    assert "[EMPTY]" not in rendered
    assert "coverage query failed" in rendered


def test_run_gate_query_error_advisory_is_loud_but_never_halts(monkeypatch):
    """Advisory sources stay ok=True (advisory never halts) but the ERROR is
    rendered loudly — never a silent EMPTY that reads as a data gap."""
    _patch_evaluate_raises(monkeypatch)
    report = cov.run_gate("B", as_of=date(2026, 6, 8), raise_on_block=True)
    assert report.blocking_failures == []          # never halts the run
    assert all(r.status == cov.ERROR for r in report.results)
    assert all(r.ok for r in report.results)
    # ERROR rows surface as advisory gaps (status != OK), so the end-of-run
    # summary lists them too.
    assert len(report.advisory_gaps) == len(report.results)
    rendered = cov.render(report)
    assert "[ERROR]" in rendered
    assert "GATE EVALUATION ERROR" in rendered
    assert "NOT a data gap" in rendered


# --- interior-gap contract (nav_daily only) ---------------------------------

class _NavCfg:
    max_nav_lag_bdays = 5
    max_nav_interior_gap_days = 2


def _patch_healthy_nav(monkeypatch):
    """Patch the DB boundary so the nav_daily contract evaluates to OK before
    the interior-gap check; returns the nav contract."""
    fresh = date(2026, 6, 7)
    entities = {f"S{i}" for i in range(10)}
    _patch_db(monkeypatch, bounds=(date(2013, 1, 1), fresh, 1000),
              latest={e: fresh for e in entities}, entities=entities)
    return next(c for c in cov.CONTRACTS if c.table == "nav_daily")


def test_only_nav_contract_declares_interior_gap():
    nav = next(c for c in cov.CONTRACTS if c.table == "nav_daily")
    assert nav.interior_gap is True
    assert all(not c.interior_gap for c in cov.CONTRACTS if c.table != "nav_daily")


def test_interior_gap_over_threshold_blocks(monkeypatch):
    """3 sub-floor days > threshold 2 → INTERIOR_GAP, ok=False (blocking)."""
    c = _patch_healthy_nav(monkeypatch)
    gaps = [date(2026, 5, 25), date(2026, 5, 26), date(2026, 5, 27)]
    monkeypatch.setattr(cov, "_nav_interior_gap_days", lambda end, **kw: gaps)
    r = cov.evaluate(c, date(2026, 6, 8), _NavCfg())
    assert r.status == cov.INTERIOR_GAP
    assert r.ok is False
    assert "2026-05-25" in r.detail and "2026-05-27" in r.detail
    # remediation points at the FIRST missing date (inclusive re-fetch heals it)
    assert r.remediation == "mfs ingest navs --since 2026-05-25"


def test_interior_gap_at_threshold_passes(monkeypatch):
    """2 sub-floor days == threshold 2 (special-session allowance) → OK."""
    c = _patch_healthy_nav(monkeypatch)
    gaps = [date(2026, 5, 25), date(2026, 5, 26)]
    monkeypatch.setattr(cov, "_nav_interior_gap_days", lambda end, **kw: gaps)
    r = cov.evaluate(c, date(2026, 6, 8), _NavCfg())
    assert r.status == cov.OK
    assert r.ok is True


def test_interior_gap_skipped_when_threshold_null(monkeypatch):
    """max_nav_interior_gap_days=None disables the check entirely (the
    rollback switch): the helper must never be called."""
    c = _patch_healthy_nav(monkeypatch)

    def _boom(end, **kw):
        pytest.fail("_nav_interior_gap_days called despite threshold=None")

    monkeypatch.setattr(cov, "_nav_interior_gap_days", _boom)

    class NullCfg:
        max_nav_lag_bdays = 5
        max_nav_interior_gap_days = None

    r = cov.evaluate(c, date(2026, 6, 8), NullCfg())
    assert r.status == cov.OK
    assert r.ok is True


def test_interior_gap_helper_not_called_for_other_contracts(monkeypatch):
    """Contracts with interior_gap=False (everything but nav_daily) never run
    the NAV gap query."""
    c = next(c for c in cov.CONTRACTS if c.table == "benchmark_daily")
    fresh = date(2026, 6, 7)
    _patch_db(monkeypatch, bounds=(date(2013, 1, 1), fresh, 1000),
              latest={"NIFTY 50 TRI": fresh}, entities={"NIFTY 50 TRI"})

    def _boom(end, **kw):
        pytest.fail("_nav_interior_gap_days called for a non-interior_gap contract")

    monkeypatch.setattr(cov, "_nav_interior_gap_days", _boom)

    class Cfg:
        max_bench_lag_bdays = 5
        max_nav_interior_gap_days = 2

    r = cov.evaluate(c, date(2026, 6, 8), Cfg())
    assert r.status == cov.OK


# --- registry completeness (the "for all metrics" guarantee) ---------------

def test_every_schema_table_is_contracted_or_out_of_contract():
    """A new ingest source can't ship without a coverage contract: every
    CREATE TABLE in schema.sql must be either in CONTRACTS or OUT_OF_CONTRACT."""
    sql = Path("src/mfs/db/schema.sql").read_text()
    tables = set(re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?([a-z_]+)", sql))
    contracted = {c.table for c in cov.CONTRACTS}
    uncovered = tables - contracted - set(cov.OUT_OF_CONTRACT)
    assert not uncovered, (
        f"Schema tables with no coverage contract and not in OUT_OF_CONTRACT: "
        f"{sorted(uncovered)}. Add a Contract or list it in OUT_OF_CONTRACT."
    )


def test_no_stale_contract_references_a_missing_table():
    sql = Path("src/mfs/db/schema.sql").read_text()
    tables = set(re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?([a-z_]+)", sql))
    for c in cov.CONTRACTS:
        assert c.table in tables, f"Contract references unknown table {c.table!r}"


# --- rendering smoke -------------------------------------------------------

def _sample_report(gate, results):
    return cov.CoverageReport(as_of=date(2026, 6, 8), gate=gate, results=results)


def _result(**kw):
    base = dict(
        source="nav_daily", label="NAV (nav_daily)", cadence=cov.TRADING_DAY,
        severity=cov.BLOCKING, need_from=date(2021, 1, 1),
        have_from=date(2013, 1, 1), have_till=date(2026, 6, 7),
        fresh_by=date(2026, 6, 1), n_rows=1000, n_expected=665, n_present=665,
        status=cov.OK, ok=True, remediation="mfs ingest navs",
    )
    base.update(kw)
    return cov.CoverageResult(**base)


def test_render_contains_status_and_border():
    rep = _sample_report("A", [_result()])
    out = cov.render(rep)
    assert "GATE A" in out and "PASSED" in out
    assert "665/665 funds" in out


def test_render_shows_remediation_for_advisory_gap():
    r = _result(severity=cov.ADVISORY, status=cov.STALE, ok=True,
                detail="latest too old", remediation="mfs ingest managers")
    out = cov.render(_sample_report("B", [r]))
    assert "fix: mfs ingest managers" in out


def test_render_summary_reports_blocking_and_advisory():
    block = _result()
    adv = _result(source="holdings_monthly", severity=cov.ADVISORY,
                  status=cov.GAP, n_expected=665, n_present=600, ok=True)
    summary = cov.render_summary([
        _sample_report("A", [block]), _sample_report("B", [adv]),
    ])
    assert "BLOCKING : 1/1 sources OK" in summary
    assert "holdings_monthly" in summary
    assert "65 funds excluded" in summary
