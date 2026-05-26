"""Inspect page 87 word layout to see why scheme name was missed."""
import pdfplumber

with pdfplumber.open("data/raw/factsheets/kotak/2026-04.pdf") as pdf:
    page = pdf.pages[86]
    words = page.extract_words(use_text_flow=True, extra_attrs=["size", "fontname"])
    band = [w for w in words if w["bottom"] < 70]
    band.sort(key=lambda w: (round(w["bottom"]), w["x0"]))
    print("===== PAGE 87 header band =====")
    for w in band[:40]:
        print(
            f"  y={w['bottom']:6.2f}  x0={w['x0']:7.2f}  size={w.get('size','?'):>6}"
            f"  font={(w.get('fontname') or '')[:25]:25}  {w['text']!r}"
        )
