"""Dump per-page text from the ITI factsheet for calibration."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pdfplumber

logging.getLogger("pdfminer").setLevel(logging.ERROR)

PDF = Path(sys.argv[1] if len(sys.argv) > 1 else "data/raw/factsheets/iti/2026-04.pdf")

with pdfplumber.open(PDF) as pdf:
    print(f"pages: {len(pdf.pages)}", file=sys.stderr)
    pages_arg = sys.argv[2] if len(sys.argv) > 2 else "all"
    if pages_arg == "all":
        page_ids = range(1, len(pdf.pages) + 1)
    else:
        page_ids = [int(x) for x in pages_arg.split(",")]
    for pn in page_ids:
        page = pdf.pages[pn - 1]
        text = page.extract_text() or ""
        print(f"\n=== page {pn} ===")
        print(text)
