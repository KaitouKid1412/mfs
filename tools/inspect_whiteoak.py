"""Inspect the rendered HTML around id=4271 to find the file URL."""
import re

with open("tools/whiteoak_resources_filtered.html") as f:
    body = f.read()

for pat in [r'\"id\":4271', r'id\\":4271', r'id\\\":4271']:
    for m in re.finditer(pat, body):
        s = max(0, m.start() - 600)
        e = min(len(body), m.end() + 6000)
        print(f"=== pattern={pat!r} hit @{m.start()} ===")
        print(body[s:e])
        print("=== /chunk ===")
        break

# Also check what file URLs are near "Factsheet as at April 30"
needle = "Factsheet as at April 30, 2026"
for m in re.finditer(re.escape(needle), body):
    s = max(0, m.start() - 200)
    e = min(len(body), m.end() + 6000)
    print(f"=== '{needle}' hit @{m.start()} ===")
    print(body[s:e])
    print("=== /needle ===")
    break
