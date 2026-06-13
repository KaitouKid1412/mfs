"""Run provenance: a ``manifest.json`` per shortlist run (F-10).

Every ``rank_deep`` run (standalone ``mfs rank-deep`` or via the pipeline)
writes ``data/output/shortlist/<as_of>/manifest.json`` recording exactly what
produced that shortlist:

  * git SHA + dirty flag of the working tree the run executed from;
  * a SHA-256 over the three config files that shape ranking
    (``configs/pipeline.yaml``, ``configs/category_thresholds.yaml``,
    ``configs/benchmarks.csv``);
  * ``pipeline_version`` (single source of truth: configs/pipeline.yaml —
    the config.py default was removed so a missing key fails loudly) and the
    installed ``mfs`` package version;
  * per-table row counts of every input table plus the ``computed_metrics``
    partition at the run's as_of (``db.queries.table_counts``);
  * stage 1/2/3 file + survivor/drop counts from the ``rank_deep`` result.

The manifest is written atomically (``io/atomic.py``) and is best-effort at
the call site: a provenance failure must never break a completed ranking run
(``shortlist.rank_deep`` wraps the hook in try/except). Each section here is
also individually fault-tolerant — e.g. a missing ``git`` binary records
``git_sha=None`` rather than raising.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date, datetime, timezone
from importlib import metadata as _importlib_metadata
from pathlib import Path
from typing import Any

from mfs.config import REPO_ROOT, get_pipeline_config, get_settings
from mfs.db import queries as q
from mfs.io.atomic import atomic_write_text
from mfs.utils.logging import get_logger

log = get_logger(__name__)

MANIFEST_FILENAME = "manifest.json"

#: Required keys of every manifest — tests pin this list so a manifest can
#: never silently lose a provenance field.
MANIFEST_REQUIRED_KEYS = (
    "as_of",
    "generated_at",
    "git_sha",
    "git_dirty",
    "config_sha256",
    "pipeline_version",
    "mfs_version",
    "db_row_counts",
    "stage1",
    "stage2",
    "stage3",
)


def git_info(repo_root: Path | None = None) -> dict[str, Any]:
    """``{"git_sha": <HEAD sha or None>, "git_dirty": <bool or None>}``.

    Reading git state is the only subprocess use; any failure (no git binary,
    not a repo) degrades to ``None`` values rather than raising.
    """
    root = repo_root or REPO_ROOT
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root, capture_output=True, text=True, timeout=30, check=True,
        ).stdout
        return {"git_sha": sha, "git_dirty": bool(porcelain.strip())}
    except Exception as e:  # noqa: BLE001 - provenance must degrade, not raise
        log.warning("provenance.git_info_failed", err=str(e))
        return {"git_sha": None, "git_dirty": None}


def config_files() -> list[Path]:
    """The config files whose bytes are hashed into ``config_sha256``."""
    s = get_settings()
    return [s.config_path, s.thresholds_yaml, s.benchmarks_csv]


def config_sha256(files: list[Path] | None = None) -> str | None:
    """SHA-256 over the ranking config files' bytes (name + content per file,
    in fixed order, so a renamed-but-identical file still changes the hash).
    ``None`` if any file is unreadable."""
    h = hashlib.sha256()
    try:
        for p in files if files is not None else config_files():
            h.update(p.name.encode())
            h.update(b"\x00")
            h.update(p.read_bytes())
            h.update(b"\x00")
    except OSError as e:
        log.warning("provenance.config_hash_failed", err=str(e))
        return None
    return h.hexdigest()


def mfs_version() -> str:
    try:
        return _importlib_metadata.version("mfs")
    except _importlib_metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def _stage_sections(result: dict) -> dict[str, dict[str, Any]]:
    """Stage 1/2/3 file + survivor counts from the ``rank_deep`` result."""
    stage1 = result.get("stage1") or {}
    stage2 = result.get("stage2") or {}
    stage3 = result.get("stage3") or {}
    return {
        "stage1": {
            "n_category_files": len(stage1),
            "n_excluded": result.get("n_excluded"),
            "excluded_file": result.get("excluded_file"),
        },
        "stage2": {
            "n_survivors": stage2.get("n_survivors"),
            "n_dropped": stage2.get("n_dropped"),
        },
        "stage3": {
            "n_final": stage3.get("n_final"),
            "n_flagged": stage3.get("n_flagged"),
            "n_breach_pairs": stage3.get("n_breach_pairs"),
        },
    }


def build_manifest(
    as_of: date,
    result: dict,
    *,
    git: dict[str, Any] | None = None,
    db_row_counts: dict[str, int] | None = None,
    config_hash: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the run manifest. Keyword seams (``git``, ``db_row_counts``,
    ``config_hash``, ``now``) exist for tests; production callers pass none
    of them. The DB-count section degrades to an ``{"error": ...}`` marker on
    failure so an unreachable DB can't lose the rest of the provenance."""
    if git is None:
        git = git_info()
    if db_row_counts is None:
        try:
            db_row_counts = q.table_counts(as_of)
        except Exception as e:  # noqa: BLE001 - degrade, never raise
            log.warning("provenance.table_counts_failed", err=str(e))
            db_row_counts = {"error": str(e)}
    if config_hash is None:
        config_hash = config_sha256()
    try:
        pipeline_version: str | None = get_pipeline_config().pipeline_version
    except Exception as e:  # noqa: BLE001 - degrade, never raise
        log.warning("provenance.pipeline_version_failed", err=str(e))
        pipeline_version = None
    ts = now or datetime.now(timezone.utc)
    return {
        "as_of": as_of.isoformat(),
        "generated_at": ts.isoformat(),
        "git_sha": git.get("git_sha"),
        "git_dirty": git.get("git_dirty"),
        "config_sha256": config_hash,
        "pipeline_version": pipeline_version,
        "mfs_version": mfs_version(),
        "db_row_counts": db_row_counts,
        **_stage_sections(result),
    }


def write_manifest(manifest: dict[str, Any], out_dir: Path) -> Path:
    """Atomically write ``manifest.json`` under ``out_dir``."""
    path = Path(out_dir) / MANIFEST_FILENAME
    atomic_write_text(
        path, json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n"
    )
    log.info("provenance.manifest_written", path=str(path))
    return path


def write_run_manifest(as_of: date, result: dict) -> Path:
    """Build + write the manifest for a completed ``rank_deep`` run, into the
    run's output directory (``result['out_dir']``)."""
    manifest = build_manifest(as_of, result)
    return write_manifest(manifest, Path(result["out_dir"]))
