"""
Alt Text Bulk Updater — Lee Renee Jewellery (Shopify)

Fetches all products with images missing alt text and applies generated alt text
using the productImageUpdate mutation. Processes all ~3,385 missing images.

Alt text formula:
    [Product Title] | Lee Renee Jewellery

Usage:
    python3 alt_text_update.py                  # full run (with prompt)
    python3 alt_text_update.py --dry-run        # preview only, no changes
    python3 alt_text_update.py --client "Lee Renee Jewellery"

This script only writes to image altText — it never touches:
  - product.title
  - product.body_html
  - product.seo.title / product.seo.description
"""

import argparse
import os
import time

import requests

from config import load_env

load_env()

API_VER = "2024-10"

# ── GraphQL ──────────────────────────────────────────────────────────────────

_GQL_PRODUCTS = """
query ($cursor: String) {
  products(first: 250, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        status
        images(first: 30) {
          edges { node { id altText src } }
        }
      }
    }
  }
}
"""

# Image alt text is updated via the Shopify REST API (PUT /products/{pid}/images/{iid}.json)
# because ProductInput does not expose images[] in the GraphQL Admin API 2024-10.
# This only writes to image.alt — it never touches product title, body, or seo fields.


def _shopify_gql(shop: str, token: str, query: str, variables: dict = None) -> dict:
    url  = f"https://{shop}/admin/api/{API_VER}/graphql.json"
    hdrs = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    resp = requests.post(url, json={"query": query, "variables": variables or {}}, headers=hdrs)
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        raise RuntimeError(f"Shopify GQL error: {body['errors']}")
    return body


def _update_image_alt(shop: str, token: str,
                      product_gid: str, image_gid: str, alt: str) -> None:
    """Update a single product image's alt text via the REST API."""
    # Extract numeric IDs from GIDs: gid://shopify/Product/123 → 123
    pid = product_gid.split("/")[-1]
    iid = image_gid.split("/")[-1]
    url = f"https://{shop}/admin/api/{API_VER}/products/{pid}/images/{iid}.json"
    hdrs = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    resp = requests.put(url, json={"image": {"alt": alt}}, headers=hdrs)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"REST API error: {data['errors']}")


def fetch_products(shop: str, token: str) -> list:
    """Fetch all active products with their images."""
    products = []
    cursor   = None
    page     = 0
    while True:
        page += 1
        data = _shopify_gql(shop, token, _GQL_PRODUCTS, {"cursor": cursor})
        edges     = data["data"]["products"]["edges"]
        page_info = data["data"]["products"]["pageInfo"]
        for edge in edges:
            p = edge["node"]
            if p.get("status") != "ACTIVE":
                continue
            images = [e["node"] for e in (p.get("images", {}).get("edges") or [])]
            missing = [img for img in images if not img.get("altText")]
            if missing:
                products.append({
                    "gid":    p["id"],
                    "title":  p.get("title", ""),
                    "images": missing,
                })
        print(f"  Page {page}: {len(edges)} products fetched, "
              f"{sum(len(p['images']) for p in products)} missing alt so far")
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
    return products


def generate_alt_text(product_title: str, image_index: int, total_images: int) -> str:
    """
    Generate alt text for a product image.

    Formula: [Product Title] | Lee Renee Jewellery
    For products with multiple images, append the variant/angle index.
    Keep under 125 characters (Google truncates beyond this).
    """
    brand = "Lee Renee Jewellery"
    base  = f"{product_title} | {brand}"
    if len(base) > 125:
        # Truncate product title to fit
        max_title_len = 125 - len(f" | {brand}")
        base = f"{product_title[:max_title_len].rstrip()} | {brand}"
    if total_images > 1:
        suffix = f" – view {image_index + 1}"
        if len(base) + len(suffix) <= 125:
            base = base + suffix
    return base


def apply_alt_text(shop: str, token: str, products: list, dry_run: bool = False) -> dict:
    """Apply alt text to all missing images. One mutation call per product."""
    total      = sum(len(p["images"]) for p in products)
    applied    = 0
    errors     = 0

    print(f"\n  {'[DRY RUN] ' if dry_run else ''}Applying alt text to {total} images "
          f"across {len(products)} products (1 call per product)...\n")

    for product in products:
        pid   = product["gid"]
        title = product["title"]
        imgs  = product["images"]
        n     = len(imgs)

        # Build image inputs for all missing-alt images on this product
        image_inputs = []
        for idx, img in enumerate(imgs):
            alt = generate_alt_text(title, idx, n)
            if dry_run:
                print(f"  [DRY RUN] {title[:50]!r} image {idx+1}/{n} → {alt!r}")
            image_inputs.append({"id": img["id"], "altText": alt})

        if dry_run:
            print(f"  ✅ {title[:60]} ({n} image{'s' if n > 1 else ''})")
            applied += n
            continue

        product_errors = 0
        for idx, img in enumerate(imgs):
            alt = image_inputs[idx]["altText"]
            try:
                _update_image_alt(shop, token, pid, img["id"], alt)
                applied += 1
                # Shopify standard tier ~2 req/s; 0.6s gives comfortable headroom
                time.sleep(0.6)
            except Exception as exc:
                print(f"  ⚠️  {title[:40]} img {idx+1}: {exc}")
                errors += 1
                product_errors += 1
                time.sleep(1)
        status = "⚠️ " if product_errors else "✅"
        print(f"  {status} {title[:60]} ({n} image{'s' if n > 1 else ''})")

    return {"total": total, "applied": applied, "errors": errors}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client",  default="Lee Renee Jewellery")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes",     action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    shop  = os.environ.get("SHOPIFY_SHOP", "leereneejewellery.myshopify.com")
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
    if not token:
        raise SystemExit("SHOPIFY_ACCESS_TOKEN not set in .env")

    print(f"\n{'='*65}")
    print(f"  Alt Text Bulk Update — {args.client}")
    print(f"{'='*65}\n")

    print("  Fetching products with missing alt text...")
    products = fetch_products(shop, token)
    total_images = sum(len(p["images"]) for p in products)
    print(f"\n  Found {len(products)} products with {total_images} images missing alt text.")

    if not products:
        print("  Nothing to update — all images already have alt text!")
        return

    if args.dry_run:
        print("\n  [DRY RUN] Previewing changes only (no writes to Shopify).\n")
    elif not args.yes:
        print(f"\n  Ready to write alt text to {total_images} images.")
        confirm = input("  Proceed? [y/N] ").strip().lower()
        if confirm != "y":
            print("  Aborted.")
            return

    stats = apply_alt_text(shop, token, products, dry_run=args.dry_run)

    print(f"\n{'='*65}")
    print(f"  Alt Text Update Complete")
    print(f"{'='*65}")
    print(f"  Applied : {stats['applied']}")
    print(f"  Errors  : {stats['errors']}")
    if not args.dry_run:
        print(f"\n  Shopify updated. Images will appear in Google Image Search")
        print(f"  once Googlebot re-crawls the product pages.")


if __name__ == "__main__":
    main()
