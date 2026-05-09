"""
Thin Descriptions Report — Lee Renee Jewellery

Generates a client-facing Markdown report listing all products with descriptions
under 80 words. This report is for CLIENT REVIEW — no auto-apply. The client
reviews with their copywriter and approves changes before anything goes live.

Output: Obsidian note at:
  03_Google Ads Clients/01_Lee Renee Jewellery/SEO Audits/
  thin_descriptions_YYYY-MM-DD.md

Usage:
    python3 thin_desc_report.py
    python3 thin_desc_report.py --min-words 80     # default threshold
    python3 thin_desc_report.py --max-products 50  # limit rows in report
"""

import argparse
import os
import re
import textwrap
from datetime import date

import requests

from config import load_env
from clients import SEO_AUDIT_CLIENTS

load_env()

API_VER        = "2024-10"
OBSIDIAN_BASE  = os.environ.get(
    "OBSIDIAN_BASE",
    os.path.expanduser(
        "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/My Brain/03_Google Ads Clients"
    ),
)

_GQL_PRODUCTS = """
query ($cursor: String) {
  products(first: 250, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        handle
        status
        descriptionHtml
        seo { title description }
      }
    }
  }
}
"""


def _shopify_gql(shop: str, token: str, query: str, variables: dict = None) -> dict:
    url  = f"https://{shop}/admin/api/{API_VER}/graphql.json"
    hdrs = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    resp = requests.post(url, json={"query": query, "variables": variables or {}}, headers=hdrs)
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        raise RuntimeError(f"Shopify GQL error: {body['errors']}")
    return body


def strip_html(html: str) -> str:
    """Remove HTML tags and normalise whitespace."""
    if not html:
        return ""
    clean = re.sub(r"<[^>]+>", " ", html)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def word_count(text: str) -> int:
    return len(text.split()) if text else 0


def fetch_thin_products(shop: str, token: str, min_words: int) -> list:
    """Fetch all active products where the description is under min_words."""
    products = []
    cursor   = None
    while True:
        data     = _shopify_gql(shop, token, _GQL_PRODUCTS, {"cursor": cursor})
        edges    = data["data"]["products"]["edges"]
        page_info = data["data"]["products"]["pageInfo"]
        for edge in edges:
            p = edge["node"]
            if p.get("status") != "ACTIVE":
                continue
            desc_text = strip_html(p.get("descriptionHtml") or "")
            words     = word_count(desc_text)
            if words < min_words:
                products.append({
                    "id":          p["id"].split("/")[-1],
                    "gid":         p["id"],
                    "title":       p.get("title", ""),
                    "handle":      p.get("handle", ""),
                    "words":       words,
                    "description": desc_text[:500],  # first 500 chars for context
                    "seo_title":   (p.get("seo") or {}).get("title") or "",
                })
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

    # Sort: 0 words first (no description), then ascending word count
    products.sort(key=lambda p: p["words"])
    return products


def categorise(product: dict) -> str:
    """Guess the product category from its title for recommendations context."""
    t = product["title"].lower()
    if "ring" in t:       return "Ring"
    if "necklace" in t:   return "Necklace"
    if "earring" in t:    return "Earring"
    if "bracelet" in t:   return "Bracelet"
    if "cufflink" in t:   return "Cufflink"
    if "brooch" in t or "lapel" in t or "tie pin" in t: return "Accessory"
    if "bangle" in t:     return "Bracelet"
    return "Jewellery"


def write_report(products: list, domain: str, folder: str,
                 min_words: int, client_name: str) -> str:
    today = date.today().isoformat()
    no_desc    = [p for p in products if p["words"] == 0]
    short_desc = [p for p in products if 0 < p["words"] < min_words]

    L = [
        f"# Product Copy Recommendations — {client_name}",
        f"*Generated: {today}*",
        "",
        "> **For client review.** This document lists products where the description is "
        f"under {min_words} words. Google classifies pages with very short descriptions as "
        "thin content, which can reduce rankings and click-through rates.",
        ">",
        "> Please work through these with your copywriter. For each product, either:",
        "> - **Write a new description** (100–150 words is ideal) and confirm the update",
        "> - **Mark as intentional** (e.g. limited-edition / archived) and I'll flag it as excluded",
        "",
        "---",
        "",
        "## Summary",
        "",
        f"| Issue | Count |",
        f"| --- | --- |",
        f"| No description at all | {len(no_desc)} |",
        f"| Description under {min_words} words | {len(short_desc)} |",
        f"| **Total needing attention** | **{len(products)}** |",
        "",
        "---",
        "",
    ]

    if no_desc:
        L += [
            "## Products with No Description",
            "",
            "> These have zero words in their description. They are most at risk of being ranked poorly.",
            "",
            "| Product | Category | URL | Priority |",
            "| --- | --- | --- | --- |",
        ]
        for p in no_desc:
            cat  = categorise(p)
            url  = f"https://{domain}/products/{p['handle']}"
            L.append(f"| {p['title']} | {cat} | {url} | 🔴 High |")
        L += ["", "---", ""]

    if short_desc:
        L += [
            f"## Products with Short Descriptions (1–{min_words - 1} words)",
            "",
            "| Product | Words | Category | Current Description (preview) | URL |",
            "| --- | --- | --- | --- | --- |",
        ]
        for p in short_desc:
            cat     = categorise(p)
            url     = f"https://{domain}/products/{p['handle']}"
            preview = (p["description"][:80] + "…").replace("|", "\\|") if p["description"] else "_(none)_"
            L.append(f"| {p['title'].replace('|', chr(92)+'|')} | {p['words']} | {cat} | {preview} | {url} |")
        L += ["", "---", ""]

    L += [
        "## What a Good Description Looks Like",
        "",
        "A strong Lee Renée product description does three things:",
        "",
        "1. **Describes the piece specifically** — material, gemstone, finish, dimensions if known",
        "2. **Signals occasion/intent** — who buys this and why (gift, everyday wear, occasion piece)",
        "3. **Reassures on quality/provenance** — hallmarked, ethically sourced, UK-made where relevant",
        "",
        "**Target length:** 100–150 words. Longer is fine; shorter risks thin-content classification.",
        "",
        "**Example template:**",
        "```",
        "[Product name] crafted in [material] with [key detail: gemstone, finish, closure type].",
        "The [design element] gives it [character/distinctive quality].",
        "Suitable for [occasion 1] and [occasion 2] — works as [everyday/statement/gift].",
        "[Optional: sizing, hallmarking, care note].",
        "Presented in a Lee Renée gift box. Free UK delivery.",
        "```",
        "",
        "---",
        "",
        f"*{len(products)} products flagged. Send corrections back to Lewis with the product title "
        "and the new/updated description text.*",
    ]

    report_dir = os.path.join(OBSIDIAN_BASE, folder, "SEO Audits")
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"thin_descriptions_{today}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client",      default="Lee Renee Jewellery")
    parser.add_argument("--min-words",   type=int, default=80)
    args = parser.parse_args()

    cfg = SEO_AUDIT_CLIENTS.get(args.client)
    if not cfg:
        raise SystemExit(f"Client '{args.client}' not found in SEO_AUDIT_CLIENTS")

    shop   = cfg["shopify_shop"]
    domain = cfg["shopify_domain"]
    folder = cfg["folder"]
    token  = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
    if not token:
        raise SystemExit("SHOPIFY_ACCESS_TOKEN not set in .env")

    print(f"\n{'='*65}")
    print(f"  Thin Descriptions Report — {args.client}")
    print(f"{'='*65}\n")
    print(f"  Fetching products with descriptions under {args.min_words} words...")

    products = fetch_thin_products(shop, token, args.min_words)
    no_desc  = sum(1 for p in products if p["words"] == 0)
    thin     = sum(1 for p in products if p["words"] > 0)
    print(f"  Found {len(products)} products: {no_desc} with no description, {thin} under {args.min_words} words")

    path = write_report(products, domain, folder, args.min_words, args.client)
    print(f"\n  ✅ Report written: {path}")
    print(f"\n  Next steps:")
    print(f"  1. Share this report with the client (or paste into a Google Doc)")
    print(f"  2. Ask them to write/update descriptions for flagged products")
    print(f"  3. Once approved, update Shopify manually or via bulk import")
    print(f"\n  ⚠️  Do NOT edit descriptions directly in Shopify without client approval.")
    print(f"     Per content ownership rules, product.body_html requires explicit sign-off.")


if __name__ == "__main__":
    main()
