"""Look more carefully at SSR JSON structure for any item that has a file URL."""
import re

with open("tools/whiteoak_resources_filtered.html") as f:
    body = f.read()

# Look for any place where 'attributes' contains a 'url' or 'data' that holds a content.whiteoakamc.com PDF URL
# The pattern would be e.g. \"url\":\"...content.whiteoakamc.com/...\"
for m in re.finditer(r'attributes\\":\{[^}]{0,2000}content\.whiteoakamc\.com', body):
    s = m.start()
    e = min(len(body), s + 3000)
    print("HIT:")
    print(body[s:e])
    print("---")
    break

# Also look at where SIP_Report_Feb_2026 lives in the JSON
for m in re.finditer(r"SIP_Report_Feb_2026", body):
    s = max(0, m.start() - 600)
    e = min(len(body), m.end() + 200)
    print("SIP_Report context:")
    print(body[s:e])
    print("---")
    break
