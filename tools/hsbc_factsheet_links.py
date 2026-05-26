"""Extract HSBC monthly factsheet PDF links from the investor-resources page."""
from __future__ import annotations
import re

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def main() -> int:
    url = "https://www.assetmanagement.hsbc.co.in/en/mutual-funds/investor-resources?Doc=fund-factsheets"
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60) as c:
        r = c.get(url)
    html = r.text
    # Look for all `the-asset` PDFs
    pdfs = re.findall(
        r"""https://www\.assetmanagement\.hsbc\.co\.in/assets/documents/mutual-funds/en/[a-f0-9\-]+/the-asset[^"'<>\s]+\.pdf""",
        html,
    )
    print(f"Found {len(pdfs)} 'the-asset' PDF URLs:")
    for p in sorted(set(pdfs)):
        print(" ", p)
    # Also fund-factsheets data-date markers
    print("\n--- data-date markers ---")
    print(set(re.findall(r'data-date="(\d{4})"', html)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
