"""Find the Documents entity via the DocumentCategory schema."""
from __future__ import annotations
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
    # 1) Explore DocumentCategory shape: includes documents?
    s, t = query({
        "query": """
        query {
          documentCategories(pagination: { limit: 30 }) {
            data {
              id
              attributes {
                title
                value
              }
            }
          }
        }
        """
    })
    print(s, t[:2000])

    # 2) Try to discover the field name on DocumentCategory that holds the docs
    for fld in [
        "form_documents",
        "documents",
        "form_and_downloads",
        "downloads",
        "factsheets",
        "factsheet_documents",
    ]:
        s, t = query({
            "query": f"""
            query {{
              documentCategories(filters: {{ title: {{ eq: "Factsheet" }} }}, pagination: {{ limit: 5 }}) {{
                data {{
                  id
                  attributes {{
                    title
                    {fld} {{ data {{ id attributes {{ title }} }} }}
                  }}
                }}
              }}
            }}
            """
        })
        if "GRAPHQL_VALIDATION_FAILED" not in t:
            print(f"\n--- with field {fld!r} ---")
            print(s, t[:2000])
    # 3) Try DocumentSubcategory linkages
    for fld in [
        "form_documents",
        "documents",
        "form_and_downloads",
        "downloads",
        "factsheets",
    ]:
        s, t = query({
            "query": f"""
            query {{
              documentSubcategories(filters: {{ title: {{ eq: "Monthly Factsheet" }} }}, pagination: {{ limit: 5 }}) {{
                data {{
                  id
                  attributes {{
                    title
                    {fld} {{ data {{ id attributes {{ title }} }} }}
                  }}
                }}
              }}
            }}
            """
        })
        if "GRAPHQL_VALIDATION_FAILED" not in t:
            print(f"\n--- subcat with field {fld!r} ---")
            print(s, t[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
