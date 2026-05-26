"""Pull EVERY JS chunk referenced from layout/page and search for API hostname."""
from __future__ import annotations
import re

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {"User-Agent": UA, "Accept": "*/*"}


def main() -> int:
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60)
    try:
        # Pages we want chunks from
        urls = [
            "https://mf.whiteoakamc.com/resources?resource-type=downloads&category=Factsheet&subcategory=Monthly%20Factsheet&page=1",
            "https://mf.whiteoakamc.com/forms-and-downloads",
            "https://mf.whiteoakamc.com/scheme-list/all-funds",
            "https://mf.whiteoakamc.com/",
            "https://mf.whiteoakamc.com/about-us",
        ]
        chunk_urls = set()
        for u in urls:
            r = client.get(u, timeout=30)
            for c in re.findall(r'/_next/static/chunks/[\w/.-]+\.js', r.text):
                chunk_urls.add(c)
            # also pull RSC payload for additional chunks
            r2 = client.get(u, headers={**HEADERS, "rsc": "1"}, timeout=30)
            for c in re.findall(r'/_next/static/chunks/[\w/.-]+\.js', r2.text):
                chunk_urls.add(c)
        print("chunks discovered:", len(chunk_urls))

        all_text = []
        for c in sorted(chunk_urls):
            try:
                rr = client.get(f"https://mf.whiteoakamc.com{c}", timeout=15)
                if rr.status_code == 200:
                    all_text.append((c, rr.text))
            except Exception:
                pass

        combined = "\n".join(t for _, t in all_text)
        print("combined len:", len(combined))

        # Look for content host or API host references
        for pat in [
            r"https?://[a-zA-Z0-9.-]+\.whiteoakamc\.com/?[\w/.-]*",
            r"NEXT_PUBLIC_[A-Z_]+",
            r"download_media_file",
            r"document_attachment",
            r"document_file",
            r"DOWNLOAD_URL",
            r"baseURL",
            r"BASE_URL",
            r"baseUrl",
            r"https?://[\w.-]+/api/[\w./-]+",
            r"\"NEXT_PUBLIC_API[^\"]+\":[^,}]+",
            r"axios\.create\(",
            r"\b[a-zA-Z_]+_API_URL\b",
        ]:
            hits = sorted(set(re.findall(pat, combined)))
            if hits:
                print(f"\n=== {pat!r} -> {len(hits)} ===")
                for h in hits[:30]:
                    print("  ", h[:300])

        # Where 'factsheet' or 'Factsheet' appears in JS
        for name, t in all_text:
            for m in re.finditer(r"(?i)factsheet", t):
                s = max(0, m.start() - 80)
                e = min(len(t), m.end() + 200)
                ctx = t[s:e]
                if "content.whiteoakamc.com" in ctx or "url" in ctx.lower() or "http" in ctx.lower():
                    print(f"\nchunk {name} factsheet ctx:\n   {ctx[:400]}")
                    break
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    main()
