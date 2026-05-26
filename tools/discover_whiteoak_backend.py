"""Look for the GraphQL backend by checking common patterns and known Strapi hosts."""
import re

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {"User-Agent": UA, "Accept": "*/*"}


def main() -> int:
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30)
    try:
        # Try GraphQL endpoints on a known WhiteOak-owned subdomain
        hosts = [
            "https://content.whiteoakamc.com",
            "https://api-mf.whiteoakamc.com",
            "https://mfapi.whiteoakamc.com",
            "https://strapi-mf.whiteoakamc.com",
            "https://cms-mf.whiteoakamc.com",
            "https://woccms.whiteoakamc.com",
            "https://woccms-mf.whiteoakamc.com",
            "https://cms.whiteoakamc.com",
            "https://backend.whiteoakamc.com",
        ]
        for h in hosts:
            for p in ["/graphql", "/api/graphql", "/api/documents", "/api"]:
                try:
                    r = client.get(h + p, timeout=10)
                    if r.status_code not in (404, 0):
                        print(f"{h + p} -> {r.status_code} {r.headers.get('content-type','')}")
                        if r.status_code in (200, 401, 403):
                            print("    ", r.text[:200])
                except Exception as e:
                    if "ConnectError" not in type(e).__name__:
                        print(f"{h + p} ERR {type(e).__name__}: {e}")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    main()
