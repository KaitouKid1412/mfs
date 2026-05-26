"""Inspection script for the cached Sundaram April 2026 factsheet PDF."""
import logging
import re
from pathlib import Path

import pdfplumber

logging.getLogger("pdfminer").setLevel(logging.ERROR)

PDF = Path("data/raw/factsheets/sundaram/2026-04.pdf")


def main() -> None:
    """Look at pages that should have PTR but didn't extract via text regex."""
    with pdfplumber.open(PDF) as pdf:
        # Check pages 25 (Global Brand FoF), 34 (Multi Asset), 38 (Income Plus Arb)
        # The mapping page summary showed they have AUM but no PTR.
        # Also check word-positions of PTR on some standard equity pages for
        # robustness validation.
        for i in (6, 7, 9, 11, 24, 27, 33, 37):  # 0-indexed
            page = pdf.pages[i]
            text = page.extract_text() or ""
            print(f"\n=== PAGE {i+1} ===")
            lines = text.splitlines()
            # find lines with Turnover or Month End or Avg.
            for ln in lines:
                if (
                    "Turnover" in ln
                    or "Month End" in ln
                    or ln.startswith("Avg")
                    or "AUM" in ln
                ):
                    print(" ", repr(ln.strip()))


if __name__ == "__main__":
    main()
