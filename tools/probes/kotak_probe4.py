"""Calibrate full extraction (scheme name + PTR + AUM) on every page."""
import pdfplumber
import re

PTR_RE = re.compile(r"Portfolio\s+Turnover\s+([\d.]+)\s*%", re.IGNORECASE)
AUM_RE = re.compile(r"(?<!A)AUM[:\s]*[`₹]?\s*([\d,]+\.\d+)\s*crs?", re.IGNORECASE)


def scheme_name(page):
    words = page.extract_words(use_text_flow=True, extra_attrs=["size", "fontname"])
    header_words = [
        w
        for w in words
        if w.get("size", 0) and w["size"] >= 12.5
        and "Bold" in (w.get("fontname") or "")
        and w["x0"] >= 0
        and w["bottom"] < 60
    ]
    if not header_words:
        return None
    header_words.sort(key=lambda w: (round(w["bottom"]), w["x0"]))
    groups = []
    for w in header_words:
        if groups and abs(w["bottom"] - groups[-1][-1]["bottom"]) < 4:
            groups[-1].append(w)
        else:
            groups.append([w])
    name = " ".join(" ".join(w["text"] for w in g) for g in groups).strip()
    if not name.upper().startswith("KOTAK"):
        return None
    return name


with pdfplumber.open("data/raw/factsheets/kotak/2026-04.pdf") as pdf:
    rows_ptr = []
    rows_aum = []
    no_name_ptr = 0
    no_name_aum = 0
    seen_names_aum = []
    for i, page in enumerate(pdf.pages):
        name = scheme_name(page)
        text = page.extract_text() or ""
        m_ptr = PTR_RE.search(text)
        m_aum = AUM_RE.search(text)
        if m_ptr:
            ptr = float(m_ptr.group(1))
            if name:
                rows_ptr.append((i + 1, name, ptr))
            else:
                no_name_ptr += 1
                print(f"PTR no-name p{i+1}: {ptr}%")
        if m_aum:
            try:
                aum = float(m_aum.group(1).replace(",", ""))
            except ValueError:
                continue
            if name:
                rows_aum.append((i + 1, name, aum))
                seen_names_aum.append(name)
            else:
                no_name_aum += 1
                print(f"AUM no-name p{i+1}: {aum}cr")
    print(f"\nPTR rows: {len(rows_ptr)} (no-name skipped: {no_name_ptr})")
    print(f"AUM rows: {len(rows_aum)} (no-name skipped: {no_name_aum})")
    print(f"\nUnique scheme names with AUM: {len(set(seen_names_aum))}")
    # Sample a few
    print("\nSample PTR records:")
    for r in rows_ptr[:5]:
        print(" ", r)
    for r in rows_ptr[-5:]:
        print(" ", r)
    print("\nSample AUM records:")
    for r in rows_aum[:5]:
        print(" ", r)
    for r in rows_aum[-5:]:
        print(" ", r)
