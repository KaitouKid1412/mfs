"""Atomic on-disk writes.

Write to a temp sibling, then ``os.replace`` it onto the destination. ``replace``
is atomic within a filesystem, so a process killed mid-write never leaves a
truncated file at the destination path — the destination is either the old
content or the complete new content, never a partial.

This matters because several cache-hit checks in the ingest layer trust a file's
mere existence (or a loose size floor) as "valid cache". A non-atomic write that
is interrupted would otherwise strand a truncated file that the next run reuses
as if complete (e.g. a half-downloaded RBI press release silently dropping an
auction, or a truncated bhavcopy under-counting ADV).
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (tmp sibling + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        # On success the tmp was renamed away; on failure remove the partial.
        tmp.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically."""
    atomic_write_bytes(path, text.encode(encoding))
