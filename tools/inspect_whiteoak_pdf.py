"""Inspect WhiteOak Apr-2026 factsheet PDF layout."""
import logging
import pdfplumber

logging.getLogger("pdfminer").setLevel(logging.ERROR)

PDF = "data/raw/factsheets/whiteoak_capital/2026-04.pdf"

with pdfplumber.open(PDF) as pdf:
    print(f"pages: {len(pdf.pages)}")
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        line0 = text.split("\n", 1)[0] if text else ""
        # Show first 5, last 5, and all containing "Turnover" or "AUM" markers
        if i < 5 or i >= len(pdf.pages) - 3 or "Turnover" in text or "AUM" in text:
            print(f"\n--- page {i+1} len={len(text)} first line: {line0!r} ---")
            print(text[:1200])
