"""Try fetching the page with RSC streaming to capture lazy-loaded data."""
from __future__ import annotations
import re

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {"User-Agent": UA, "Accept": "*/*"}


def main() -> int:
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60)
    try:
        # Try various Next.js Server Action / data routes
        for u in [
            "https://mf.whiteoakamc.com/resources?resource-type=downloads&category=Factsheet&subcategory=Monthly%20Factsheet&page=1&_rsc=1",
            "https://mf.whiteoakamc.com/resources.json?resource-type=downloads&category=Factsheet&subcategory=Monthly%20Factsheet&page=1",
            "https://mf.whiteoakamc.com/_next/data/n51U-CWiicGflBhrlz_TG/resources.json?resource-type=downloads&category=Factsheet&subcategory=Monthly%20Factsheet&page=1",
        ]:
            try:
                r = client.get(u, headers={**HEADERS, "rsc": "1"}, timeout=15)
                print(f"\n--- {u}")
                print(f"  -> {r.status_code} {r.headers.get('content-type','')}")
                t = r.text
                pdfs = sorted(set(re.findall(r'https?://content\.whiteoakamc\.com/[^"\\\s\'<>`]+?\.[Pp][Dd][Ff]', t)))
                fact_pdfs = [p for p in pdfs if "factsheet" in p.lower()]
                print(f"  factsheet pdfs: {len(fact_pdfs)}")
                for p in fact_pdfs[:20]:
                    print("   ", p)
                # show first 800
                print("    preview:", t[:600])
            except Exception as e:
                print(f"  ERR {e}")

        # Try POSTing to a server action
        # Next 13 app router server actions look like:
        #   POST /resources?_action=lookup
        # but invoking them requires the action id from JS. Skipping.

        # Probe sitemap for /resources paginated archive
        for slug in [
            "/resources?resource-type=downloads&category=Factsheet",
            "/resources?resource-type=downloads&category=Factsheet&page=1",
            "/resources?resource-type=downloads&category=Factsheet&page=2",
        ]:
            try:
                r = client.get(
                    f"https://mf.whiteoakamc.com{slug}",
                    headers={**HEADERS, "rsc": "1", "Next-Action": "x", "Content-Type": "text/x-component"},
                    timeout=15,
                )
                pdfs = sorted(set(re.findall(r'https?://content\.whiteoakamc\.com/[^"\\\s\'<>`]+?\.[Pp][Dd][Ff]', r.text)))
                fact = [p for p in pdfs if "factsheet" in p.lower()]
                print(f"\n{slug} -> {r.status_code} factsheet hits:", len(fact))
                for p in fact[:20]:
                    print("   ", p)
            except Exception as e:
                print(f"  ERR {e}")

    finally:
        client.close()
    return 0


if __name__ == "__main__":
    main()
