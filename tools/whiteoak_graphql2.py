"""Probe WhiteOak GraphQL for the correct documents query field name."""
from __future__ import annotations
import json
import sys

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HEADERS = {"User-Agent": UA, "Accept": "*/*", "Content-Type": "application/json"}


def query(body: dict) -> dict:
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30)
    try:
        r = client.post("https://cms.whiteoakamc.com/graphql", json=body)
        return {"status": r.status_code, "text": r.text}
    finally:
        client.close()


def main() -> int:
    # Try common Strapi v4 named queries — note we already saw `DocumentEntity` typename
    # in the SSR data (used for downloads / forms-and-downloads).
    # Strapi v4 default plural form: documents( filters, pagination, sort, publicationState )
    candidates = [
        "documents",
        "documentCategories",
        "documentSubcategories",
        "knowledgeCenterResources",
        "blogResources",
        "mediaCenters",
    ]
    for c in candidates:
        body = {
            "query": f"""
            query {{
              {c}(pagination: {{ limit: 2 }}) {{
                data {{
                  id
                  attributes {{ title }}
                }}
              }}
            }}
            """
        }
        res = query(body)
        print(f"\n--- {c} ---")
        print(res["status"], res["text"][:500])

    # Now try documents( filters: {id: {eq: 4271}} )
    body = {
        "query": """
        query {
          documents(filters: { id: { eq: 4271 } }) {
            data {
              id
              attributes {
                title
                desc
                document_subcategories { data { attributes { title value } } }
                document_categories { data { attributes { title value } } }
              }
            }
          }
        }
        """
    }
    res = query(body)
    print("\n--- documents(filters: id eq 4271) basic ---")
    print(res["status"], res["text"][:1500])

    # Probe for the attachment field name
    for fld in [
        "download_media_file",
        "document_file",
        "document_attachment",
        "attachment",
        "file",
        "upload",
        "downloadFile",
    ]:
        body = {
            "query": f"""
            query {{
              documents(filters: {{ id: {{ eq: 4271 }} }}) {{
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
        }
        res = query(body)
        if "GRAPHQL_VALIDATION_FAILED" not in res["text"]:
            print(f"\n--- documents w/ {fld} ---")
            print(res["status"], res["text"][:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
