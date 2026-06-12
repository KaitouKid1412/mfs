"""Run-to-run shortlist diff (E7): ``mfs shortlist diff <run1> <run2>``.

Pure file diff over two shortlist run directories — no DB access, so it
works on historical run dirs as-is. The final list for each run is
``stage3/mf_report.csv`` (falling back to ``stage2/mf_report.csv`` with a
visible note); funds are keyed by ``scheme_code``.

Per category the diff emits ENTERED, EXITED and RANK-MOVED (``stage2_rank``
delta) lists. For EXITED funds a reason is derived from the newer run's
artifacts, checked in order:

  1. ``stage1/excluded.csv`` (D2)        → excluded: insufficient data
  2. ``stage2/dropped.csv``              → dropped at stage 2 (pre-D3 runs)
  3. ``stage3/dropped.csv``              → overlap-dropped (pre-D4 runs)
  4. ranked in run2 stage2/stage1 file   → score fell to rank N
  5. absent from run2 stage1 entirely    → no longer ranked

ENTERED reasons are symmetric against the OLDER run's artifacts where
derivable, else 'new to final list'.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import polars as pl

from mfs.utils.logging import get_logger

log = get_logger(__name__)

#: Markdown artifact written next to the newer run: DIFF_vs_<run1>.md
DIFF_FILENAME_TEMPLATE = "DIFF_vs_{run1}.md"

_FALLBACK_NOTE = (
    "stage3/mf_report.csv absent — diff computed from stage2/mf_report.csv"
)


def _read_csv(path: Path) -> pl.DataFrame:
    """Read one artifact CSV with every column as Utf8 (scheme codes are
    numeric-looking strings; rank columns are cast back where needed).
    Empty frame when the file is absent or header-only."""
    if not path.exists():
        return pl.DataFrame()
    try:
        df = pl.read_csv(path, infer_schema_length=0)  # all Utf8
    except pl.exceptions.NoDataError:
        return pl.DataFrame()
    return df


def _safe_name(category: str) -> str:
    """Mirror the stage writers' category → filename mangling."""
    return "".join(c if c.isalnum() else "_" for c in category)


def _load_final(run_dir: Path) -> tuple[pl.DataFrame, str | None]:
    """The run's final consolidated report; (frame, note). Note is non-None
    on the stage2 fallback path."""
    stage3 = run_dir / "stage3" / "mf_report.csv"
    if stage3.exists():
        return _read_csv(stage3), None
    stage2 = run_dir / "stage2" / "mf_report.csv"
    if stage2.exists():
        return _read_csv(stage2), f"{run_dir.name}: {_FALLBACK_NOTE}"
    raise FileNotFoundError(
        f"{run_dir}: neither stage3/mf_report.csv nor stage2/mf_report.csv "
        "exists — not a shortlist run dir?"
    )


def _final_rows(df: pl.DataFrame) -> dict[str, dict]:
    """Final report keyed by scheme_code, with int-cast stage2_rank."""
    if df.is_empty() or "scheme_code" not in df.columns:
        return {}
    out: dict[str, dict] = {}
    for r in df.iter_rows(named=True):
        rank = r.get("stage2_rank")
        out[str(r["scheme_code"])] = {
            "scheme_code": str(r["scheme_code"]),
            "scheme_name": r.get("scheme_name"),
            "canonical_category": r.get("canonical_category") or "(unknown)",
            "stage2_rank": int(rank) if rank not in (None, "") else None,
        }
    return out


class _RunArtifacts:
    """Lazy, cached readers over one run dir's reason-bearing artifacts."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir

    @lru_cache(maxsize=None)  # noqa: B019 — instances live for one diff call
    def excluded(self) -> dict[str, dict]:
        df = _read_csv(self.run_dir / "stage1" / "excluded.csv")
        if df.is_empty() or "scheme_code" not in df.columns:
            return {}
        return {str(r["scheme_code"]): r for r in df.iter_rows(named=True)}

    @lru_cache(maxsize=None)  # noqa: B019
    def stage2_dropped(self) -> dict[str, dict]:
        df = _read_csv(self.run_dir / "stage2" / "dropped.csv")
        if df.is_empty() or "scheme_code" not in df.columns:
            return {}
        return {str(r["scheme_code"]): r for r in df.iter_rows(named=True)}

    @lru_cache(maxsize=None)  # noqa: B019
    def stage3_dropped(self) -> dict[str, dict]:
        df = _read_csv(self.run_dir / "stage3" / "dropped.csv")
        if df.is_empty() or "scheme_code_dropped" not in df.columns:
            return {}
        return {
            str(r["scheme_code_dropped"]): r for r in df.iter_rows(named=True)
        }

    @lru_cache(maxsize=None)  # noqa: B019
    def _category_rank(self, stage: str, category: str) -> dict[str, int]:
        """scheme_code → rank within one run's stage1/stage2 category file."""
        rank_col = {"stage1": "rank", "stage2": "stage2_rank"}[stage]
        df = _read_csv(self.run_dir / stage / f"{_safe_name(category)}.csv")
        if df.is_empty() or not {"scheme_code", rank_col} <= set(df.columns):
            return {}
        return {
            str(r["scheme_code"]): int(r[rank_col])
            for r in df.iter_rows(named=True)
            if r[rank_col] not in (None, "")
        }

    def rank_in_category(self, code: str, category: str) -> int | None:
        """The fund's rank in this run, preferring the stage2 file (post
        re-rank) over the stage1 file."""
        for stage in ("stage2", "stage1"):
            rank = self._category_rank(stage, category).get(code)
            if rank is not None:
                return rank
        return None

    def in_stage1_universe(self, code: str, category: str) -> bool:
        return code in self._category_rank("stage1", category)


def _exit_reason(code: str, category: str, newer: _RunArtifacts) -> str:
    """Why a fund present in the older final list is absent from the newer
    one — derived from the newer run's artifacts, most specific first."""
    row = newer.excluded().get(code)
    if row is not None:
        detail = row.get("missing_core_metrics") or row.get("exclusion_reason")
        return f"excluded: insufficient data ({detail})"
    row = newer.stage2_dropped().get(code)
    if row is not None:
        return f"dropped at stage 2: missing {row.get('missing_metrics')}"
    row = newer.stage3_dropped().get(code)
    if row is not None:
        kept = f"{row.get('scheme_code_kept')} {row.get('scheme_name_kept')}"
        return f"overlap-dropped vs {kept} @ {row.get('overlap_pct')}%"
    rank = newer.rank_in_category(code, category)
    if rank is not None:
        return f"score fell to rank {rank}"
    return "no longer ranked (hard-filtered or no data)"


def _enter_reason(code: str, category: str, older: _RunArtifacts) -> str:
    """Symmetric derivation against the OLDER run's artifacts."""
    row = older.excluded().get(code)
    if row is not None:
        detail = row.get("missing_core_metrics") or row.get("exclusion_reason")
        return f"previously excluded: insufficient data ({detail})"
    row = older.stage2_dropped().get(code)
    if row is not None:
        return (
            f"previously dropped at stage 2: missing {row.get('missing_metrics')}"
        )
    row = older.stage3_dropped().get(code)
    if row is not None:
        kept = f"{row.get('scheme_code_kept')} {row.get('scheme_name_kept')}"
        return f"previously overlap-dropped vs {kept}"
    rank = older.rank_in_category(code, category)
    if rank is not None:
        return f"rose from rank {rank}"
    return "new to final list"


def diff_runs(run_a: Path, run_b: Path) -> dict:
    """Pure diff of two shortlist run dirs (older ``run_a`` → newer ``run_b``).

    Returns ``{"run_a", "run_b", "notes", "entered", "exited", "rank_moved",
    "n_final_a", "n_final_b"}`` where each list entry carries scheme
    identity, category, rank(s) and a derived reason. Sorted by
    (category, scheme_code) for deterministic output.
    """
    run_a, run_b = Path(run_a), Path(run_b)
    final_a_df, note_a = _load_final(run_a)
    final_b_df, note_b = _load_final(run_b)
    final_a = _final_rows(final_a_df)
    final_b = _final_rows(final_b_df)
    artifacts_a = _RunArtifacts(run_a)
    artifacts_b = _RunArtifacts(run_b)

    exited = []
    for code in sorted(set(final_a) - set(final_b)):
        row = final_a[code]
        cat = row["canonical_category"]
        exited.append({
            **row,
            "reason": _exit_reason(code, cat, artifacts_b),
        })

    entered = []
    for code in sorted(set(final_b) - set(final_a)):
        row = final_b[code]
        cat = row["canonical_category"]
        entered.append({
            **row,
            "reason": _enter_reason(code, cat, artifacts_a),
        })

    rank_moved = []
    for code in sorted(set(final_a) & set(final_b)):
        a, b = final_a[code], final_b[code]
        if a["stage2_rank"] is None or b["stage2_rank"] is None:
            continue
        if a["stage2_rank"] != b["stage2_rank"]:
            rank_moved.append({
                "scheme_code": code,
                "scheme_name": b["scheme_name"],
                "canonical_category": b["canonical_category"],
                "rank_a": a["stage2_rank"],
                "rank_b": b["stage2_rank"],
                "delta": b["stage2_rank"] - a["stage2_rank"],
            })

    key = lambda r: (r["canonical_category"], r["scheme_code"])  # noqa: E731
    return {
        "run_a": run_a.name,
        "run_b": run_b.name,
        "notes": [n for n in (note_a, note_b) if n],
        "entered": sorted(entered, key=key),
        "exited": sorted(exited, key=key),
        "rank_moved": sorted(rank_moved, key=key),
        "n_final_a": len(final_a),
        "n_final_b": len(final_b),
    }


def render_diff(diff: dict) -> str:
    """Render a ``diff_runs`` result as markdown."""
    lines = [
        f"# Shortlist diff: {diff['run_a']} → {diff['run_b']}",
        "",
        f"Final list size: {diff['n_final_a']} → {diff['n_final_b']}. "
        f"Entered: {len(diff['entered'])}, exited: {len(diff['exited'])}, "
        f"rank-moved: {len(diff['rank_moved'])}.",
    ]
    for note in diff["notes"]:
        lines.append(f"\n> NOTE: {note}")

    categories = sorted({
        r["canonical_category"]
        for section in ("entered", "exited", "rank_moved")
        for r in diff[section]
    })
    if not categories:
        lines.append("\nNo changes between the two runs.")
    for cat in categories:
        lines.append(f"\n## {cat}")
        for r in diff["entered"]:
            if r["canonical_category"] == cat:
                rank = f" (rank {r['stage2_rank']})" if r["stage2_rank"] else ""
                lines.append(
                    f"- ENTERED: {r['scheme_code']} {r['scheme_name']}{rank}"
                    f" — {r['reason']}"
                )
        for r in diff["exited"]:
            if r["canonical_category"] == cat:
                lines.append(
                    f"- EXITED: {r['scheme_code']} {r['scheme_name']}"
                    f" — {r['reason']}"
                )
        for r in diff["rank_moved"]:
            if r["canonical_category"] == cat:
                arrow = "improved" if r["delta"] < 0 else "fell"
                lines.append(
                    f"- RANK-MOVED: {r['scheme_code']} {r['scheme_name']} "
                    f"{r['rank_a']} → {r['rank_b']} ({arrow})"
                )
    lines.append("")
    return "\n".join(lines)


def write_diff(run_a: Path, run_b: Path) -> tuple[str, Path]:
    """Diff + render + write ``<run_b>/DIFF_vs_<run_a>.md``; returns
    ``(markdown_text, written_path)``."""
    run_a, run_b = Path(run_a), Path(run_b)
    diff = diff_runs(run_a, run_b)
    text = render_diff(diff)
    out_path = run_b / DIFF_FILENAME_TEMPLATE.format(run1=run_a.name)
    out_path.write_text(text)
    log.info(
        "shortlist.diff.written",
        run_a=run_a.name,
        run_b=run_b.name,
        n_entered=len(diff["entered"]),
        n_exited=len(diff["exited"]),
        n_rank_moved=len(diff["rank_moved"]),
        path=str(out_path),
    )
    return text, out_path
