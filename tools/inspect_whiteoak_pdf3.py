"""Verify scheme-name parsing for multi-line titles and find debt pages without PTR."""
import logging
import re
import pdfplumber

logging.getLogger("pdfminer").setLevel(logging.ERROR)

PDF = "data/raw/factsheets/whiteoak_capital/2026-04.pdf"

with pdfplumber.open(PDF) as pdf:
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        lines = [l.strip() for l in text.splitlines() if l.strip()][:5]
        first2 = " ".join(lines[:2])
        if "WhiteOak Capital" in first2 and any(k in text for k in ("Month End AUM", "Monthly Average AUM")):
            print(f"\n=== page {i+1} first lines: ===")
            for l in lines:
                print("  ", l)
            print("---")
