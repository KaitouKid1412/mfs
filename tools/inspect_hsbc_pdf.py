"""Dump per-page text & spot-check PTR/AUM/scheme-title patterns in HSBC factsheet."""
from __future__ import annotations
import re
import sys
from pathlib import Path

import pdfplumber

PDF = Path("data/raw/factsheets/hsbc/2026-04.pdf")


def main(args: list[str]) -> int:
    pages_arg = args[0] if args else None
    targets: set[int] | None = None
    if pages_arg:
        targets = set(int(p) for p in pages_arg.split(","))
    with pdfplumber.open(PDF) as pdf:
        total = len(pdf.pages)
        print(f"=== HSBC factsheet 2026-04: {total} pages ===")
        for i, page in enumerate(pdf.pages):
            pno = i + 1
            if targets and pno not in targets:
                continue
            text = page.extract_text() or ""
            head = text[:600]
            lower = text.lower()
            has_turn = "turnover" in lower or "ptr" in lower
            has_aum = "aum" in lower
            head_clean = head.replace("\n", " | ")
            print(f"\n--- page {pno} aum={has_aum} ptr={has_turn} ---")
            print(head_clean[:500])
            if targets:
                # full dump for explicitly-asked pages
                print("FULL:")
                print(text[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
