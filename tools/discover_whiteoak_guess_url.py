"""Guess possible WhiteOak factsheet PDF URL patterns and HEAD them."""
from __future__ import annotations
import itertools

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {"User-Agent": UA}


def main() -> int:
    bases = [
        "https://content.whiteoakamc.com/",
        "https://wocamc-prd-prelogin-s3-00.s3.ap-south-1.amazonaws.com/",
    ]
    patterns = [
        "Factsheet_April_2026.pdf",
        "Factsheet_Apr_2026.pdf",
        "Factsheet_April_30_2026.pdf",
        "Factsheet_as_on_April_30_2026.pdf",
        "Factsheet_as_at_April_30_2026.pdf",
        "WhiteOak_Capital_Factsheet_April_2026.pdf",
        "WhiteOak_Capital_Mutual_Fund_Factsheet_April_2026.pdf",
        "WhiteOak_Capital_MF_Factsheet_April_2026.pdf",
        "Monthly_Factsheet_April_2026.pdf",
        "WOC_Factsheet_April_2026.pdf",
        "WhiteOak_Capital_Factsheet_Apr_2026.pdf",
        "Factsheet-April-2026.pdf",
        "Factsheet-Apr-2026.pdf",
        "April_2026_Factsheet.pdf",
        "factsheet_apr_2026.pdf",
        "WhiteOak_Capital_MF_Monthly_Factsheet_April_2026.pdf",
        "WhiteOak_Factsheet_April_2026.pdf",
        "Monthly_Factsheet_Apr_2026.pdf",
    ]
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30)
    try:
        for base, p in itertools.product(bases, patterns):
            url = base + p
            try:
                r = client.head(url, timeout=15)
                if r.status_code == 200:
                    print(f"FOUND: {url}")
                # 403 is "exists but ACL denied for listing"; the actual file may also return 200 on GET
            except Exception as e:
                print(f"ERR {url} {e}")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    main()
