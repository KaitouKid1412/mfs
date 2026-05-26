"""Show all attribute fields present in a form-and-downloads doc entry."""
import re

with open("tools/whiteoak_resources_filtered.html") as f:
    body = f.read()

# Get the first 'attributes":{"title": ' chunk that has more keys after 'search_keywords'
# Compare with a doc that contains 'download_media_file' to see if any do.
print("=== unique field names in attributes blocks ===")
fields = set()
for m in re.finditer(r'attributes\\":\{[^}]+\}', body):
    s = m.group(0)
    # extract field names (escaped quotes)
    for f in re.findall(r'\\"(\w+)\\":', s):
        fields.add(f)
print(sorted(fields))

print("\n=== check if ANY doc has download_media_file/document_file ===")
print("has download_media_file:", "download_media_file" in body)
print("has document_file:", "document_file" in body)
print("has document_attachment:", "document_attachment" in body)
print("has UploadFile:", "UploadFile" in body)

# Find the FIRST doc in the SSR list with each field
for needle in ["download_media_file", "document_file", "document_attachment"]:
    for m in re.finditer(needle, body):
        s = max(0, m.start() - 200)
        e = min(len(body), m.end() + 1500)
        print(f"\n--- '{needle}' at {m.start()} ---")
        print(body[s:e][:1500])
        break

# Try to find a doc whose URL is referenced via 'document.X' inside a card
# Look for {document, title} or {file:{ pdf-url }}
print("\n=== content.whiteoakamc.com PDF URLs near 'monthly factsheet' or 'Factsheet' ===")
for m in re.finditer(r'https?://content\.whiteoakamc\.com/[^"\\\s\'<>`]+?\.pdf', body, re.IGNORECASE):
    u = m.group(0)
    print(u)
