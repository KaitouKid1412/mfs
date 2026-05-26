"""Probe scheme-name word positions including x0/x1 ranges and font sizes."""
import pdfplumber

with pdfplumber.open("data/raw/factsheets/kotak/2026-04.pdf") as pdf:
    for page_i in [17, 29, 8, 14, 38, 40, 23, 37]:
        page = pdf.pages[page_i - 1]
        words = page.extract_words(use_text_flow=True, extra_attrs=["size", "fontname"])
        # All words with y between 25 and 50 (top header band)
        band = [w for w in words if 25 <= w["bottom"] <= 50]
        band.sort(key=lambda w: (round(w["bottom"]), w["x0"]))
        print(f"===== PAGE {page_i} header band =====")
        for w in band[:30]:
            print(
                f"  y={w['bottom']:6.2f}  x0={w['x0']:7.2f} x1={w['x1']:7.2f}"
                f"  size={w.get('size','?'):>4}  font={w.get('fontname','?')[:25]:25}  {w['text']!r}"
            )
        print()
