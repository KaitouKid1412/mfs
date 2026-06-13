"""Pipeline orchestration: the full ingest → build → compute → rank-deep run.

Extracted from cli.py (F-2) so stage wiring is unit-testable and Stage-2
remediation tasks edit this module instead of the Typer layer. ``run`` raises
``PipelineError`` (never ``typer.Exit``); the CLI wrapper translates that into
an exit code. All progress / failure / gate output goes through the injected
``echo`` callable (``typer.echo`` in production, a recorder in tests) so the
stdout/stderr split is preserved exactly.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Callable

from mfs.errors import CoverageError, IngestError, PipelineError
from mfs.utils.logging import get_logger

if TYPE_CHECKING:
    from mfs.coverage import CoverageReport

log = get_logger("mfs.pipeline")


def _default_echo(message: str = "", *, err: bool = False) -> None:
    """Stdout/stderr-aware default for ``echo`` (mirrors typer.echo's split)."""
    print(message, file=sys.stderr if err else sys.stdout)


def timed_call(
    name: str,
    fn: Callable[[], Any],
    timings: list[tuple[str, float]],
) -> Any:
    """Run ``fn()``, appending ``(name, elapsed_seconds)`` to ``timings``.

    The timing record is appended whether ``fn`` returns or raises — a failing
    stage still shows how long it ran before failing, and the exception
    propagates unchanged. On success a machine-readable structlog event
    ``pipeline.stage.done`` is emitted with the stage name and seconds (C1:
    the measurement basis for the C2-C6 before/after acceptance).
    """
    t0 = time.perf_counter()
    try:
        result = fn()
    except BaseException:
        timings.append((name, time.perf_counter() - t0))
        raise
    elapsed = time.perf_counter() - t0
    timings.append((name, elapsed))
    log.info("pipeline.stage.done", stage=name, seconds=round(elapsed, 3))
    return result


def render_stage_timings(timings: list[tuple[str, float]]) -> str:
    """Per-stage wall-clock summary table printed at the end of every run."""
    width = max([len(n) for n, _ in timings] + [len("TOTAL")])
    lines = ["", "STAGE TIMING SUMMARY", "-" * (width + 12)]
    for name, secs in timings:
        lines.append(f"  {name:<{width}s} {secs:>8.1f}s")
    lines.append("-" * (width + 12))
    lines.append(f"  {'TOTAL':<{width}s} {sum(s for _, s in timings):>8.1f}s")
    return "\n".join(lines)


@dataclass(frozen=True)
class PipelineResult:
    """Summary of a successful end-to-end run.

    Consumed by the CLI wrapper for the final summary lines and (F-10) by the
    run-manifest writer. ``stage1_files`` / ``stage2_counts`` / ``stage3_counts``
    carry the corresponding sub-dicts of ``shortlist.rank_deep``'s result.
    """

    as_of: date
    out_dir: str
    stage1_files: dict[str, str]
    stage2_counts: dict[str, Any]
    stage3_counts: dict[str, Any]
    gate_reports: list[CoverageReport] = field(default_factory=list)


def _latest_amfi_quarter_label(d: date) -> str:
    """Pick the most recently published AMFI AAUM quarter for date `d`.

    AMFI publishes ~45 days after quarter end. Walk back through quarter
    boundaries and pick the most recent one that ended at least 45 days
    before `d` (so AMFI has had time to publish).

    Returns labels like 'Q4-2026' (Jan-Mar 2026) or 'Q3-2025' (Oct-Dec 2025).
    Label convention matches ``amfi_aum.quarter_from_label``: the year is
    the calendar year of the quarter's END month; Q1=Apr-Jun, Q2=Jul-Sep,
    Q3=Oct-Dec, Q4=Jan-Mar.
    """
    from datetime import date as _date, timedelta as _td

    cutoff = d - _td(days=45)
    # Quarter-end dates ordered most-recent-first relative to d. For each
    # we also know its label (q_num, end_year).
    candidates: list[tuple[_date, int, int]] = []
    for cy in (d.year, d.year - 1, d.year - 2):
        candidates.extend([
            (_date(cy, 3, 31), 4, cy),    # Jan-Mar  -> Q4-cy
            (_date(cy, 6, 30), 1, cy),    # Apr-Jun  -> Q1-cy
            (_date(cy, 9, 30), 2, cy),    # Jul-Sep  -> Q2-cy
            (_date(cy, 12, 31), 3, cy),   # Oct-Dec  -> Q3-cy
        ])
    candidates.sort(key=lambda t: t[0], reverse=True)
    for qe, q_num, cy in candidates:
        if qe <= cutoff:
            return f"Q{q_num}-{cy}"
    # Should not reach here unless d is in the distant past.
    return f"Q4-{d.year - 2}"


def run(
    as_of: date,
    *,
    full: bool = False,
    skip_phase2: bool = False,
    allow_fallback: bool = False,
    echo: Callable[..., None] = _default_echo,
) -> PipelineResult:
    """Run the full pipeline end-to-end: ingest → build → compute → rank-deep.

    Phase 1 (NAV, benchmarks, T-bill, scheme-master) is required.
    Factsheet ingest (holdings/PTR/AUM via the per-AMC factsheet adapters)
    plus bhavcopy plus Phase 3.C per-scheme portfolio Excels are required
    for Stage 2/3 of rank-deep to produce non-trivial output. Constituents
    are DERIVED in-pipeline from the just-ingested index-tracker holdings
    (D9: required stage), then the CSV ingest upserts them (best-effort —
    it no-ops cleanly if the directory is empty).

    Halts at the first REQUIRED stage that fails by raising ``PipelineError``
    (the original ``IngestError``/``PipelineError`` from the stage, or
    ``CoverageError`` on a blocking gate breach). No partial / stale data
    ever reaches compute or rank-deep.
    """
    import httpx

    from mfs import coverage
    from mfs.compute import orchestrator
    from mfs.db.connection import pipeline_lock
    from mfs.freshness import check_freshness
    from mfs.ingest import (
        amfi_aum, amfi_nav, amfi_ter, benchmarks, bhavcopy as bhavcopy_mod,
        constituents, fbil_tbill, holdings, managers, synthetic_hybrid,
    )
    from mfs.master import scheme_master
    from mfs.rank import shortlist

    d = as_of
    stage_timings: list[tuple[str, float]] = []

    def _stage(name: str, fn, *, required: bool = True):
        echo(f"[pipeline] {name} ...")
        try:
            result = timed_call(name, fn, stage_timings)
        except (PipelineError, IngestError) as e:
            if required:
                echo(f"[pipeline] FAIL at {name}:\n{e}", err=True)
                raise
            echo(f"[pipeline] WARN at {name} (best-effort, continuing):\n{e}", err=True)
            return None
        except (httpx.HTTPStatusError, RuntimeError) as e:
            # B8 backstop: a stage leaked a raw 4xx or bare RuntimeError that
            # its ingester should have translated to IngestError. Render the
            # same clean failure output (no traceback) and halt via
            # PipelineError so the CLI wrapper still exits 2.
            # (PipelineError subclasses RuntimeError, but the clause above
            # catches it first, so only genuinely untranslated errors land here.)
            msg = f"{type(e).__name__}: {e}"
            if required:
                echo(f"[pipeline] FAIL at {name}:\n{msg}", err=True)
                raise PipelineError(f"{name}: {msg}") from e
            echo(f"[pipeline] WARN at {name} (best-effort, continuing):\n{msg}", err=True)
            return None
        echo(f"[pipeline] {name} done in {stage_timings[-1][1]:.1f}s")
        return result

    gate_reports: list[CoverageReport] = []

    def _coverage_gate(gate: str, *, halt_on_block: bool):
        """Run a coverage gate, print the bordered report to stdout (NOT via
        structlog, so the table isn't flattened into one line), and halt the
        run on a BLOCKING breach. The full report is rendered whether it passes
        or fails — the data-quality posture is never silent."""
        name = f"coverage gate {gate}"
        echo(f"[pipeline] {name} ...")

        def _run_gate():
            report = coverage.run_gate(gate, as_of=d, raise_on_block=False)
            echo(coverage.render(report))
            gate_reports.append(report)
            if halt_on_block and report.blocking_failures:
                echo(
                    f"[pipeline] HALTING (exit 2) — Gate {gate} blocking coverage "
                    f"failure; refusing to compute metrics on incomplete inputs.",
                    err=True,
                )
                raise CoverageError(
                    f"Gate {gate} blocking coverage failure; refusing to compute "
                    f"metrics on incomplete inputs."
                )
            return report

        report = timed_call(name, _run_gate, stage_timings)
        echo(f"[pipeline] {name} done in {stage_timings[-1][1]:.1f}s")
        return report

    # Serialize concurrent pipeline runs with a Postgres advisory lock (released
    # automatically if this process dies, so a killed run never strands it).
    try:
        _lock = pipeline_lock()
        _lock.__enter__()
    except PipelineError as e:
        echo(f"[pipeline] {e}", err=True)
        raise

    try:
        # ----- Phase 1: required base data -----
        _stage("ingest navs (incremental)", amfi_nav.ingest_incremental)
        _stage("ingest benchmarks", lambda: benchmarks.ingest_all_known(full=full))
        # Synthesize the 3 hybrid TRIs (Hybrid 50:50, 65:35, Equity Savings) from
        # the now-fresh NIFTY 50 TRI sleeve + risk-free. These are NOT on NSE's
        # public TRI endpoint, so ingest_all_known never produces them — without
        # this step they go stale and the freshness gate halts the run. Mirrors
        # what the standalone `mfs ingest benchmarks` command already does.
        def _synthesize_hybrids():
            # synthesize_* raises RuntimeError/ValueError on missing components;
            # convert to IngestError so a failure halts cleanly via _stage rather
            # than escaping as an uncaught traceback.
            try:
                return synthetic_hybrid.synthesize_all()
            except (RuntimeError, ValueError) as e:
                raise IngestError(f"Hybrid TRI synthesis failed: {e}") from e

        _stage("synthesize hybrid TRIs", _synthesize_hybrids)
        _stage("ingest tbill", lambda: fbil_tbill.ingest(allow_fallback=allow_fallback))
        _stage("build scheme-master", scheme_master.build)

        # ----- Coverage Gate A (BLOCKING): NAV / benchmarks / risk-free /
        # scheme-master. Fails fast HERE — before the expensive ~50-AMC Phase-2
        # scrape — so broken base data costs seconds, not 15+ minutes. -----
        _coverage_gate("A", halt_on_block=True)

        if not skip_phase2:
            # ----- AMFI quarterly AAUM (per-scheme, all AMCs in one shot) -----
            # Authoritative SEBI source covering ~98% of the ranked universe with
            # a single GET. Replaces per-AMC factsheet AUM extraction.
            _stage(
                "ingest amfi aaum (latest quarter)",
                lambda: amfi_aum.ingest_quarter(_latest_amfi_quarter_label(d)),
            )

            # ----- AMFI monthly TER (latest published month, all AMCs) -----
            # Display column + Stage-2 tiebreaker only (C2). Advisory: a TER
            # fetch miss must not halt the run (it never gates ranking), unlike
            # the Gate-A base data.
            _stage(
                "ingest amfi ter (latest month)",
                lambda: amfi_ter.ingest_month(),
                required=False,
            )

            # ----- Factsheet ingest: holdings + PTR (AUM now sourced from AMFI) -----
            _stage("ingest managers (all AMCs)", lambda: managers.run_all(force=full))

            # ----- Phase 2.3.B: NSE bhavcopy for stock ADV (needed by AUM Impact) -----
            _stage(
                "ingest bhavcopy (last 75d)",
                lambda: bhavcopy_mod.ingest_recent(n_days=75, full=full),
            )

            # ----- Phase 3.C: per-AMC monthly portfolio Excels (HDFC + SBI + Nippon) -----
            # force=full (B9): --full re-downloads the current data month's
            # ~640 Excels, re-parses, and re-writes (shrinkage guard overridden).
            _stage(
                "ingest holdings (all registered AMCs)",
                lambda: holdings.run_all(ym=None, force=full),
            )

            # ----- D9: derive constituent weights for the latest holdings month
            # from the just-ingested index-tracker portfolios, so
            # index_constituents_monthly accrues monthly (active_share needs
            # >=3 matched months). Local-only (DB + CSVs); per-index misses are
            # SKIPPED rows, so a raise here is systemic and halts. -----
            _stage(
                "derive constituents (latest month)",
                constituents.derive_latest_month,
            )

            # ----- Phase 2.2.B: ingest constituent weight CSVs (the freshly
            # derived month plus any operator backfills) -----
            _stage(
                "ingest constituents (derived CSVs)",
                lambda: constituents.run_all(),
                required=False,
            )

            # ----- Coverage Gate B (ADVISORY): holdings / PTR / AAUM /
            # constituents / stock-ADV. Gate B is the PER-FUND net for these
            # signals: gaps are reported loudly and the affected funds are
            # excluded downstream; the run continues. Whole-source staleness is
            # instead BLOCKING via the freshness thresholds (B7) below. -----
            _coverage_gate("B", halt_on_block=False)

        # ----- freshness gate before compute -----
        _stage("freshness check", lambda: check_freshness(as_of=d, raise_on_fail=True))

        # Phase 1 compute on every eligible scheme. Phase 2 compute is deferred
        # to rank-deep, which restricts it to the Stage 1 top-N candidate pool.
        _stage("compute phase1", lambda: orchestrator.run_phase1(as_of=d))

        # ----- rank-deep (Stage 1 + 2 + 3) — runs Phase 2 compute internally -----
        result = _stage(
            "rank-deep (stage 1 + 2 + 3)",
            lambda: shortlist.rank_deep(as_of=d),
        )
    finally:
        # Print the data-quality posture on EVERY run (success, advisory gaps,
        # or a Gate A halt) — it's never silent. Guarded so a rendering error
        # can't mask the real pipeline exception.
        if gate_reports:
            try:
                echo(coverage.render_summary(gate_reports))
            except Exception as _e:  # noqa: BLE001
                echo(f"[pipeline] (summary render failed: {_e})", err=True)
        _lock.__exit__(None, None, None)

    if result is None:
        # rank-deep is a required stage: _stage either returns its dict or
        # raises, so this is unreachable in practice — guard defensively.
        raise PipelineError("rank-deep produced no result")
    return PipelineResult(
        as_of=d,
        out_dir=result["out_dir"],
        stage1_files=result["stage1"],
        stage2_counts=result["stage2"],
        stage3_counts=result["stage3"],
        gate_reports=gate_reports,
    )
