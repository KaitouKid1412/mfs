"""Find which pages have small KOTAK headers and see if they're scheme pages."""
import pdfplumber

with pdfplumber.open("data/raw/factsheets/kotak/2026-04.pdf") as pdf:
    for i, page in enumerate(pdf.pages):
        words = page.extract_words(use_text_flow=True, extra_attrs=["size", "fontname"])
        for w in words:
            if (
                "Bold" in (w.get("fontname") or "")
                and w["bottom"] < 35
                and w["x0"] >= 0
                and "KOTAK" in w["text"].upper()
                and 9 <= w["size"] <= 15.5
            ):
                # Show the page's first line and the header words at this y
                text = page.extract_text() or ""
                first = text.splitlines()[0:2] if text else []
                print(
                    f"page {i+1}: size={w['size']}: text head={first[:1]} ... "
                    f"has AUM={'AUM' in text} has PTR={'Portfolio Turnover' in text}"
                )
                break
