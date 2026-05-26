"""Probe size distribution of bold header words to find a safe threshold."""
import pdfplumber
from collections import Counter

with pdfplumber.open("data/raw/factsheets/kotak/2026-04.pdf") as pdf:
    size_counter = Counter()
    for i, page in enumerate(pdf.pages):
        words = page.extract_words(use_text_flow=True, extra_attrs=["size", "fontname"])
        for w in words:
            if (
                "Bold" in (w.get("fontname") or "")
                and w["bottom"] < 35
                and w["x0"] >= 0
                and "KOTAK" in w["text"].upper()
            ):
                size_counter[round(w["size"], 1)] += 1
    print("Header KOTAK-word size distribution:")
    for sz, n in sorted(size_counter.items()):
        print(f"  size={sz}: {n} pages")
