"""Find the actual PDF URL for WhiteOak Capital April 2026 factsheet."""
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


def fetch(url: str) -> tuple[int, str]:
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60)
    try:
        r = client.get(url)
        return r.status_code, r.text
    finally:
        client.close()


def main() -> int:
    url = "https://mf.whiteoakamc.com/forms-and-downloads"
    sc, body = fetch(url)
    print(f"status={sc} len={len(body)}")
    # Search for "Factsheet - Apr-26" entries and adjacent URLs
    # Look for patterns near Factsheet
    for needle in ("Factsheet - Apr-26", "Factsheet - Apr -26", "Factsheet-Apr-26",
                   "Factsheet - May-26", "Factsheet - Apr-26", "Factsheet Apr-26",
                   "Apr-26", "April 2026", "April-2026", "Apr_2026", "Apr 26"):
        for m in re.finditer(re.escape(needle), body, re.IGNORECASE):
            s = max(0, m.start() - 300)
            e = min(len(body), m.end() + 600)
            print(f"\n=== HIT '{needle}' @{m.start()} ===")
            print(body[s:e])
    # Look for any PDF URLs containing 'factsheet' (case insensitive)
    print("\n=== ALL factsheet PDF URLs (case insensitive) ===")
    for u in sorted(set(re.findall(r'https?://[^"\'<>\s\\)]+?\.[Pp][Dd][Ff]', body))):
        if "factsheet" in u.lower() or "fact_sheet" in u.lower():
            print(u)

    # Look for URLs near "Factsheet" entries
    print("\n=== content.whiteoakamc.com URLs (all) ===")
    urls = set(re.findall(r'https?://[^"\'<>\s\\)]+?\.[Pp][Dd][Ff]', body))
    # group by year-month token in name
    apr_urls = [u for u in urls if re.search(r'Apr', u, re.IGNORECASE) and re.search(r'26|2026', u)]
    print("Apr 2026:", apr_urls)
    may_urls = [u for u in urls if re.search(r'May', u, re.IGNORECASE) and re.search(r'26|2026', u)]
    print("May 2026:", may_urls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
