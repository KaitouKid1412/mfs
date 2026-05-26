"""Inspect quant factsheet PDF: find PTR / AUM markers and scheme-name anchors.

Usage:
  uv run python tools/probes/inspect_quant.py survey
  uv run python tools/probes/inspect_quant.py page <N>
  uv run python tools/probes/inspect_quant.py words <N>
  uv run python tools/probes/inspect_quant.py find <needle>
"""
import logging
import sys

import pdfplumber

logging.getLogger("pdfminer").setLevel(logging.ERROR)

PDF = "data/raw/factsheets/quant/2026-04.pdf"

mode = sys.argv[1] if len(sys.argv) > 1 else "survey"

with pdfplumber.open(PDF) as pdf:
    print(f"Total pages: {len(pdf.pages)}")
    if mode == "survey":
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            first = lines[:2]
            has_ptr = "Turnover" in text or "turnover" in text
            has_aum = (
                "AUM" in text or "AAUM" in text or "Fund Size" in text
                or "Net Asset" in text
            )
            tag = "EQ" if has_ptr and has_aum else ("AUM" if has_aum else ("PTR" if has_ptr else "-"))
            print(f"pg{i+1:3d} [{tag}] {first[:1]}")
    elif mode == "page":
        idx = int(sys.argv[2]) - 1
        page = pdf.pages[idx]
        text = page.extract_text() or ""
        print("=" * 60)
        print(text[:3500])
        print("=" * 60)
    elif mode == "words":
        idx = int(sys.argv[2]) - 1
        page = pdf.pages[idx]
        words = page.extract_words(use_text_flow=True)
        for w in words[:300]:
            print(f"x0={w['x0']:6.1f} y={w['top']:6.1f} x1={w['x1']:6.1f} '{w['text']}'")
    elif mode == "find":
        needle = sys.argv[2].lower()
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if needle in text.lower():
                lines = [ln for ln in text.splitlines() if needle in ln.lower()]
                print(f"--- pg{i+1} ---")
                for ln in lines[:5]:
                    print(f"  {ln}")
