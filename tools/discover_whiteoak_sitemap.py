"""Pull the WhiteOak sitemap and look for factsheet-related slugs."""
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
        for url in [
            "https://mf.whiteoakamc.com/sitemap.xml",
            "https://mf.whiteoakamc.com/sitemap-0.xml",
            "https://mf.whiteoakamc.com/sitemap-1.xml",
            "https://mf.whiteoakamc.com/robots.txt",
        ]:
            try:
                r = client.get(url, timeout=15)
                print(f"\n--- {url} -> {r.status_code} len={len(r.text)} ---")
                print(r.text[:3000])
            except Exception as e:
                print(f"{url} ERR {e}")

        # Also check content.whiteoakamc.com root
        r = client.get("https://content.whiteoakamc.com/", timeout=15)
        print(f"\n--- content/ -> {r.status_code} ---")
        print(r.text[:1500])

        # Try listing S3 bucket
        for url in [
            "https://content.whiteoakamc.com/?list-type=2",
            "https://content.whiteoakamc.com/?prefix=Factsheet",
            "https://content.whiteoakamc.com/?prefix=Factsheet_",
            "https://content.whiteoakamc.com/?prefix=WhiteOak_Capital_Factsheet",
        ]:
            try:
                r = client.get(url, timeout=15)
                print(f"\n--- {url} -> {r.status_code} ---")
                print(r.text[:1500])
            except Exception as e:
                print(f"{url} ERR {e}")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
