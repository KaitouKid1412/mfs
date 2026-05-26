"""Examine the SSR JSON for ANY downloadable doc and isolate its URL field path."""
import json
import re

with open("tools/whiteoak_resources_filtered.html") as f:
    body = f.read()

# Strapi pattern: docs of type "Document" appear with various __typename
# Find unique __typename values
print("=== Unique __typename values ===")
for tn in sorted(set(re.findall(r'__typename\\":\\"(\w+)\\"', body))):
    print(" ", tn)

# Get titles of docs with __typename "Document"
print("\n=== sample titles of Document-typed items ===")
for m in re.finditer(r'__typename\\":\\"Document\\"[^}]+?\\"title\\":\\"([^\\]+)\\"', body):
    print(" ", m.group(1)[:80])
    if False:
        break

# Show first 5 Document objects in full
print("\n=== first 3 full Document entities ===")
for i, m in enumerate(re.finditer(r'__typename\\":\\"DocumentEntity\\".{0,2000}?(?=\\"DocumentEntity\\"|\\"id\\":\\")', body)):
    print(f"\n-- doc {i} --")
    print(m.group(0)[:1500])
    if i > 3:
        break
