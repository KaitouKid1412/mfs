"""E7 — ``mfs shortlist diff``: run-to-run ENTERED / EXITED / RANK-MOVED.

Two synthetic run dirs are crafted so each of the five EXIT-reason
derivation paths fires exactly once:

  1. run2 stage1/excluded.csv          → E_EXCL
  2. run2 stage2/dropped.csv           → E_S2DROP
  3. run2 stage3/dropped.csv (pre-D4)  → E_S3DROP
  4. ranked in run2 stage1 category    → E_FELL ('score fell to rank 7')
  5. absent from run2 entirely         → E_GONE

Pure file diff — no DB access anywhere.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from mfs.rank.diff import diff_runs, render_diff, write_diff


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_csv(path)


def _final_row(code: str, cat: str, rank: int) -> dict:
    return {
        "scheme_code": code,
        "scheme_name": f"Fund {code}",
        "canonical_category": cat,
        "stage2_rank": rank,
    }


@pytest.fixture
def run_dirs(tmp_path: Path) -> tuple[Path, Path]:
    run_a = tmp_path / "2026-05-22"
    run_b = tmp_path / "2026-06-12"

    # --- older run final list (stage3/mf_report.csv) ----------------------
    _write(run_a / "stage3" / "mf_report.csv", [
        _final_row("KEEP1", "Alpha", 1),
        _final_row("E_EXCL", "Alpha", 2),
        _final_row("MOVER", "Alpha", 3),
        _final_row("E_S2DROP", "Alpha", 4),
        _final_row("E_FELL", "Alpha", 5),
        _final_row("E_S3DROP", "Beta Cap", 1),
        _final_row("E_GONE", "Beta Cap", 2),
    ])
    # N_ROSE was ranked 9 at stage 1 in the older run (entered-reason path).
    _write(run_a / "stage1" / "Alpha.csv", [
        {"rank": 9, "scheme_code": "N_ROSE", "canonical_category": "Alpha"},
    ])

    # --- newer run final list ---------------------------------------------
    _write(run_b / "stage3" / "mf_report.csv", [
        _final_row("MOVER", "Alpha", 1),
        _final_row("KEEP1", "Alpha", 2),
        _final_row("N_ROSE", "Alpha", 3),
        _final_row("N_NEW", "Beta Cap", 1),
    ])

    # --- newer run reason artifacts ----------------------------------------
    _write(run_b / "stage1" / "excluded.csv", [{
        "scheme_code": "E_EXCL",
        "scheme_name": "Fund E_EXCL",
        "canonical_category": "Alpha",
        "data_quality_flag": "GOOD",
        "exclusion_reason": "MISSING_CORE_METRIC:sortino_3y",
        "missing_core_metrics": "sortino_3y",
    }])
    _write(run_b / "stage2" / "dropped.csv", [{
        "canonical_category": "Alpha",
        "stage1_rank": 6,
        "scheme_code": "E_S2DROP",
        "missing_metrics": "ptr_latest",
    }])
    _write(run_b / "stage3" / "dropped.csv", [{
        "scheme_code_dropped": "E_S3DROP",
        "scheme_name_dropped": "Fund E_S3DROP",
        "category_dropped": "Beta Cap",
        "stage2_rank_dropped": 1,
        "scheme_code_kept": "KEEP1",
        "scheme_name_kept": "Fund KEEP1",
        "category_kept": "Alpha",
        "stage2_rank_kept": 2,
        "overlap_pct": 45.2,
        "top_shared_holdings": "Shared Co (10%)",
    }])
    # E_FELL is still stage-1 ranked in the newer run, just below the cut.
    _write(run_b / "stage1" / "Alpha.csv", [
        {"rank": 7, "scheme_code": "E_FELL", "canonical_category": "Alpha"},
    ])
    return run_a, run_b


def test_membership_entered_exited_rank_moved(run_dirs):
    run_a, run_b = run_dirs
    diff = diff_runs(run_a, run_b)
    assert diff["run_a"] == "2026-05-22"
    assert diff["run_b"] == "2026-06-12"
    assert diff["notes"] == []
    assert [r["scheme_code"] for r in diff["entered"]] == ["N_ROSE", "N_NEW"]
    assert [r["scheme_code"] for r in diff["exited"]] == [
        "E_EXCL", "E_FELL", "E_S2DROP",     # Alpha, code-sorted
        "E_GONE", "E_S3DROP",               # Beta Cap, code-sorted
    ]
    moved = {r["scheme_code"]: r for r in diff["rank_moved"]}
    assert set(moved) == {"KEEP1", "MOVER"}
    assert (moved["MOVER"]["rank_a"], moved["MOVER"]["rank_b"]) == (3, 1)
    assert moved["MOVER"]["delta"] == -2
    assert moved["KEEP1"]["delta"] == 1
    assert diff["n_final_a"] == 7
    assert diff["n_final_b"] == 4


def test_exit_reasons_all_five_paths(run_dirs):
    run_a, run_b = run_dirs
    reasons = {
        r["scheme_code"]: r["reason"] for r in diff_runs(run_a, run_b)["exited"]
    }
    assert reasons["E_EXCL"] == "excluded: insufficient data (sortino_3y)"
    assert reasons["E_S2DROP"] == "dropped at stage 2: missing ptr_latest"
    assert reasons["E_S3DROP"] == (
        "overlap-dropped vs KEEP1 Fund KEEP1 @ 45.2%"
    )
    assert reasons["E_FELL"] == "score fell to rank 7"
    assert reasons["E_GONE"] == "no longer ranked (hard-filtered or no data)"


def test_enter_reasons_symmetric(run_dirs):
    run_a, run_b = run_dirs
    reasons = {
        r["scheme_code"]: r["reason"]
        for r in diff_runs(run_a, run_b)["entered"]
    }
    assert reasons["N_ROSE"] == "rose from rank 9"
    assert reasons["N_NEW"] == "new to final list"


def test_diff_markdown_written_and_rendered(run_dirs):
    run_a, run_b = run_dirs
    text, out_path = write_diff(run_a, run_b)
    assert out_path == run_b / "DIFF_vs_2026-05-22.md"
    assert out_path.exists()
    assert out_path.read_text() == text
    assert "# Shortlist diff: 2026-05-22 → 2026-06-12" in text
    assert "## Alpha" in text and "## Beta Cap" in text
    assert "- ENTERED: N_NEW Fund N_NEW (rank 1) — new to final list" in text
    assert (
        "- EXITED: E_EXCL Fund E_EXCL — excluded: insufficient data (sortino_3y)"
        in text
    )
    assert "- RANK-MOVED: MOVER Fund MOVER 3 → 1 (improved)" in text
    assert "- RANK-MOVED: KEEP1 Fund KEEP1 1 → 2 (fell)" in text


def test_stage2_fallback_note_when_stage3_absent(run_dirs, tmp_path):
    """A run dir with only stage2/mf_report.csv diffs fine — with a visible
    note on both the dict and the rendered markdown."""
    run_a, _ = run_dirs
    run_c = tmp_path / "2026-07-01"
    _write(run_c / "stage2" / "mf_report.csv", [
        _final_row("KEEP1", "Alpha", 1),
    ])
    diff = diff_runs(run_a, run_c)
    assert len(diff["notes"]) == 1
    assert "stage3/mf_report.csv absent" in diff["notes"][0]
    assert "2026-07-01" in diff["notes"][0]
    text = render_diff(diff)
    assert "NOTE:" in text and "stage2/mf_report.csv" in text


def test_identical_runs_no_changes(run_dirs):
    run_a, _ = run_dirs
    diff = diff_runs(run_a, run_a)
    assert diff["entered"] == [] and diff["exited"] == []
    assert diff["rank_moved"] == []
    assert "No changes between the two runs." in render_diff(diff)


def test_missing_run_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        diff_runs(tmp_path / "nope_a", tmp_path / "nope_b")
