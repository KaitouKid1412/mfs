"""Tests for atomic on-disk writes (mfs.io.atomic)."""

from __future__ import annotations

from mfs.io.atomic import atomic_write_bytes, atomic_write_text


def test_write_bytes_creates_parent_and_no_leftover_tmp(tmp_path):
    p = tmp_path / "nested" / "f.bin"
    atomic_write_bytes(p, b"hello")
    assert p.read_bytes() == b"hello"
    assert list(p.parent.glob("*.tmp")) == []


def test_write_bytes_overwrites(tmp_path):
    p = tmp_path / "f.bin"
    atomic_write_bytes(p, b"old")
    atomic_write_bytes(p, b"newer-content")
    assert p.read_bytes() == b"newer-content"
    assert list(p.parent.glob("*.tmp")) == []


def test_write_text_roundtrip(tmp_path):
    p = tmp_path / "f.txt"
    atomic_write_text(p, "héllo wörld")
    assert p.read_text() == "héllo wörld"
    assert list(p.parent.glob("*.tmp")) == []
