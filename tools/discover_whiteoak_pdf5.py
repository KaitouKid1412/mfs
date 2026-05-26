"""Diff the SSR HTML of /forms-and-downloads vs /resources?downloads&factsheet,
and search for file URL fields associated with id 4271 (Factsheet Apr-2026).
"""
from __future__ import annotations
import re
import sys

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def main() -> int:
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60)
    try:
        # 1. fetch resources factsheet category page
        r = client.get("https://mf.whiteoakamc.com/resources?resource-type=downloads&category=factsheet&page=1")
        body = r.text
        out = "tools/whiteoak_resources_filtered.html"
        with open(out, "w") as f:
            f.write(body)
        print("resources?factsheet len:", len(body))

        # Show all URLs containing 'factsheet' / 'Factsheet'
        for u in sorted(set(re.findall(r'https?://[^\\\\\s"\'<>)\]]+\.[Pp][Dd][Ff]', body))):
            if "factsheet" in u.lower():
                print("PDF:", u)
        # Look for any content.whiteoakamc.com URLs
        cb = set(re.findall(r'https?://content\.whiteoakamc\.com/[^\\\\\s"\'<>)\]]+', body))
        print("content.whiteoakamc.com hits:", len(cb))
        # Show any factsheet-ish ones
        for u in sorted(cb):
            if "factsheet" in u.lower() or "fact" in u.lower():
                print("CONTENT:", u)
        # Search for the dctermsTitle / document-id patterns
        for m in re.finditer(r'"id":4271[^,]*?,(?:[^,]*?,){0,30}', body):
            print("4271 chunk:", m.group(0)[:1500])
            break
        # As a sanity check, see how SOME other category (like Investment Acorns) renders its file URLs
        # That'll show whether the document_attachment field is exposed
        m = re.search(r'"title":"Investment Acorns[^"]*"', body)
        if m:
            s = max(0, m.start() - 200)
            e = min(len(body), m.end() + 1500)
            print("\n--- Acorns chunk ---")
            print(body[s:e])

        # Try /resources directly with no filter and an Apr-26 ID search
        # Look for SSR-rendered tile hrefs
        for m in re.finditer(r'href="([^"]*?)"[^>]*?>[^<]{0,80}Factsheet[^<]{0,80}', body):
            print("HREF-near-factsheet:", m.group(0)[:300])

        # Also try /factsheet directly with referer
        r2 = client.get("https://mf.whiteoakamc.com/factsheet")
        body2 = r2.text
        print("/factsheet status:", r2.status_code, "len:", len(body2))
        with open("tools/whiteoak_factsheet_page.html", "w") as f:
            f.write(body2)
        # see if /factsheet rendered with anchors
        anchors = re.findall(r'<a[^>]+href="([^"]*?(?:\.pdf|/[^"]*?factsheet[^"]*?))"', body2, re.IGNORECASE)
        for a in anchors[:20]:
            print("/factsheet anchor:", a)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
