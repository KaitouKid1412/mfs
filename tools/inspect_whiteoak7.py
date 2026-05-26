"""Look at the immediate __typename context of id 4271."""
import re

with open("tools/whiteoak_resources_filtered.html") as f:
    body = f.read()

for m in re.finditer(r'\\"id\\":4271\b', body):
    s = max(0, m.start() - 800)
    e = min(len(body), m.end() + 1000)
    print("=== 4271 context ===")
    print(body[s:e])
    print("---")
    break

# Find what __typename is in the immediate context
ctx_start = body.rfind('__typename\\":\\"', 0, m.start())
ctx = body[ctx_start:ctx_start+200]
print("\nNearest __typename:", ctx[:200])
