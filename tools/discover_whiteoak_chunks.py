"""Search all JS chunks for the file-URL template / hostname used in fetches."""
from __future__ import annotations
import re
import sys

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
}


def main() -> int:
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60)
    try:
        r = client.get("https://mf.whiteoakamc.com/forms-and-downloads", timeout=30)
        html = r.text
        chunks = sorted(set(re.findall(r'/_next/static/chunks/[\w/.-]+\.js', html)))
        print("chunks:", len(chunks))
        # collect across all chunks
        all_text = []
        for c in chunks:
            try:
                rr = client.get(f"https://mf.whiteoakamc.com{c}", timeout=20)
                if rr.status_code == 200:
                    all_text.append(rr.text)
            except Exception:
                pass
        combined = "\n".join(all_text)
        print("total JS bytes:", len(combined))

        # Look for content.whiteoakamc.com patterns
        for pat in [
            r"content\.whiteoakamc\.com[^\"'`\s]+",
            r"strapi[A-Za-z]+\(",
            r"document_attachment",
            r"document_file",
            r"document_link",
            r"link_image",
            r"`https?://[^`]+\$\{[^`]+\.pdf",
            r"fetch\(\"[^\"]+\"",
            r"axios\.get\(\"[^\"]+\"",
            r"\$\{[^}]+\}_[\w]+\.pdf",
            r"DOWNLOAD[A-Z_]*\s*=\s*\"[^\"]+\"",
            r"document.attribute",
            r"\"NEXT_PUBLIC_[A-Z_]+\":\s*\"[^\"]+\"",
            r"NEXT_PUBLIC_[A-Z_]+",
            r"contentBaseUrl|contentBase|fileBase",
        ]:
            hits = sorted(set(re.findall(pat, combined)))
            if hits:
                print(f"\n=== pattern={pat!r} hits={len(hits)} ===")
                for h in hits[:30]:
                    print("  ", h[:200])
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
