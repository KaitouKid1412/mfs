"""Look for any doc in the SSR JSON that has a download_media_file with a PDF URL,
particularly under 'document_category' or 'document_subcategory'."""
import re

with open("tools/whiteoak_resources_filtered.html") as f:
    body = f.read()

# Search the SSR JSON for "Factsheet" inside document_subcategory tags AND nearby file URLs
print("=== document_subcategories blocks ===")
for m in re.finditer(r'document_subcategories[^{]{0,200}', body):
    print("  ", m.group(0).replace("\\", "")[:300])

# Pull any 'document_category' chunks
for m in re.finditer(r'document_category', body):
    s = max(0, m.start() - 200)
    e = min(len(body), m.end() + 400)
    ctx = body[s:e]
    if "Factsheet" in ctx:
        print(ctx[:600])
        print("---")
        break

# Find all docs with title containing "Factsheet" and inspect for URL
print("\n=== docs with title containing 'Factsheet' ===")
# Each doc-row in this JSON has format {"id":N,"attributes":{"title":"...", ...other fields...}}
# Some entries may include `document_file` or `download_media_file` after `search_keywords`
# Let's pull a fuller chunk from a doc with explicit file
for m in re.finditer(r'"title":"([^"]*[Ff]actsheet[^"]*)"', body):
    s = max(0, m.start() - 200)
    e = min(len(body), m.end() + 2000)
    ctx = body[s:e]
    has_url = "download_media_file" in ctx or "document_file" in ctx or "UploadFile" in ctx or "content.whiteoakamc.com" in ctx
    if has_url:
        print(f"\n>>> {m.group(1)!r}")
        print(ctx[:2000])
        print("<<<")
        break
