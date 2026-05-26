"""Inspect page 87 (no scheme-name match) to ensure we're not missing a fund."""
import pdfplumber

with pdfplumber.open("data/raw/factsheets/kotak/2026-04.pdf") as pdf:
    page = pdf.pages[86]
    text = page.extract_text() or ""
    print(text[:2500])
