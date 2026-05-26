"""Walk JS chunks and find the API endpoint."""
from __future__ import annotations
import re

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {"User-Agent": UA, "Accept": "*/*"}


def main() -> int:
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60)
    try:
        # First find the JS chunk URLs referenced from /resources page
        r = client.get(
            "https://mf.whiteoakamc.com/resources?resource-type=downloads&category=factsheet&page=1",
            timeout=30,
        )
        html = r.text
        # collect <script src=...> + preload links
        scripts = sorted(set(re.findall(r'/_next/static/chunks/[\w/.-]+\.js', html)))
        print("chunks referenced:", len(scripts))
        # Also pull build manifest if any
        for path in [
            "/_next/static/buildManifest.js",
            "/_next/static/chunks/_buildManifest.js",
        ]:
            try:
                rr = client.get(f"https://mf.whiteoakamc.com{path}")
                if rr.status_code == 200:
                    print(f"\n{path} found, scanning chunks...")
            except Exception:
                pass
        # combine + scan
        big = []
        for s in scripts:
            try:
                rr = client.get(f"https://mf.whiteoakamc.com{s}", timeout=20)
                if rr.status_code == 200:
                    big.append((s, rr.text))
            except Exception:
                pass
        # Look for hostnames + API patterns
        combined = "\n".join(t for _, t in big)
        print("combined js len:", len(combined))
        # Now search for content.whiteoakamc.com URLs and template literals
        for pat in [
            r"content\.whiteoakamc\.com",
            r"NEXT_PUBLIC_[A-Z_]+",
            r"process\.env\.[A-Z_]+",
            r"\"baseURL\":\s*\"[^\"]+\"",
            r"href:[\w\s,]+url",
            r"download_media_file",
            r"document_attachment",
            r"document_file",
            r"DOWNLOAD_URL",
            r"https?://[a-z.-]+\.com/api/[^\"\\s'`]+",
            r"\"/api/[^\"\\s]+\"",
            r"`/api/[^`]+`",
        ]:
            hits = sorted(set(re.findall(pat, combined)))
            if hits:
                print(f"\n=== {pat!r}: {len(hits)} hits ===")
                for h in hits[:30]:
                    print("  ", h[:200])
        # Also search per-chunk where 'factsheet' or 'Factsheet' appears
        for name, t in big:
            if "factsheet" in t.lower() or "Factsheet" in t:
                print(f"\n--- chunk {name} mentions factsheet ---")
                for m in re.finditer(r"[Ff]actsheet", t):
                    s = max(0, m.start() - 80)
                    e = min(len(t), m.end() + 200)
                    print("   ", t[s:e].replace("\n", " ")[:300])
                    print()
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    main()
