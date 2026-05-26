"""Merge Mahindra Manulife per-scheme PDFs into a combined factsheet PDF.

Uses poppler's ``pdfunite`` (system tool) since the project doesn't depend
on ``pypdf`` / ``pikepdf``. This is run once during adapter calibration.
The adapter's own ``fetch()`` also performs the same merge when downloading.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(
    "/Users/suryavamseeayyagari/mfs/data/raw/factsheets/mahindra_manulife"
)
PARTS = ROOT / "2026-04_parts"
OUT = ROOT / "2026-04.pdf"


def main() -> int:
    parts = sorted(PARTS.glob("*.pdf"))
    if not parts:
        print(f"no PDFs in {PARTS}", file=sys.stderr)
        return 1
    cmd = ["pdfunite", *[str(p) for p in parts], str(OUT)]
    print(f"merging {len(parts)} PDFs -> {OUT.name}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("STDERR:", r.stderr, file=sys.stderr)
        return r.returncode
    print(f"OK -> {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
