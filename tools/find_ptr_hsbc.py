"""Scan all pages of HSBC factsheet for 'Turnover' / 'PTR' anchors and report
the line context.
"""
from __future__ import annotations
import re
from pathlib import Path

import pdfplumber

PDF = Path("data/raw/factsheets/hsbc/2026-04.pdf")


def main() -> int:
    with pdfplumber.open(PDF) as pdf:
        for i, page in enumerate(pdf.pages):
            pno = i + 1
            text = page.extract_text() or ""
            for ln in text.splitlines():
                low = ln.lower()
                if "turnover" in low or "ptr" in low:
                    print(f"p{pno:>3}: {ln.strip()[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
