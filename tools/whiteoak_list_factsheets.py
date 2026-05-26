"""List all factsheet downloads from WhiteOak GraphQL with browser UA."""
import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Accept": "*/*",
    "Content-Type": "application/json",
}

body = {
    "query": (
        "query { downloads(filters: { title: { contains: \"Factsheet as at\" } }, "
        "pagination: { limit: 50 }) { data { id attributes { title publishedAt "
        "download_media_file { data { attributes { url } } } } } } }"
    )
}

r = httpx.post(
    "https://cms.whiteoakamc.com/graphql",
    headers=HEADERS,
    json=body,
    timeout=60,
)
print("status:", r.status_code)
data = r.json()
for d in data["data"]["downloads"]["data"]:
    a = d["attributes"]
    media = a["download_media_file"]["data"]
    url = media["attributes"]["url"] if media else None
    print(f"  id={d['id']} {a['title']!r}  pub={a.get('publishedAt')}  url={url}")
