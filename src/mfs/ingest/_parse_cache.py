"""Content-hash "already-ingested" markers for skipping redundant re-parses.

Parsing a factsheet PDF with pdfplumber is the repeated cost of an incremental
run — the download itself is already disk-cached. We skip the re-parse ONLY when
it is provably a no-op, i.e. all three hold:

  1. the artifact bytes are byte-identical to what we last parsed, AND
  2. the scheme-match universe that drove the previous ingest is unchanged, AND
  3. the DB already holds rows for that (amc, month).

Under those conditions, re-parsing + the DELETE-reinsert would reproduce the
exact same rows, so skipping changes nothing observable.

This is the SAFE variant of incrementality: we never skip on mere DB presence
(a corrected/republished factsheet, or a changed scheme_master that would
re-route fuzzy matches, MUST re-ingest) — only on a verified-identical
(artifact, universe) pair. Condition 2 is what closes the scheme_master-coupling
hole that a naive "skip if (amc, ym) already in DB" would leave open.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mfs.io.atomic import atomic_write_text
from mfs.utils.logging import get_logger

log = get_logger(__name__)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(parts) -> str:
    """Stable hash of an iterable of strings (e.g. sorted candidate scheme_codes).
    Order-independent (sorted) so it captures set identity, not iteration order."""
    h = hashlib.sha256()
    for p in sorted(parts):
        h.update(str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _marker_path(artifact: Path) -> Path:
    return artifact.with_suffix(artifact.suffix + ".ingested.json")


def should_skip_parse(artifact: Path, universe_fp: str, *, db_has_rows: bool) -> bool:
    """True iff the artifact AND the match universe are byte-for-byte the same as
    the last successful ingest, and the DB already holds that ingest's rows."""
    if not db_has_rows or not artifact.exists():
        return False
    marker = _marker_path(artifact)
    if not marker.exists():
        return False
    try:
        m = json.loads(marker.read_text())
    except Exception as e:  # noqa: BLE001 — a corrupt marker just means "don't skip"
        log.warning("parse_cache.marker_unreadable", path=str(marker), err=str(e))
        return False
    return (
        m.get("artifact_sha256") == sha256_file(artifact)
        and m.get("universe_fp") == universe_fp
    )


def write_marker(artifact: Path, universe_fp: str, extra: dict | None = None) -> None:
    """Record a successful ingest of ``artifact`` under the current universe.
    Call ONLY after the rows are committed, so a crash mid-ingest leaves no
    marker and the next run re-parses."""
    payload = {"artifact_sha256": sha256_file(artifact), "universe_fp": universe_fp}
    if extra:
        payload.update(extra)
    atomic_write_text(_marker_path(artifact), json.dumps(payload, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Dir-keyed variant (C5) — for ingests whose unit is a SET of artifacts
# (the holdings path: one Excel per scheme under data/raw/holdings/<amc>/<ym>/)
# rather than a single file. The artifact-set fingerprint is computed by the
# caller (e.g. over sorted '{printed_name}:{sha256(excel)}' entries) so adding,
# removing, renaming or mutating any member forces a re-parse; the universe
# fingerprint carries the same scheme_master-coupling guarantee as the
# single-file marker above.
# ---------------------------------------------------------------------------

_DIR_MARKER_NAME = ".ingested.json"


def _dir_marker_path(marker_dir: Path) -> Path:
    return marker_dir / _DIR_MARKER_NAME


def should_skip_parse_dir(
    marker_dir: Path, artifact_set_fp: str, universe_fp: str, *, db_has_rows: bool
) -> bool:
    """True iff the artifact SET and the match universe fingerprints match the
    last successful ingest recorded in ``<marker_dir>/.ingested.json``, and the
    DB already holds that ingest's rows."""
    if not db_has_rows:
        return False
    marker = _dir_marker_path(marker_dir)
    if not marker.exists():
        return False
    try:
        m = json.loads(marker.read_text())
    except Exception as e:  # noqa: BLE001 — a corrupt marker just means "don't skip"
        log.warning("parse_cache.marker_unreadable", path=str(marker), err=str(e))
        return False
    return (
        m.get("artifact_set_fp") == artifact_set_fp
        and m.get("universe_fp") == universe_fp
    )


def write_dir_marker(
    marker_dir: Path, artifact_set_fp: str, universe_fp: str,
    extra: dict | None = None,
) -> None:
    """Record a successful set-ingest under ``marker_dir``. Call ONLY after the
    rows are committed, so a crash mid-ingest leaves no marker and the next run
    re-parses."""
    payload = {"artifact_set_fp": artifact_set_fp, "universe_fp": universe_fp}
    if extra:
        payload.update(extra)
    marker_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        _dir_marker_path(marker_dir), json.dumps(payload, indent=2, sort_keys=True)
    )
