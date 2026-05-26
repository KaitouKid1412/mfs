"""Locate FUND SIZE / AUM tokens on each quant factsheet detail page."""
import logging
import re

import pdfplumber

logging.getLogger("pdfminer").setLevel(logging.ERROR)

PDF = "data/raw/factsheets/quant/2026-04.pdf"

with pdfplumber.open(PDF) as pdf:
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        if "FUND SIZE" not in text:
            continue
        words = page.extract_words(use_text_flow=True)
        # FUND SIZE label is in the top-right (x0 > 500).
        anchor = None
        for w in words:
            if w["text"] == "FUND" and 470 < w["x0"] < 600 and w["top"] < 130:
                anchor = w
                break
        if anchor is None:
            print(f"pg{i+1} FUND SIZE present in text but no top-right FUND anchor found")
            continue
        # Now find Rs glyph + number + cr / bn / lakh in the same column,
        # within ~120pt below the FUND SIZE anchor.
        rels: list[tuple[float, float, str]] = []
        for v in words:
            if v["top"] <= anchor["bottom"]:
                continue
            if v["top"] - anchor["bottom"] > 120:
                continue
            if v["x0"] < 470:
                continue
            rels.append((v["top"], v["x0"], v["text"]))
        rels.sort()
        # Show first ~12 tokens after anchor
        title = text.splitlines()[0] if text.strip() else "?"
        print(f"pg{i+1}  scheme={title!r}")
        for r in rels[:14]:
            print(f"   y={r[0]:6.1f} x0={r[1]:6.1f} text={r[2]!r}")
        print()
