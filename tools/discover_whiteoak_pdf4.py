"""Find how WhiteOak's site exposes the PDF file for a Document id."""
from __future__ import annotations
import json
import re
import sys

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def main() -> int:
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60)
    try:
        # 1) try the resources?resource-type=downloads&category=factsheet flow which the nav links to
        r = client.get("https://mf.whiteoakamc.com/resources?resource-type=downloads&category=factsheet&page=1")
        body = r.text
        print("resources page status:", r.status_code, "len:", len(body))
        # Save for inspection
        out = "tools/whiteoak_resources.html"
        with open(out, "w") as f:
            f.write(body)
        print(f"written to {out}")
        # Look for PDF urls
        pdfs = sorted(set(re.findall(r'https?://[^"\'<>\s\\)]+?\.[Pp][Dd][Ff]', body)))
        print("Total PDFs found:", len(pdfs))
        for p in pdfs[:80]:
            print(p)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
