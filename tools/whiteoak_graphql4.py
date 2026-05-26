"""Probe the 'download' (singular?) entity for its file URL field name."""
from __future__ import annotations
import json
import sys

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {"User-Agent": UA, "Accept": "*/*", "Content-Type": "application/json"}


def query(body):
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30)
    try:
        r = client.post("https://cms.whiteoakamc.com/graphql", json=body)
        return r.status_code, r.text
    finally:
        client.close()


def main() -> int:
    # Top-level query for 'downloads'
    for q in [
        "downloads",
        "download",
        "documents",
        "documentDownloads",
    ]:
        s, t = query({
            "query": f"""
            query {{
              {q}(filters: {{ title: {{ contains: "Factsheet as at April 30" }} }}, pagination: {{ limit: 3 }}) {{
                data {{
                  id
                  attributes {{
                    title
                    publishedAt
                  }}
                }}
              }}
            }}
            """
        })
        if "GRAPHQL_VALIDATION_FAILED" not in t:
            print(f"\n--- top {q} ---")
            print(s, t[:2000])
        else:
            print(f"  {q}: invalid")

    # Try field discovery on the entity returned in the previous step
    for fld in [
        "document_file",
        "download_media_file",
        "attachment",
        "file",
        "media_file",
        "document",
        "upload",
    ]:
        s, t = query({
            "query": f"""
            query {{
              downloads(filters: {{ title: {{ contains: "Factsheet as at April 30" }} }}, pagination: {{ limit: 3 }}) {{
                data {{
                  id
                  attributes {{
                    title
                    {fld} {{ data {{ attributes {{ url name }} }} }}
                  }}
                }}
              }}
            }}
            """
        })
        if "GRAPHQL_VALIDATION_FAILED" not in t:
            print(f"\n--- downloads w/ {fld} ---")
            print(s, t[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
