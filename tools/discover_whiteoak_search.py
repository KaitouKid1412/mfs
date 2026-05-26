"""Try the WhiteOak public search endpoint used by the SPA."""
from __future__ import annotations
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
        # Probe the search/api endpoints commonly used by Next.js apps
        candidates = [
            "https://mf.whiteoakamc.com/api/search?q=Factsheet+as+at+April+30",
            "https://mf.whiteoakamc.com/api/documents?title=Factsheet+as+at+April+30",
            "https://mf.whiteoakamc.com/api/document?id=4271",
            "https://mf.whiteoakamc.com/api/documents/4271",
            "https://mf.whiteoakamc.com/api/factsheet",
            "https://mf.whiteoakamc.com/api/factsheets",
            "https://mf.whiteoakamc.com/api/form-document/4271",
            "https://mf.whiteoakamc.com/api/strapi-document/4271",
            # Static asset patterns
            "https://content.whiteoakamc.com/Factsheet_April_2026.pdf",
            "https://content.whiteoakamc.com/Factsheet_as_at_April_30_2026.pdf",
            "https://content.whiteoakamc.com/WhiteOak_Capital_Factsheet_April_2026.pdf",
            "https://content.whiteoakamc.com/Factsheet_Apr_2026.pdf",
        ]
        for u in candidates:
            try:
                r = client.head(u, timeout=15)
                print(f"HEAD {u} -> {r.status_code} ct={r.headers.get('content-type','')} len={r.headers.get('content-length','')}")
            except Exception as e:
                print(f"HEAD {u} ERR {type(e).__name__}")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
