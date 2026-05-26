"""Fetch /resources and look for the listing of factsheets with file URLs."""
from __future__ import annotations
import json
import re

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,*/*",
}


def main() -> int:
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60)
    try:
        # try multiple resource pages
        for u in [
            "https://mf.whiteoakamc.com/resources",
            "https://mf.whiteoakamc.com/resources?resource-type=downloads",
            "https://mf.whiteoakamc.com/resources?resource-type=downloads&category=factsheet",
            "https://mf.whiteoakamc.com/resources?resource-type=downloads&category=factsheet&subcategory=monthly-factsheet",
            "https://mf.whiteoakamc.com/resources?resource-type=downloads&category=factsheet&subcategory=Monthly%20Factsheet",
            "https://mf.whiteoakamc.com/resources?resource-type=downloads&category=Factsheet&subcategory=Monthly%20Factsheet",
        ]:
            r = client.get(u, timeout=30)
            print(f"\n=== {u} -> {r.status_code} len={len(r.text)} ===")
            # find PDF URLs
            for p in sorted(set(re.findall(r'https?://content\.whiteoakamc\.com/[^\"\\\\\s]+?\.[Pp][Dd][Ff]', r.text))):
                if "Factsheet" in p or "factsheet" in p.lower():
                    print("  FS_PDF:", p)
            # find titles in 'name' field that may correspond
            for m in re.finditer(r'"name":"([^"]*Factsheet[^"]*)"', r.text):
                print("  NAME:", m.group(1))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    main()
