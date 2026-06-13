"""F-10: run-provenance manifest + pipeline_version single-source tests.

No network, no database: DB counts and git info are injected through
``build_manifest``'s keyword seams; the degrade paths are exercised with a
non-repo directory and a failing counts function.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from mfs import provenance
from mfs.config import REPO_ROOT, PipelineConfig
from mfs.rank import shortlist

AS_OF = date(2026, 6, 12)

FAKE_GIT = {"git_sha": "a" * 40, "git_dirty": True}
FAKE_COUNTS = {
    "nav_daily": 30_000_000,
    "benchmark_daily": 120_000,
    "holdings_monthly": 250_000,
    "portfolio_turnover_monthly": 4_000,
    "scheme_aum_monthly": 9_000,
    "index_constituents_monthly": 6_000,
    "computed_metrics": 666,
}

RESULT = {
    "as_of": AS_OF.isoformat(),
    "out_dir": "/tmp/ignored",
    "n_excluded": 7,
    "excluded_file": "/tmp/ignored/stage1/excluded.csv",
    "stage1": {"Large Cap": "a.csv", "Mid Cap": "b.csv"},
    "stage2": {"n_survivors": 115, "n_dropped": 0},
    "stage3": {"n_final": 115, "n_flagged": 9, "n_breach_pairs": 5},
}


def _build(**kw):
    defaults = dict(
        git=FAKE_GIT,
        db_row_counts=FAKE_COUNTS,
        config_hash="deadbeef",
        now=datetime(2026, 6, 12, 10, 30, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return provenance.build_manifest(AS_OF, RESULT, **defaults)


# ---------------------------------------------------------------------------
# build_manifest
# ---------------------------------------------------------------------------


def test_manifest_has_all_required_keys():
    m = _build()
    for key in provenance.MANIFEST_REQUIRED_KEYS:
        assert key in m, f"manifest missing required key {key!r}"


def test_manifest_core_fields():
    m = _build()
    assert m["as_of"] == "2026-06-12"
    assert m["git_sha"] == "a" * 40
    assert m["git_dirty"] is True
    assert m["config_sha256"] == "deadbeef"
    assert m["db_row_counts"] == FAKE_COUNTS
    assert m["generated_at"] == "2026-06-12T10:30:00+00:00"
    # pipeline_version comes from the real configs/pipeline.yaml (the single
    # source of truth) — it must be the yaml string, never a code default.
    with open(REPO_ROOT / "configs" / "pipeline.yaml") as f:
        yaml_version = yaml.safe_load(f)["pipeline_version"]
    assert m["pipeline_version"] == yaml_version
    assert isinstance(m["mfs_version"], str) and m["mfs_version"]


def test_manifest_dirty_flag_false_passthrough():
    m = _build(git={"git_sha": "b" * 40, "git_dirty": False})
    assert m["git_dirty"] is False


def test_manifest_stage_counts_from_result():
    m = _build()
    assert m["stage1"] == {
        "n_category_files": 2,
        "n_excluded": 7,
        "excluded_file": "/tmp/ignored/stage1/excluded.csv",
    }
    assert m["stage2"] == {"n_survivors": 115, "n_dropped": 0}
    assert m["stage3"] == {"n_final": 115, "n_flagged": 9, "n_breach_pairs": 5}


def test_manifest_db_counts_degrade_to_error_marker(monkeypatch):
    def _boom(as_of):
        raise RuntimeError("db down")

    monkeypatch.setattr(provenance.q, "table_counts", _boom)
    m = _build(db_row_counts=None)
    assert m["db_row_counts"] == {"error": "db down"}


# ---------------------------------------------------------------------------
# git_info / config_sha256 degrade + determinism
# ---------------------------------------------------------------------------


def test_git_info_degrades_outside_a_repo(tmp_path):
    info = provenance.git_info(repo_root=tmp_path)
    assert info == {"git_sha": None, "git_dirty": None}


def test_config_sha256_changes_with_content(tmp_path):
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_bytes(b"x: 1\n")
    b.write_bytes(b"y: 2\n")
    h1 = provenance.config_sha256([a, b])
    assert h1 == provenance.config_sha256([a, b])  # deterministic
    b.write_bytes(b"y: 3\n")
    assert provenance.config_sha256([a, b]) != h1


def test_config_sha256_none_when_file_missing(tmp_path):
    assert provenance.config_sha256([tmp_path / "absent.yaml"]) is None


# ---------------------------------------------------------------------------
# write_manifest / write_run_manifest
# ---------------------------------------------------------------------------


def test_write_manifest_round_trips_json(tmp_path):
    m = _build()
    path = provenance.write_manifest(m, tmp_path)
    assert path == tmp_path / provenance.MANIFEST_FILENAME
    assert json.loads(path.read_text()) == m


def test_write_run_manifest_targets_result_out_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "git_info", lambda repo_root=None: FAKE_GIT)
    monkeypatch.setattr(provenance.q, "table_counts", lambda as_of: FAKE_COUNTS)
    result = dict(RESULT, out_dir=str(tmp_path))
    path = provenance.write_run_manifest(AS_OF, result)
    assert path.parent == tmp_path
    assert json.loads(path.read_text())["db_row_counts"] == FAKE_COUNTS


# ---------------------------------------------------------------------------
# shortlist._emit_manifest: best-effort, never raises
# ---------------------------------------------------------------------------


def test_emit_manifest_success_records_path(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "git_info", lambda repo_root=None: FAKE_GIT)
    monkeypatch.setattr(provenance.q, "table_counts", lambda as_of: FAKE_COUNTS)
    result = dict(RESULT, out_dir=str(tmp_path))
    shortlist._emit_manifest(AS_OF, result)
    assert result["manifest_file"] == str(tmp_path / provenance.MANIFEST_FILENAME)
    assert (tmp_path / provenance.MANIFEST_FILENAME).exists()


def test_emit_manifest_failure_never_raises(monkeypatch):
    def _boom(as_of, result):
        raise RuntimeError("provenance exploded")

    monkeypatch.setattr(shortlist.provenance, "write_run_manifest", _boom)
    result = dict(RESULT)
    shortlist._emit_manifest(AS_OF, result)  # must not raise
    assert "manifest_file" not in result


# ---------------------------------------------------------------------------
# pipeline_version: yaml is the single source — a missing key fails loudly
# ---------------------------------------------------------------------------


def _real_config_dict() -> dict:
    with open(REPO_ROOT / "configs" / "pipeline.yaml") as f:
        return yaml.safe_load(f)


def test_pipeline_version_required():
    raw = _real_config_dict()
    raw.pop("pipeline_version", None)
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(raw)


def test_pipeline_version_read_from_yaml():
    raw = _real_config_dict()
    cfg = PipelineConfig.model_validate(raw)
    assert cfg.pipeline_version == raw["pipeline_version"]
