"""Tests for the factsheet parse-skip safety (mfs.ingest._parse_cache).

The skip is only safe if it fires EXACTLY when re-parsing would be a no-op:
artifact bytes unchanged AND match-universe fingerprint unchanged AND the DB
already holds the rows. Any change to any of the three must force a re-parse.
"""

from __future__ import annotations

from mfs.ingest import _parse_cache as pc


def _write(p, data: bytes):
    p.write_bytes(data)
    return p


def test_fingerprint_is_order_independent():
    assert pc.fingerprint(["a", "b", "c"]) == pc.fingerprint(["c", "a", "b"])


def test_fingerprint_changes_with_membership():
    assert pc.fingerprint(["a", "b"]) != pc.fingerprint(["a", "b", "c"])


def test_sha256_file_changes_with_content(tmp_path):
    p = _write(tmp_path / "f.pdf", b"hello")
    s1 = pc.sha256_file(p)
    _write(p, b"hello world")
    assert pc.sha256_file(p) != s1


def test_skip_when_artifact_and_universe_unchanged(tmp_path):
    art = _write(tmp_path / "f.pdf", b"factsheet-bytes")
    fp = pc.fingerprint(["S1=100", "S2=200"])
    pc.write_marker(art, fp, {"ym": "2026-04"})
    assert pc.should_skip_parse(art, fp, db_has_rows=True) is True


def test_no_skip_when_db_has_no_rows(tmp_path):
    art = _write(tmp_path / "f.pdf", b"factsheet-bytes")
    fp = pc.fingerprint(["S1=100"])
    pc.write_marker(art, fp, {})
    # Marker present and artifact identical, but DB empty → must re-parse.
    assert pc.should_skip_parse(art, fp, db_has_rows=False) is False


def test_no_skip_when_artifact_changed(tmp_path):
    art = _write(tmp_path / "f.pdf", b"v1")
    fp = pc.fingerprint(["S1=100"])
    pc.write_marker(art, fp, {})
    _write(art, b"v2-corrected-factsheet")  # republished PDF
    assert pc.should_skip_parse(art, fp, db_has_rows=True) is False


def test_no_skip_when_universe_changed(tmp_path):
    art = _write(tmp_path / "f.pdf", b"bytes")
    pc.write_marker(art, pc.fingerprint(["S1=100"]), {})
    # scheme_master changed → a new scheme would re-route matches → re-parse.
    new_fp = pc.fingerprint(["S1=100", "S2=200"])
    assert pc.should_skip_parse(art, new_fp, db_has_rows=True) is False


def test_no_skip_when_marker_missing(tmp_path):
    art = _write(tmp_path / "f.pdf", b"bytes")
    assert pc.should_skip_parse(art, pc.fingerprint(["S1=100"]), db_has_rows=True) is False


def test_no_skip_when_marker_corrupt(tmp_path):
    art = _write(tmp_path / "f.pdf", b"bytes")
    pc._marker_path(art).write_text("{not valid json")
    assert pc.should_skip_parse(art, pc.fingerprint(["S1=100"]), db_has_rows=True) is False
