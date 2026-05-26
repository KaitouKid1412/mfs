"""Extract scheme name on every page using size-based heuristic.

Rule: collect words on the page where size >= 14, fontname contains 'Bold',
x0 >= 0 (filter sidebar/page-rotation artifacts that have negative x),
y < 60 (header band).
"""
import pdfplumber
import re

with pdfplumber.open("data/raw/factsheets/kotak/2026-04.pdf") as pdf:
    pages_with_kotak_header = 0
    pages_without = []
    for i, page in enumerate(pdf.pages):
        words = page.extract_words(use_text_flow=True, extra_attrs=["size", "fontname"])
        header_words = [
            w
            for w in words
            if w.get("size", 0) and w["size"] >= 14
            and "Bold" in (w.get("fontname") or "")
            and w["x0"] >= 0
            and w["bottom"] < 60
        ]
        if not header_words:
            continue
        header_words.sort(key=lambda w: (round(w["bottom"]), w["x0"]))
        # Group by approx y (within 4pt)
        groups = []
        for w in header_words:
            if groups and abs(w["bottom"] - groups[-1][-1]["bottom"]) < 4:
                groups[-1].append(w)
            else:
                groups.append([w])
        # Build name from joined rows
        rows = [" ".join(w["text"] for w in g) for g in groups]
        name = " ".join(rows)
        if not name.upper().startswith("KOTAK"):
            continue
        pages_with_kotak_header += 1
        if i + 1 in (8, 11, 17, 20, 29, 38, 40):
            print(f"page {i+1}: {name!r}")
    print(f"\nTotal pages with KOTAK header: {pages_with_kotak_header}")
