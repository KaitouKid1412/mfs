"""Probe WhiteOak Strapi CMS for the factsheet PDF URL."""
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
        # Strapi's standard pattern
        # Try various Strapi v3/v4 endpoints
        bases = [
            "https://cms.whiteoakamc.com",
            "https://cms.mf.whiteoakamc.com",
            "https://cmsmf.whiteoakamc.com",
            "https://api.whiteoakamc.com",
            "https://strapi.whiteoakamc.com",
        ]
        for base in bases:
            for path in [
                "/api/documents/4271?populate=*",
                "/api/documents?filters[id][$eq]=4271&populate=*",
                "/api/form-documents/4271?populate=*",
                "/api/form-and-downloads/4271?populate=*",
                "/api/document/4271?populate=*",
            ]:
                try:
                    r = client.get(f"{base}{path}", timeout=10)
                    if r.status_code in (200, 401, 403):
                        print(f"\n{base}{path} -> {r.status_code}")
                        print(r.text[:500])
                except Exception as e:
                    print(f"{base}{path} ERR {type(e).__name__}")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
