"""Exhaustive search for any PTR-related numeric in the quant factsheet PDF."""
import logging
import re

import pdfplumber

logging.getLogger("pdfminer").setLevel(logging.ERROR)

PDF = "data/raw/factsheets/quant/2026-04.pdf"

with pdfplumber.open(PDF) as pdf:
    for i, p in enumerate(pdf.pages):
        text = p.extract_text() or ""
        for ln in text.splitlines():
            low = ln.lower()
            if ("turnover" in low or "ptr" in low or "churn" in low) and re.search(r"\d", ln):
                print(f"pg{i+1}: {ln.strip()[:200]}")
