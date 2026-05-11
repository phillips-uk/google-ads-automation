"""
Append standard footer text to all active products with thin descriptions (<80 words).

ONE-TIME USE — explicit client approval given 2026-05-10.
Only writes to product.body_html (descriptionHtml). Never touches SEO fields.

Idempotent: skips any product whose description already contains 'Hatton Garden'.
"""

import os
import re
import time

import requests

from config import load_env

load_env()

API_VER = "2024-10"
SHOP    = "leereneejewellery.myshopify.com"
TOKEN   = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
MIN_WORDS = 80

APPEND_HTML = """<p>Handmade in our Hatton Garden studio in London's famous jewellery quarter, from 100% recycled sterling silver and gold.</p>
<p>Our philosophy is to love more and consume less. So, we carefully craft every piece of Lee Renée jewellery by hand, to ensure it will remain a forever-favourite in your jewellery box for years to come.</p>
<p>Read about our ethics and see how your purchases help support our chosen charities.</p>"""

_GQL_PRODUCTS = """
query ($cursor: String) {
  products(first: 250, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        status
        descriptionHtml
      }
    }
  }
}
"""

_GQL_UPDATE = """
mutation productUpdate($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id }
    userErrors { field message }
  }
}
"""


def gql(query, variables=None):
    url  = f"https://{SHOP}/admin/api/{API_VER}/graphql.json"
    hdrs = {"X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"}
    r = requests.post(url, json={"query": query, "variables": variables or {}}, headers=hdrs)
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        raise RuntimeError(f"GQL error: {body['errors']}")
    return body


def strip_html(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def word_count(text):
    return len(text.split()) if text else 0


def fetch_thin():
    products, cursor = [], None
    while True:
        data     = gql(_GQL_PRODUCTS, {"cursor": cursor})
        edges    = data["data"]["products"]["edges"]
        page_info = data["data"]["products"]["pageInfo"]
        for edge in edges:
            p = edge["node"]
            if p.get("status") != "ACTIVE":
                continue
            html  = p.get("descriptionHtml") or ""
            text  = strip_html(html)
            words = word_count(text)
            if words < MIN_WORDS and "Hatton Garden" not in html:
                products.append({"gid": p["id"], "title": p["title"], "html": html, "words": words})
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
    return products


def main():
    if not TOKEN:
        raise SystemExit("SHOPIFY_ACCESS_TOKEN not set")

    print(f"\n{'='*65}")
    print(f"  Append Description Footer — Lee Renée Jewellery")
    print(f"{'='*65}\n")
    print(f"  Fetching products with descriptions under {MIN_WORDS} words...")

    products = fetch_thin()
    print(f"  Found {len(products)} products to update\n")

    applied = errors = 0
    for p in products:
        new_html = (p["html"].rstrip() + "\n" + APPEND_HTML) if p["html"].strip() else APPEND_HTML
        result = gql(_GQL_UPDATE, {"input": {"id": p["gid"], "descriptionHtml": new_html}})
        user_errors = result.get("data", {}).get("productUpdate", {}).get("userErrors", [])
        if user_errors:
            print(f"  ❌ {p['title']}: {user_errors}")
            errors += 1
        else:
            print(f"  ✅ {p['title']} ({p['words']} words)")
            applied += 1
        time.sleep(0.4)

    print(f"\n  Applied: {applied}  |  Errors: {errors}")
    print(f"\n  ⚠️  product.body_html updated under one-time client approval (2026-05-10).")
    print(f"     Do not auto-apply description changes without explicit approval in future.\n")


if __name__ == "__main__":
    main()
