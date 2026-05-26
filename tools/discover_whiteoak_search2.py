"""Use WhiteOak's contextual search to find the Factsheet doc URL."""
from __future__ import annotations
import re

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {"User-Agent": UA, "Accept": "*/*"}


def main() -> int:
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60)
    try:
        # Site has /contextual-search endpoint
        for q in ["Factsheet+as+at+April", "Factsheet April 2026", "Monthly Factsheet"]:
            for u in [
                f"https://mf.whiteoakamc.com/contextual-search?q={q}",
                f"https://mf.whiteoakamc.com/contextual-search?searchTag={q}",
                f"https://mf.whiteoakamc.com/contextual-search?search={q}",
            ]:
                r = client.get(u, timeout=30)
                t = r.text
                # save first
                pdfs = sorted(set(re.findall(r'https?://content\.whiteoakamc\.com/[^"\\\s\'<>`]+?\.[Pp][Dd][Ff]', t)))
                fact = [p for p in pdfs if "factsheet" in p.lower()]
                print(f"{u} -> {r.status_code} fact_pdfs:{len(fact)}")
                for p in fact[:30]:
                    print("  ", p)
                # also search for the title literal
                hit = re.search(r"Factsheet as at[^\"]{0,40}", t)
                if hit:
                    print("  TITLE-HIT:", hit.group(0)[:200])
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    main()
