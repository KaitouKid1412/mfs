"""Probe WhiteOak Strapi/Next.js API endpoints for the factsheet file URL."""
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
        # First fetch forms-and-downloads and look for build-id + API endpoints
        r = client.get("https://mf.whiteoakamc.com/forms-and-downloads")
        body = r.text
        print("forms-and-downloads len:", len(body))
        # Look for buildId, RSC payload references to content.whiteoakamc.com / strapi / api routes
        # Try a few known Strapi endpoint shapes
        for path in [
            "/api/documents?filters[id][$eq]=4271&populate=*",
            "/api/document/4271?populate=*",
            "/api/documents/4271?populate=*",
        ]:
            try:
                rr = client.get(f"https://mf.whiteoakamc.com{path}")
                print(f"\n--- {path} {rr.status_code} ---")
                print(rr.text[:1000])
            except Exception as e:
                print(f"{path} ERR {e}")
        # Look at Next.js _next/data
        m = re.search(r'"buildId":"([^"]+)"', body)
        if m:
            bid = m.group(1)
            print("\nbuildId:", bid)
            for path in [
                f"/_next/data/{bid}/forms-and-downloads.json",
            ]:
                try:
                    rr = client.get(f"https://mf.whiteoakamc.com{path}")
                    print(f"\n--- {path} {rr.status_code} len={len(rr.text)} ---")
                    print(rr.text[:600])
                except Exception as e:
                    print(f"{path} ERR {e}")

        # Try content.whiteoakamc.com — Strapi typically lives there
        for path in [
            "/api/documents?filters[id][$eq]=4271&populate=*",
            "/api/documents?filters[title][$contains]=Factsheet%20as%20at%20April&populate=*",
        ]:
            try:
                rr = client.get(f"https://content.whiteoakamc.com{path}")
                print(f"\n--- content{path} {rr.status_code} ---")
                print(rr.text[:1500])
            except Exception as e:
                print(f"content{path} ERR {e}")

        # Scan the body for 'document_file' / 'file' / 'upload' / 'attachment' references near id 4271
        for m in re.finditer(r'"id":4271\b', body):
            s = m.start()
            ctx = body[s : s + 8000]
            # Show any URL-like tokens
            for u in re.finditer(r"https?://[^\\\s\"',<>]+", ctx):
                print("URL-near-4271:", u.group(0))
            break
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
