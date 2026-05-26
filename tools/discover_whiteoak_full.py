"""Pull full sitemap and key JS bundles, looking for the file URL pattern."""
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
        # Sitemap
        r = client.get("https://mf.whiteoakamc.com/sitemap-0.xml", timeout=15)
        sm = r.text
        # find any URL containing 'factsheet' / 'fact-sheet'
        for u in re.findall(r"<loc>(https?://[^<]+)</loc>", sm):
            if "factsheet" in u.lower() or "fact-sheet" in u.lower() or "fact_sheet" in u.lower():
                print("SM:", u)
        # Show a summary of unique path-prefixes
        paths = sorted(set(re.findall(r"<loc>https://mf\.whiteoakamc\.com(/[^<]+)</loc>", sm)))
        print("\nPath prefix histogram (first 60):")
        for p in paths[:60]:
            print(p)
        print("\nUnique path prefixes count:", len(paths))

        # Try fetching the forms-and-downloads page from the RSC endpoint
        r = client.get(
            "https://mf.whiteoakamc.com/forms-and-downloads?_rsc=1",
            headers={**HEADERS, "rsc": "1", "Next-Router-State-Tree": "%5B%22%22%2C%7B%22children%22%3A%5B%22forms-and-downloads%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%5D%7D%5D%7D%5D"},
            timeout=30,
        )
        print(f"\nRSC forms-and-downloads -> {r.status_code} len={len(r.text)}")
        # See if PDF URLs are inside this payload
        pdfs = sorted(set(re.findall(r'https?://[^\\\\\\"\'<>\s]+?\.[Pp][Dd][Ff]', r.text)))
        for p in pdfs[:50]:
            if "factsheet" in p.lower() or "Apr" in p or "april" in p.lower() or "2026" in p:
                print(" RSC PDF:", p)

        # Inspect a key JS chunk for fileurl pattern
        # find chunks references in HTML
        h = client.get("https://mf.whiteoakamc.com/forms-and-downloads", timeout=30).text
        chunk_urls = sorted(set(re.findall(r'/_next/static/chunks/[\w/.-]+\.js', h)))
        print("\nChunk JS count:", len(chunk_urls))
        # Look at the largest-named ones first (heuristic) — try main-app and big numerics
        target_chunks = [c for c in chunk_urls if "main-app" in c or "layout" in c][:4]
        # Plus all
        for c in target_chunks + chunk_urls[:20]:
            try:
                jr = client.get(f"https://mf.whiteoakamc.com{c}", timeout=15)
                if jr.status_code != 200:
                    continue
                text = jr.text
                # Search for content.whiteoakamc.com prefix patterns
                hits = sorted(set(re.findall(r"content\.whiteoakamc\.com/[\w/.-]+", text)))
                for h2 in hits[:20]:
                    if any(k in h2.lower() for k in ["fact","upload","file","attach"]):
                        print(f"  {c} -> {h2}")
                # Look for explicit URL templates: ${ ... }/{...}.pdf
                tmpl = sorted(set(re.findall(r"`(https?://[^\s`]+?\$\{[^`]+?)`", text)))
                for t in tmpl[:6]:
                    if "factsheet" in t.lower():
                        print(f"  TMPL {c} -> {t}")
            except Exception as e:
                pass
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
