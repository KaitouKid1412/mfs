"""Probe HSBC India MF site for factsheet PDF URLs."""
from __future__ import annotations
import re
import sys

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


def probe(url: str) -> tuple[int, str, str]:
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=45)
    try:
        r = client.get(url)
        return r.status_code, r.headers.get("content-type", ""), r.text
    finally:
        client.close()


def find_pdfs(html: str) -> list[str]:
    pat = re.compile(r"""https?://[^\s"'<>\\)]+?\.pdf""", re.IGNORECASE)
    return list(dict.fromkeys(pat.findall(html)))


def find_relative_pdfs(html: str) -> list[str]:
    pat = re.compile(r"""["']((?:/[^"'<>\s]*?)\.pdf)["']""", re.IGNORECASE)
    return list(dict.fromkeys(pat.findall(html)))


def main(args: list[str]) -> int:
    urls = args or [
        "https://www.assetmanagement.hsbc.co.in/",
        "https://www.assetmanagement.hsbc.co.in/en/mutual-funds/individual-investor",
        "https://www.assetmanagement.hsbc.co.in/en/mutual-funds/individual-investor/forms-and-downloads",
        "https://www.assetmanagement.hsbc.co.in/en-in/mutual-funds",
    ]
    for u in urls:
        try:
            sc, ct, body = probe(u)
        except Exception as e:
            print(f"{u}\n  ERR {e}")
            continue
        print(f"\n=== {u} -> {sc} {ct} len={len(body)} ===")
        pdfs = find_pdfs(body)
        rels = find_relative_pdfs(body)
        for p in pdfs[:50]:
            print("  PDF:", p)
        for p in rels[:50]:
            print("  REL:", p)
        # Look for "factsheet" or "fact sheet" anchors
        for m in re.finditer(r"(?i)factsheet|fact[ \-]?sheet|fact[- ]?book", body):
            s = max(0, m.start() - 60)
            e = min(len(body), m.end() + 80)
            print("  CTX:", body[s:e].replace("\n", " ")[:200])
        # Useful clues
        for m in re.finditer(r"""(?i)href=["']([^"']+)["']""", body):
            link = m.group(1)
            if "factsheet" in link.lower() or "fact-sheet" in link.lower() or "fact_sheet" in link.lower():
                print("  ANCHOR:", link)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
