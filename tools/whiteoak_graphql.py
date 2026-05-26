"""Query WhiteOak's Strapi GraphQL endpoint at cms.whiteoakamc.com."""
from __future__ import annotations
import json
import re
import sys

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Content-Type": "application/json",
}


def main() -> int:
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60)
    try:
        # 1) introspect available query fields
        introspect = {
            "query": """
            query {
              __schema {
                queryType {
                  fields { name }
                }
              }
            }
            """
        }
        r = client.post("https://cms.whiteoakamc.com/graphql", json=introspect)
        print("introspect status:", r.status_code)
        print(r.text[:3000])

        # 2) try documents query
        # In Strapi v4 GraphQL, the standard query is documents(filters:..., pagination:..., sort:...)
        q = {
            "query": """
            query Docs($id: ID!) {
              document(id: $id) {
                data {
                  id
                  attributes {
                    title
                    download_media_file {
                      data { attributes { url name } }
                    }
                  }
                }
              }
            }
            """,
            "variables": {"id": "4271"},
        }
        r = client.post("https://cms.whiteoakamc.com/graphql", json=q)
        print("\ndocument(4271) status:", r.status_code)
        print(r.text[:3000])
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
