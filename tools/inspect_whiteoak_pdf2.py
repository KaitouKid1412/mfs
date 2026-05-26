"""Find PTR / AUM markers in WhiteOak factsheet."""
import logging
import re
import pdfplumber

logging.getLogger("pdfminer").setLevel(logging.ERROR)

PDF = "data/raw/factsheets/whiteoak_capital/2026-04.pdf"

with pdfplumber.open(PDF) as pdf:
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        if "Turnover" in text or "AUM" in text or "Fund Size" in text or "Portfolio Turnover" in text:
            line0 = text.split("\n", 1)[0] if text else ""
            print(f"\n=== page {i+1} first line: {line0!r} ===")
            # Show lines containing the markers
            for ln in text.splitlines():
                if any(k in ln for k in ("Turnover", "AUM", "Fund Size", "Portfolio Turn")):
                    print("  ", repr(ln))
