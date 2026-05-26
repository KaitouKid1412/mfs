"""Look at how the resources listing on the rendered page links each tile."""
import re

with open("tools/whiteoak_resources_filtered.html") as f:
    body = f.read()

# Find any anchor (<a href=...>) in the rendered HTML pointing to .pdf
hits = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>([^<]{0,160})</a>', body)
print("anchors with text count:", len(hits))
pdf_hits = [(h, t) for h, t in hits if h.lower().endswith(".pdf") or "factsheet" in t.lower()]
print("pdf-or-factsheet anchors:", len(pdf_hits))
for h, t in pdf_hits[:40]:
    print(f"  text={t!r}\n   href={h}")

# Check if the page has cards that link to dynamic /resources/<slug>
slug_hits = [(h, t) for h, t in hits if "/resources/" in h or "/factsheet" in h.lower()]
print("\nslug anchors:", len(slug_hits))
for h, t in slug_hits[:20]:
    print(f"  text={t!r}\n   href={h}")

# Look for the SSR card area: 'Factsheet as at' would appear in rendered card text
print("\n--- around rendered card ---")
for m in re.finditer(r"Factsheet[^<]{0,40}", body):
    s = max(0, m.start() - 200)
    e = min(len(body), m.end() + 300)
    ctx = body[s:e]
    if "<a" in ctx or "href" in ctx:
        print(ctx[:600])
        print("---")
        if "March 31" in ctx or "April 30" in ctx:
            break
