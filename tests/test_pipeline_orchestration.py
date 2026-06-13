"""Tests for mfs.pipeline stage orchestration (F-2).

Every stage callable is monkeypatched with a recorder so the wiring itself is
under test, not the stages: stage order, halt-on-required-failure, best-effort
continuation, blocking-gate semantics, advisory-lock release and gate-summary
rendering on the failure path, --skip-phase2 stage set, and the AMFI quarter
label picker. The CLI wrapper is exercised end-to-end via Typer's CliRunner
to pin exit codes (0 success / 2 on PipelineError).
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
from typer.testing import CliRunner

from mfs import coverage, freshness, pipeline
from mfs.compute import orchestrator
from mfs.db import connection
from mfs.errors import CoverageError, IngestError, PipelineError
from mfs.ingest import (
    amfi_aum, amfi_nav, amfi_ter, benchmarks, bhavcopy, constituents,
    fbil_tbill, holdings, managers, synthetic_hybrid,
)
from mfs.master import scheme_master
from mfs.rank import shortlist

AS_OF = date(2026, 6, 11)

# Stage order as wired in pipeline.run (gate entries are coverage gates).
# D9: constituents are DERIVED from the just-ingested tracker holdings, then
# ingested — both stages run after holdings and before Gate B.
FULL_ORDER = [
    "navs", "benchmarks", "hybrids", "tbill", "scheme_master", "gate_A",
    "amfi_aum", "amfi_ter", "managers", "bhavcopy", "holdings",
    "derive_constituents", "constituents", "gate_B",
    "freshness", "phase1", "rank_deep",
]
PHASE2_STAGES = [
    "amfi_aum", "amfi_ter", "managers", "bhavcopy", "holdings",
    "derive_constituents", "constituents", "gate_B",
]

RANK_RESULT = {
    "as_of": AS_OF.isoformat(),
    "out_dir": "/tmp/mfs-test-out/2026-06-11",
    "stage1": {"flexicap": "/tmp/mfs-test-out/2026-06-11/stage1/flexicap.csv"},
    "stage2": {
        "dir": "stage2", "n_survivors": 5, "n_dropped": 3,
        "coverage_file": "cov.csv", "dropped_file": "dropped.csv",
    },
    "stage3": {
        "dir": "stage3", "n_final": 4, "n_flagged": 1, "n_breach_pairs": 1,
        "breaches_file": "breaches.csv", "overlap_pairs_file": "pairs.csv",
    },
}


class FakeLock:
    """Records advisory-lock enter/exit; raise_on_enter simulates contention."""

    def __init__(self, raise_on_enter: bool = False):
        self.raise_on_enter = raise_on_enter
        self.entered = False
        self.exited = False

    def __enter__(self):
        if self.raise_on_enter:
            raise PipelineError("Another `mfs pipeline` run is already in progress")
        self.entered = True
        return None

    def __exit__(self, *exc):
        self.exited = True
        return False


class FakeReport:
    def __init__(self, blocking=()):
        self.blocking_failures = list(blocking)


class Harness:
    """All recorder state for one wired-up run."""

    def __init__(self):
        self.calls: list[str] = []
        self.kwargs: dict[str, dict] = {}
        self.lock = FakeLock()
        self.gate_reports = {"A": FakeReport(), "B": FakeReport()}
        self.summary_inputs: list[list] = []
        self.echoed: list[tuple[str, bool]] = []
        # name -> exception to raise when that stage runs
        self.failures: dict[str, Exception] = {}

    def echo(self, message: str = "", *, err: bool = False, **_kw) -> None:
        self.echoed.append((str(message), err))

    def err_lines(self) -> list[str]:
        return [m for m, err in self.echoed if err]


@pytest.fixture
def wired(monkeypatch) -> Harness:
    h = Harness()

    def make(name: str, retval=None):
        def fn(*args, **kwargs):
            h.calls.append(name)
            h.kwargs[name] = kwargs
            if name in h.failures:
                raise h.failures[name]
            return retval
        return fn

    monkeypatch.setattr(amfi_nav, "ingest_incremental", make("navs", (0, [])))
    monkeypatch.setattr(benchmarks, "ingest_all_known", make("benchmarks", {}))
    monkeypatch.setattr(synthetic_hybrid, "synthesize_all", make("hybrids", {}))
    monkeypatch.setattr(fbil_tbill, "ingest", make("tbill", 0))
    monkeypatch.setattr(scheme_master, "build", make("scheme_master"))
    monkeypatch.setattr(amfi_aum, "ingest_quarter", make("amfi_aum", {}))
    monkeypatch.setattr(amfi_ter, "ingest_month", make("amfi_ter", {}))
    monkeypatch.setattr(managers, "run_all", make("managers", {}))
    monkeypatch.setattr(bhavcopy, "ingest_recent", make("bhavcopy", {}))
    monkeypatch.setattr(constituents, "run_all", make("constituents", {}))
    monkeypatch.setattr(
        constituents, "derive_latest_month", make("derive_constituents", {})
    )
    monkeypatch.setattr(holdings, "run_all", make("holdings", {}))
    monkeypatch.setattr(freshness, "check_freshness", make("freshness"))
    monkeypatch.setattr(orchestrator, "run_phase1", make("phase1"))
    monkeypatch.setattr(shortlist, "rank_deep", make("rank_deep", RANK_RESULT))

    def fake_run_gate(gate, as_of=None, *, raise_on_block=True):
        h.calls.append(f"gate_{gate}")
        h.kwargs[f"gate_{gate}"] = {"as_of": as_of, "raise_on_block": raise_on_block}
        return h.gate_reports[gate]

    monkeypatch.setattr(coverage, "run_gate", fake_run_gate)
    monkeypatch.setattr(coverage, "render", lambda report: "<gate report>")

    def fake_render_summary(reports):
        h.summary_inputs.append(list(reports))
        return "<gate summary>"

    monkeypatch.setattr(coverage, "render_summary", fake_render_summary)
    monkeypatch.setattr(connection, "pipeline_lock", lambda: h.lock)
    return h


# --- stage sequencing -------------------------------------------------------

def test_full_run_stage_order_and_result(wired):
    result = pipeline.run(AS_OF, echo=wired.echo)

    assert wired.calls == FULL_ORDER
    assert isinstance(result, pipeline.PipelineResult)
    assert result.as_of == AS_OF
    assert result.out_dir == RANK_RESULT["out_dir"]
    assert result.stage1_files == RANK_RESULT["stage1"]
    assert result.stage2_counts["n_survivors"] == 5
    assert result.stage3_counts["n_final"] == 4
    assert result.gate_reports == [wired.gate_reports["A"], wired.gate_reports["B"]]
    # Lock held for the run and released; summary rendered exactly once.
    assert wired.lock.entered and wired.lock.exited
    assert wired.summary_inputs == [result.gate_reports]


def test_flags_thread_through_to_stages(wired):
    pipeline.run(AS_OF, full=True, allow_fallback=True, echo=wired.echo)

    assert wired.kwargs["benchmarks"] == {"full": True}
    assert wired.kwargs["tbill"] == {"allow_fallback": True}
    assert wired.kwargs["managers"] == {"force": True}
    assert wired.kwargs["bhavcopy"] == {"n_days": 75, "full": True}
    # AMFI quarter label derived from as_of (2026-06-11 → Q4-2026 published).
    assert wired.kwargs["gate_A"] == {"as_of": AS_OF, "raise_on_block": False}


def test_amfi_aum_stage_gets_latest_quarter_label(wired, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        amfi_aum, "ingest_quarter",
        lambda label: (wired.calls.append("amfi_aum"), seen.update(label=label), {})[-1],
    )
    pipeline.run(AS_OF, echo=wired.echo)
    assert seen["label"] == "Q4-2026"


# --- failure semantics ------------------------------------------------------

def test_required_stage_failure_halts_before_later_stages(wired):
    wired.failures["benchmarks"] = IngestError("AMFI down")

    with pytest.raises(IngestError, match="AMFI down"):
        pipeline.run(AS_OF, echo=wired.echo)

    # Nothing after the failing stage ran.
    assert wired.calls == ["navs", "benchmarks"]
    # Lock released on the failure path; no gate ran → no summary rendered.
    assert wired.lock.exited
    assert wired.summary_inputs == []
    assert any("FAIL at ingest benchmarks" in m for m in wired.err_lines())


def test_best_effort_stage_failure_continues(wired):
    wired.failures["constituents"] = IngestError("no manual CSVs")

    result = pipeline.run(AS_OF, echo=wired.echo)

    assert wired.calls == FULL_ORDER  # constituents failed but everything else ran
    assert isinstance(result, pipeline.PipelineResult)
    assert any(
        "WARN at ingest constituents (derived CSVs)" in m for m in wired.err_lines()
    )


def test_derive_constituents_is_required_and_halts(wired):
    """D9: a derivation failure is systemic (local DB + CSV writes only) and
    must halt before the constituents ingest / Gate B."""
    wired.failures["derive_constituents"] = IngestError(
        "derive constituents: holdings_monthly is empty"
    )

    with pytest.raises(IngestError, match="holdings_monthly is empty"):
        pipeline.run(AS_OF, echo=wired.echo)

    assert wired.calls == ["navs", "benchmarks", "hybrids", "tbill",
                           "scheme_master", "gate_A", "amfi_aum", "amfi_ter",
                           "managers", "bhavcopy", "holdings",
                           "derive_constituents"]
    assert wired.lock.exited
    assert any(
        "FAIL at derive constituents (latest month)" in m
        for m in wired.err_lines()
    )


def test_hybrid_synthesis_runtimeerror_translated_to_ingest_error(wired):
    wired.failures["hybrids"] = RuntimeError("missing NIFTY 50 TRI sleeve")

    with pytest.raises(IngestError, match="Hybrid TRI synthesis failed"):
        pipeline.run(AS_OF, echo=wired.echo)

    assert wired.calls == ["navs", "benchmarks", "hybrids"]
    assert wired.lock.exited


def _http_403() -> httpx.HTTPStatusError:
    url = "https://archives.nseindia.com/products/content/sec_bhavdata_full_01062026.csv"
    req = httpx.Request("GET", url)
    return httpx.HTTPStatusError(
        f"Client error '403 Forbidden' for url '{url}'",
        request=req,
        response=httpx.Response(403, request=req),
    )


# --- B8 backstop: raw 4xx / RuntimeError leaked by a stage ---------------------

def test_required_stage_raw_4xx_halts_cleanly_as_pipeline_error(wired):
    wired.failures["bhavcopy"] = _http_403()

    with pytest.raises(PipelineError, match="403 Forbidden") as excinfo:
        pipeline.run(AS_OF, echo=wired.echo)

    # The raw httpx error is chained, not propagated.
    assert isinstance(excinfo.value.__cause__, httpx.HTTPStatusError)
    # Halted at bhavcopy; nothing after it ran. Lock released.
    assert wired.calls == ["navs", "benchmarks", "hybrids", "tbill",
                           "scheme_master", "gate_A", "amfi_aum", "amfi_ter",
                           "managers", "bhavcopy"]
    assert wired.lock.exited
    fail = [m for m in wired.err_lines() if "FAIL at ingest bhavcopy" in m]
    assert fail and "HTTPStatusError" in fail[0]


def test_required_stage_bare_runtimeerror_halts_cleanly(wired):
    wired.failures["scheme_master"] = RuntimeError(
        "Empty AMFI NAVAll snapshot; cannot build scheme master"
    )

    with pytest.raises(PipelineError, match="Empty AMFI NAVAll snapshot") as excinfo:
        pipeline.run(AS_OF, echo=wired.echo)

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert wired.calls == ["navs", "benchmarks", "hybrids", "tbill", "scheme_master"]
    assert wired.lock.exited
    assert any("FAIL at build scheme-master" in m for m in wired.err_lines())


def test_best_effort_stage_bare_runtimeerror_continues(wired):
    wired.failures["constituents"] = RuntimeError("manual CSV dir unreadable")

    result = pipeline.run(AS_OF, echo=wired.echo)

    assert wired.calls == FULL_ORDER  # constituents failed but everything else ran
    assert isinstance(result, pipeline.PipelineResult)
    warn = [m for m in wired.err_lines() if "WARN at ingest constituents" in m]
    assert warn and "RuntimeError" in warn[0]


def test_gate_a_block_halts_before_phase2_and_renders_summary(wired):
    wired.gate_reports["A"] = FakeReport(blocking=["nav_daily"])

    with pytest.raises(CoverageError, match="Gate A"):
        pipeline.run(AS_OF, echo=wired.echo)

    # Halted at Gate A: amfi_aum / managers / anything later never ran.
    assert wired.calls == ["navs", "benchmarks", "hybrids", "tbill",
                           "scheme_master", "gate_A"]
    # Lock __exit__ on the failure path, HALTING message on stderr, and the
    # gate summary is still rendered (data-quality posture never silent).
    assert wired.lock.exited
    assert any("HALTING (exit 2) — Gate A" in m for m in wired.err_lines())
    assert wired.summary_inputs == [[wired.gate_reports["A"]]]


def test_gate_b_block_is_advisory_and_does_not_halt(wired):
    wired.gate_reports["B"] = FakeReport(blocking=["holdings_monthly"])

    result = pipeline.run(AS_OF, echo=wired.echo)

    assert wired.calls == FULL_ORDER
    assert isinstance(result, pipeline.PipelineResult)


def test_lock_contention_raises_pipeline_error(wired):
    wired.lock.raise_on_enter = True

    with pytest.raises(PipelineError, match="already in progress"):
        pipeline.run(AS_OF, echo=wired.echo)

    assert wired.calls == []  # no stage ran
    assert any("already in progress" in m for m in wired.err_lines())


# --- skip_phase2 ------------------------------------------------------------

def test_skip_phase2_skips_exactly_the_phase2_stages(wired):
    pipeline.run(AS_OF, skip_phase2=True, echo=wired.echo)

    expected = [s for s in FULL_ORDER if s not in PHASE2_STAGES]
    assert wired.calls == expected
    assert set(FULL_ORDER) - set(wired.calls) == set(PHASE2_STAGES)


# --- CLI wrapper exit behavior ----------------------------------------------

def test_cli_pipeline_success_exit_0(wired):
    from mfs.cli import app

    result = CliRunner().invoke(app, ["pipeline", "--as-of", "2026-06-11"])

    assert result.exit_code == 0, result.output
    assert "pipeline done. as_of=2026-06-11" in result.output
    assert "stage 2: survivors=5 dropped=3" in result.output
    assert "stage 3: final=4 overlap-flagged=1" in result.output


def test_cli_pipeline_required_failure_exit_2(wired):
    from mfs.cli import app

    wired.failures["benchmarks"] = IngestError("AMFI down")
    result = CliRunner().invoke(app, ["pipeline", "--as-of", "2026-06-11"])

    assert result.exit_code == 2


def test_cli_pipeline_gate_a_block_exit_2(wired):
    from mfs.cli import app

    wired.gate_reports["A"] = FakeReport(blocking=["nav_daily"])
    result = CliRunner().invoke(app, ["pipeline", "--as-of", "2026-06-11"])

    assert result.exit_code == 2


def test_cli_pipeline_bare_runtimeerror_exit_2_no_traceback(wired):
    # B8 backstop end-to-end: a leaked RuntimeError exits 2 via the clean
    # PipelineError path (a raw escape would surface as CliRunner exit 1
    # with result.exception set to the RuntimeError).
    from mfs.cli import app

    wired.failures["scheme_master"] = RuntimeError("Empty AMFI NAVAll snapshot")
    result = CliRunner().invoke(app, ["pipeline", "--as-of", "2026-06-11"])

    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_cli_pipeline_raw_4xx_exit_2_no_traceback(wired):
    from mfs.cli import app

    wired.failures["bhavcopy"] = _http_403()
    result = CliRunner().invoke(app, ["pipeline", "--as-of", "2026-06-11"])

    assert result.exit_code == 2
    assert "Traceback" not in result.output


# --- _latest_amfi_quarter_label ----------------------------------------------

@pytest.mark.parametrize(
    "d, expected",
    [
        # Q4-2026 (Jan-Mar 2026) ended 2026-03-31; published by 2026-05-15.
        (date(2026, 6, 11), "Q4-2026"),
        # On 2026-02-01 the Oct-Dec 2025 quarter (Q3-2025, ended 2025-12-31)
        # is only 32 days old — inside AMFI's 45-day publication window — so
        # the latest published quarter is Jul-Sep 2025 (Q2-2025).
        (date(2026, 2, 1), "Q2-2025"),
    ],
)
def test_latest_amfi_quarter_label_pinned(d, expected):
    assert pipeline._latest_amfi_quarter_label(d) == expected


def test_latest_amfi_quarter_label_45_day_boundary():
    quarter_end = date(2026, 3, 31)  # Q4-2026
    # Exactly quarter_end + 45 days: the quarter counts as published.
    assert pipeline._latest_amfi_quarter_label(
        quarter_end + timedelta(days=45)
    ) == "Q4-2026"
    # One day earlier it does not — falls back to the previous quarter.
    assert pipeline._latest_amfi_quarter_label(
        quarter_end + timedelta(days=44)
    ) == "Q3-2025"
