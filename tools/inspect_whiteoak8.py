"""Inspect Burification entities — likely the Documents list with URLs."""
import re

with open("tools/whiteoak_resources_filtered.html") as f:
    body = f.read()

# Show one full Burification entity to see schema
for m in re.finditer(r'__typename\\":\\"Burification\\"', body):
    s = max(0, m.start() - 30)
    e = min(len(body), m.end() + 3000)
    print(body[s:e][:3000])
    print("---")
    break

# Find any Burification with title containing 'Factsheet' and dump 2k context
print("\n=== Burifications mentioning Factsheet ===")
for m in re.finditer(r'__typename\\":\\"Burification\\"[^}]*?[Ff]actsheet[^}]*?\}', body):
    print(m.group(0)[:2000])
    print("---")
