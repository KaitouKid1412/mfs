"""Calibrate scheme-page detection + PTR/AUM extraction over the ITI factsheet.

Prints, per page:
    page_idx | scheme_name | aum | ptr

Run::
    uv run python tools/iti_probe/calibrate.py
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pdfplumber

logging.getLogger("pdfminer").setLevel(logging.ERROR)

PDF = Path("data/raw/factsheets/iti/2026-04.pdf")


# Scheme name: first non-empty line that begins with "ITI " (case-sensitive
# enough — ITI's title-case is consistent across all schemes; the first line
# is the scheme's printed title).
_NAME_RE = re.compile(r"^(ITI\b[^\n]*?)$")

# AUM "AUM (in ` Cr) 1,364.64". The ` is pdfplumber's rendering of the
# rupee glyph. We need to NOT match AAUM (Average AUM).
_AUM_RE = re.compile(
    r"(?<!A)AUM\s*\(in\s*[`₹]\s*Cr\)\s*([\d,]+\.\d{1,2})",
    re.IGNORECASE,
)

# PTR — "Portfolio Turnover Ratio 1.08". May be "NA" for new schemes.
_PTR_RE = re.compile(r"Portfolio\s+Turnover\s+Ratio\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
_PTR_NA_RE = re.compile(r"Portfolio\s+Turnover\s+Ratio\s+NA\b", re.IGNORECASE)


_SCHEME_NAME_RE = re.compile(
    r"^(ITI\b[\w\s&\-’'.]*?(?:Fund|FOF))\s*(?:\([^)]*\))?\s*$",
    re.IGNORECASE,
)


def scheme_name(text: str) -> str | None:
    """Return the printed scheme name, or None for non-scheme pages.

    A scheme detail page always carries an ``ITI ... Fund`` (or FOF) title
    line. On most pages it's literally the first non-empty line, but a
    handful of pages render the title after a banner / market-commentary
    block (e.g. p20 ``ITI Large & Mid Cap Fund``). We scan the first 30
    non-empty lines and accept the first ITI line that matches the
    ``ITI ... Fund`` shape AND is not a section heading
    (``Ready Reckoner``).
    """
    if not text:
        return None
    if "Ready Reckoner" in text[:400]:
        return None
    # Gate: scheme detail pages always carry both ``CATEGORY OF SCHEME``
    # and ``PORTFOLIO DETAILS`` (or the lowercase variant for some pages).
    if "CATEGORY OF SCHEME" not in text or "PORTFOLIO DETAILS" not in text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines[:60]:
        if ln.lower().startswith("iti mutual fund"):
            continue
        m = _SCHEME_NAME_RE.match(ln)
        if not m:
            continue
        name = m.group(1).strip()
        # Drop a trailing parenthetical scheme-old-name note in case it
        # slips into the match.
        name = re.sub(r"\s*\(.*\)\s*$", "", name).strip()
        return name
    return None


def main() -> None:
    with pdfplumber.open(PDF) as pdf:
        ptr_rows = []
        aum_rows = []
        scheme_pages = 0
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            name = scheme_name(text)
            if not name:
                continue
            scheme_pages += 1
            aum_m = _AUM_RE.search(text)
            aum = float(aum_m.group(1).replace(",", "")) if aum_m else None
            ptr_m = _PTR_RE.search(text)
            ptr_na = _PTR_NA_RE.search(text)
            if ptr_na:
                ptr = "NA"
            elif ptr_m:
                ptr = float(ptr_m.group(1))
            else:
                ptr = None
            print(f"p{i:02d} | {name[:60]:60s} | AUM={aum} | PTR={ptr}")
            if aum and aum > 0:
                aum_rows.append((name, aum))
            if isinstance(ptr, float) and ptr > 0:
                ptr_rows.append((name, ptr))

    print()
    print(f"scheme_pages_detected: {scheme_pages}")
    print(f"aum_rows: {len(aum_rows)}")
    print(f"ptr_rows: {len(ptr_rows)}")


if __name__ == "__main__":
    main()
