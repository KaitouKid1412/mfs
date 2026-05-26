"""Pull the actual PDF URL for the 'Factsheet as at April 30, 2026' entry."""
from __future__ import annotations
import re
import sys

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


def main() -> int:
    url = "https://mf.whiteoakamc.com/forms-and-downloads"
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60)
    try:
        r = client.get(url)
    finally:
        client.close()
    body = r.text

    # Find the chunk around id=4271 — print ±2KB
    for m in re.finditer(r'"id":4271\b', body):
        s = max(0, m.start() - 200)
        e = min(len(body), m.end() + 4000)
        print(f"=== chunk @{m.start()} ===")
        print(body[s:e])
        print("=== /chunk ===")
        break

    # Also find any block containing "Factsheet as at April 30, 2026" and dump 3KB after
    needle = "Factsheet as at April 30, 2026"
    for m in re.finditer(re.escape(needle), body):
        s = max(0, m.start() - 200)
        e = min(len(body), m.end() + 4000)
        print(f"=== needle@{m.start()} ===")
        print(body[s:e])
        print("=== /needle ===")
        break

    # And try fetching the dedicated download page if any
    return 0


if __name__ == "__main__":
    sys.exit(main())
