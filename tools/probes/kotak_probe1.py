"""One-off probe for calibrating the Kotak adapter against the cached PDF."""
import pdfplumber

with pdfplumber.open("data/raw/factsheets/kotak/2026-04.pdf") as pdf:
    for page_i in [17, 29, 8, 14, 38, 40]:
        page = pdf.pages[page_i - 1]
        words = page.extract_words(use_text_flow=True)
        kotak_words = [w for w in words if "KOTAK" in w["text"].upper()]
        print("===== PAGE", page_i, "=====")
        if kotak_words:
            k = kotak_words[0]
            row = [w for w in words if abs(w["bottom"] - k["bottom"]) < 4]
            row.sort(key=lambda w: w["x0"])
            print("  KOTAK row y=%.1f:" % k["bottom"], repr(" ".join(w["text"] for w in row)))
            ys = sorted({round(w["bottom"]) for w in words})
            idx = ys.index(round(k["bottom"]))
            for next_y in ys[idx + 1 : idx + 4]:
                next_row = [w for w in words if abs(w["bottom"] - next_y) < 4]
                next_row.sort(key=lambda w: w["x0"])
                print("  next y", next_y, ":", repr(" ".join(w["text"] for w in next_row)))
        print()
