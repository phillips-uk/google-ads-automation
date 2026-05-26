"""
Shopify Feed Optimiser

Two-phase workflow:

  Phase 1 — Propose
    python3 shopify_feed_optimiser.py --propose
    Audits all products, generates AI title improvements and infers missing
    product_types, writes them as editable review tables to the Obsidian report.
    Review the report, change "approve"/"skip" or edit proposed values, then
    tell Claude: "apply the feed optimisation report"

  Phase 2 — Apply
    Called by Claude after the user reviews the report.
    Reads approved rows from the report and writes them to Shopify.

  Audit-only (default)
    python3 shopify_feed_optimiser.py
    Runs the audit and writes findings — no proposals generated.
"""

import os
import re
import sys
import json
import time
import argparse
import glob
from datetime import date
from html.parser import HTMLParser

import requests
import anthropic
from config import OBSIDIAN_BASE, load_env

load_env()

# ── Client config — loaded from clients.py (gitignored, never committed) ───────
try:
    from clients import SHOPIFY_SHOP as SHOP, SHOPIFY_ACCOUNT as ACCOUNT
except ImportError:
    print("[config] ERROR: clients.py not found.")
    print("         Copy clients.example.py → clients.py and set SHOPIFY_SHOP and SHOPIFY_ACCOUNT.")
    sys.exit(1)

API_VER    = "2026-04"
BASE_URL   = f"https://{SHOP}/admin/api/{API_VER}"
REPORT_DIR = os.path.join(OBSIDIAN_BASE, ACCOUNT, "Feed Optimisations")

METALS = {
    "silver", "gold", "rose gold", "platinum", "titanium",
    "copper", "bronze", "rhodium", "vermeil", "diamond",
}
THIN_DESC_CHARS = 150
WEAK_TITLE_SCORE = 70

# Keyword → product_type inference for missing-type products
_TYPE_KEYWORDS = [
    ("ring",      "Rings"),
    ("necklace",  "Necklaces"),
    ("pendant",   "Necklaces"),
    ("bracelet",  "Bracelets"),
    ("earring",   "Earrings"),
    ("stud",      "Earrings"),
    ("cufflink",  "Cufflinks"),
    ("brooch",    "Brooches"),
    ("bangle",    "Bangles"),
    ("anklet",    "Anklets"),
    ("tie pin",   "Tie Pins"),
    ("charm",     "Charms"),
]

# Products that should be excluded from the feed rather than typed
_EXCLUDE_FROM_FEED = {"gift card", "ring sizer", "ring sizer gauge"}

# ── Attribute inference ────────────────────────────────────────────────────────

# Material keyword patterns (most-specific first)
_MATERIAL_PATTERNS = [
    (r"gold vermeil",                     "Gold Vermeil"),
    (r"rose gold",                        "Rose Gold"),
    (r"18ct gold|18 carat gold|18k gold", "18ct Gold"),
    (r"9ct gold|9 carat gold|9k gold",    "9ct Gold"),
    (r"sterling silver",                  "Sterling Silver"),
    (r"\bgold\b",                         "Gold"),
    (r"\bsilver\b",                       "Sterling Silver"),
    (r"platinum",                         "Platinum"),
    (r"titanium",                         "Titanium"),
    (r"copper",                           "Copper"),
    (r"bronze",                           "Bronze"),
]

# Material → dominant colour for Shopping feed
_MATERIAL_COLOR = {
    "Gold Vermeil":    "Gold",
    "Rose Gold":       "Rose Gold",
    "18ct Gold":       "Gold",
    "9ct Gold":        "Gold",
    "Gold":            "Gold",
    "Sterling Silver": "Silver",
    "Platinum":        "Silver",
    "Titanium":        "Grey",
    "Copper":          "Copper",
    "Bronze":          "Bronze",
}

# Gemstone/stone keywords → override colour (checked first)
_GEMSTONE_COLORS = [
    ("ruby",       "Red"),
    ("garnet",     "Red"),
    ("carnelian",  "Red"),
    ("coral",      "Pink"),
    ("sapphire",   "Blue"),
    ("aquamarine", "Blue"),
    ("turquoise",  "Blue"),
    ("topaz",      "Blue"),
    ("emerald",    "Green"),
    ("peridot",    "Green"),
    ("jade",       "Green"),
    ("malachite",  "Green"),
    ("amethyst",   "Purple"),
    ("lavender",   "Purple"),
    ("citrine",    "Yellow"),
    ("amber",      "Amber"),
    ("pearl",      "White"),
    ("moonstone",  "White"),
    ("diamond",    "White"),
    ("onyx",       "Black"),
    ("jet",        "Black"),
    ("obsidian",   "Black"),
    ("opal",       "Multi"),
]

# product_type → Google Product Category (full taxonomy path)
_GPC_MAP = {
    "Rings":     "Apparel & Accessories > Jewelry > Rings",
    "Necklaces": "Apparel & Accessories > Jewelry > Necklaces",
    "Earrings":  "Apparel & Accessories > Jewelry > Earrings",
    "Bracelets": "Apparel & Accessories > Jewelry > Bracelets",
    "Bangles":   "Apparel & Accessories > Jewelry > Bracelets",
    "Brooches":  "Apparel & Accessories > Jewelry > Brooches & Lapel Pins",
    "Anklets":   "Apparel & Accessories > Jewelry > Anklets",
    "Charms":    "Apparel & Accessories > Jewelry > Charms & Pendants",
    "Cufflinks": "Apparel & Accessories > Jewelry > Cuff Links",
    "Tie Pins":  "Apparel & Accessories > Jewelry > Cuff Links",
}

_METAFIELD_NS = "custom"  # Shopify namespace for enrichment metafields


# ── Shopify API ────────────────────────────────────────────────────────────────

# ── IMPORTANT — title ownership rule ──────────────────────────────────────────
# This script proposes and applies changes ONLY to product.seo.title (the SEO
# meta title). It never touches product.title (the storefront display name),
# which requires client approval before any change.
# The SEO title feeds directly into Simprosys as the Shopping feed title when
# Simprosys is configured to use "SEO / HTTP title" as the feed title source.
# ──────────────────────────────────────────────────────────────────────────────

_GQL_FETCH_PRODUCTS = """
query fetchProducts($cursor: String) {
  products(first: 250, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        productType
        tags
        descriptionHtml
        seo { title }
        variants(first: 10) {
          edges { node { price } }
        }
        metafields(first: 10, namespace: "custom") {
          edges { node { key value } }
        }
      }
    }
  }
}
"""

_GQL_UPDATE_SEO_TITLE = """
mutation productSeoUpdate($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id title seo { title } }
    userErrors { field message }
  }
}
"""

_GQL_UPDATE_PRODUCT_TYPE = """
mutation setProductType($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id productType }
    userErrors { field message }
  }
}
"""

_GQL_UPDATE_METAFIELDS = """
mutation setMetafields($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { key value namespace }
    userErrors { field message }
  }
}
"""


def _gql(query, variables=None):
    """Execute a Shopify GraphQL query."""
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN_LEE_RENEE") or os.environ.get("SHOPIFY_ACCESS_TOKEN")
    if not token:
        print("ERROR: SHOPIFY_ACCESS_TOKEN_LEE_RENEE not set.")
        sys.exit(1)
    resp = requests.post(
        f"https://{SHOP}/admin/api/{API_VER}/graphql.json",
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
    )
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        raise RuntimeError(f"Shopify GQL error: {body['errors']}")
    return body["data"]


def fetch_all_products():
    """Cursor-paginate through all active products via GraphQL. Includes seo.title."""
    products = []
    cursor   = None

    while True:
        data  = _gql(_GQL_FETCH_PRODUCTS, {"cursor": cursor})
        page  = data["products"]
        for edge in page["edges"]:
            node = edge["node"]
            # Extract existing custom metafields (material, color, google_product_category)
            mf_edges = (node.get("metafields") or {}).get("edges", [])
            custom_mf = {e["node"]["key"]: e["node"]["value"] for e in mf_edges}
            # Normalise to a flat dict matching the shape the rest of the script expects
            products.append({
                "id":           int(node["id"].split("/")[-1]),
                "gid":          node["id"],
                "title":        node["title"],
                "product_type": node["productType"],
                "tags":         ", ".join(node["tags"]) if isinstance(node["tags"], list) else (node.get("tags") or ""),
                "body_html":    node.get("descriptionHtml", ""),
                "seo_title":    (node.get("seo") or {}).get("title") or "",
                "metafields":   custom_mf,
                "variants":     [
                    {"price": v["node"]["price"]}
                    for v in node["variants"]["edges"]
                ],
            })
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
        time.sleep(0.3)

    return products


def _update_product_type(gid, product_type):
    """Update product_type via GraphQL (replaces broken REST _update_product)."""
    result = _gql(_GQL_UPDATE_PRODUCT_TYPE, {"input": {"id": gid, "productType": product_type}})
    errs = (result.get("productUpdate") or {}).get("userErrors", [])
    if errs:
        raise RuntimeError(errs[0]["message"])


# ── Analysis ───────────────────────────────────────────────────────────────────

class _StripHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self._chunks = []

    def handle_data(self, data):
        self._chunks.append(data)

    def plain_text(self):
        return " ".join(self._chunks).strip()


def _strip_html(html):
    if not html:
        return ""
    p = _StripHTML()
    p.feed(html)
    return p.plain_text()


def _title_score(title):
    score = 100
    lower = title.lower()
    if not any(m in lower for m in METALS):
        score -= 35
    if len(title) < 30:
        score -= 20
    elif len(title) > 150:
        score -= 10
    if title.count("/") > 1:
        score -= 10
    return score


def _infer_product_type(title):
    """Infer product_type from title keywords. Returns (type, should_exclude)."""
    lower = title.lower()
    if any(excl in lower for excl in _EXCLUDE_FROM_FEED):
        return None, True
    for keyword, ptype in _TYPE_KEYWORDS:
        if keyword in lower:
            return ptype, False
    return None, False


def _infer_material(title):
    """Return inferred material string from title keywords, or None."""
    lower = title.lower()
    for pattern, material in _MATERIAL_PATTERNS:
        if re.search(pattern, lower):
            return material
    return None


def _infer_color(title, material=None):
    """
    Return inferred colour string.
    Checks gemstone keywords first (override), then falls back to material colour.
    """
    lower = title.lower()
    for keyword, color in _GEMSTONE_COLORS:
        if keyword in lower:
            return color
    if material:
        return _MATERIAL_COLOR.get(material)
    return None


def _infer_gpc(product_type):
    """Return Google Product Category path string for a product_type, or None."""
    if not product_type:
        return None
    return _GPC_MAP.get(product_type.strip())


def audit_products(products):
    zero_price          = []
    missing_type        = []
    thin_desc           = []
    weak_titles         = []
    missing_attributes  = []

    for p in products:
        for v in p.get("variants", []):
            try:
                if float(v["price"]) == 0.0:
                    zero_price.append({"product": p, "variant": v})
            except (ValueError, KeyError):
                pass

        if not (p.get("product_type") or "").strip():
            missing_type.append(p)

        plain = _strip_html(p.get("body_html", ""))
        if len(plain) < THIN_DESC_CHARS:
            thin_desc.append({"product": p, "desc_len": len(plain)})

        # Score against seo_title if one exists; fall back to product title.
        # We only propose changes to seo_title — never product.title.
        feed_title = p.get("seo_title") or p["title"]
        score = _title_score(feed_title)
        if score < WEAK_TITLE_SCORE:
            weak_titles.append({"product": p, "score": score})

        # Count products missing at least one enrichment metafield
        mf = p.get("metafields", {})
        excluded = any(e in p["title"].lower() for e in _EXCLUDE_FROM_FEED)
        if not excluded and not (mf.get("material") and mf.get("color") and mf.get("google_product_category")):
            missing_attributes.append(p)

    weak_titles.sort(key=lambda x: x["score"])
    return {
        "zero_price":         zero_price,
        "missing_type":       missing_type,
        "thin_desc":          thin_desc,
        "weak_titles":        weak_titles,
        "missing_attributes": missing_attributes,
    }


# ── AI title generation ────────────────────────────────────────────────────────

def _ai_propose_titles(batch):
    """
    Returns list of {id, new_title, reason} for a batch of products.
    Brand goes at the END of the title, not the front.
    """
    client = anthropic.Anthropic()

    items = "\n".join(
        f"{i+1}. ID={p['id']} | product_title={p['title']!r}"
        + (f" | current_seo_title={p['seo_title']!r}" if p.get("seo_title") else " | seo_title=(none)")
        + (f" | type={p['product_type']!r}" if p.get("product_type") else "")
        + (f" | tags={p['tags']!r}" if p.get("tags") else "")
        for i, p in enumerate(batch)
    )

    brand_short = ACCOUNT.split()[0]
    prompt = f"""Improve these Google Shopping product titles for {ACCOUNT}.

Title formula:
  [Metal/Material] [Design Name] [Product Type] – {brand_short}
  e.g. "Sterling Silver Acorn Necklace – {brand_short}"
       "9ct Gold Opal Climber Earrings – {brand_short}"

Rules:
- Lead with metal/material (Sterling Silver, 9ct Gold, Rose Gold, Gold Vermeil, etc.)
  If the metal is ambiguous from the title alone, use "Sterling Silver" as the default
- Include the design name and product type
- Brand identifier "– {brand_short}" goes at the very end
- 60–110 characters total
- UK spelling (jewellery, colour, etc.)
- No promotional words (Sale, Best, Free, Amazing)
- Keep the creative design name (Acorn, Ammonite, Pineapple, etc.) — it's part of the brand

Products:
{items}

Return only a JSON array:
[{{"id": <int>, "new_title": "<improved>", "reason": "<one-line reason for change>"}}]"""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    # Extract the JSON array by bracket-counting (safe against trailing text)
    start = text.find("[")
    if start >= 0:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    text = text[start : i + 1]
                    break
    return json.loads(text)


# ── Propose phase ──────────────────────────────────────────────────────────────

def propose_changes(products, audit):
    """
    Generates AI title proposals and infers product_types.
    Returns (title_proposals, type_proposals):
      title_proposals — list of {product_id, current, proposed, action}
      type_proposals  — list of {product_id, title, suggested_type, action, note}
    """
    # --- Title proposals ---
    candidates = [x["product"] for x in audit["weak_titles"]]
    title_proposals = []

    if candidates:
        print(f"  Generating AI title proposals for {len(candidates)} products...")
        improvements = []
        batch_count  = -(-len(candidates) // 20)
        for i in range(0, len(candidates), 20):
            batch = candidates[i:i+20]
            print(f"  Batch {i//20 + 1}/{batch_count}...")
            improvements.extend(_ai_propose_titles(batch))
            if i + 20 < len(candidates):
                time.sleep(1)

        # Deduplicate by product_id (Claude sometimes returns extras)
        seen = {}
        for imp in improvements:
            seen[imp["id"]] = imp
        improvements = [seen[pid] for pid in seen if pid in {p["id"] for p in candidates}]

        product_map = {p["id"]: p for p in candidates}
        for imp in improvements:
            pid     = imp["id"]
            product = product_map.get(pid, {})
            title   = product.get("title", "?")
            # Show current SEO title in the report (falls back to product title if none set)
            current_seo = product.get("seo_title") or title
            # Auto-skip products that should be excluded from feed
            action = "skip" if any(e in title.lower() for e in _EXCLUDE_FROM_FEED) else "approve"
            title_proposals.append({
                "product_id":  pid,
                "gid":         product.get("gid", f"gid://shopify/Product/{pid}"),
                "current":     current_seo,
                "proposed":    imp["new_title"],
                "reason":      imp.get("reason", ""),
                "action":      action,
            })

    # --- product_type proposals ---
    type_proposals = []
    for p in audit["missing_type"]:
        suggested, exclude = _infer_product_type(p["title"])
        if exclude:
            type_proposals.append({
                "product_id":    p["id"],
                "title":         p["title"],
                "suggested_type": "—",
                "action":        "skip",
                "note":          "exclude from feed",
            })
        elif suggested:
            type_proposals.append({
                "product_id":    p["id"],
                "title":         p["title"],
                "suggested_type": suggested,
                "action":        "approve",
                "note":          "",
            })
        else:
            type_proposals.append({
                "product_id":    p["id"],
                "title":         p["title"],
                "suggested_type": "?",
                "action":        "skip",
                "note":          "could not infer — set manually",
            })

    # --- Attribute proposals (material, color, google_product_category) ---
    attr_proposals = propose_attributes(products)

    return title_proposals, type_proposals, attr_proposals


def propose_attributes(products):
    """
    Infer material, color, google_product_category for products that are missing
    any of these custom metafields. Returns a list of proposal dicts.
    """
    proposals = []
    for p in products:
        if any(e in p["title"].lower() for e in _EXCLUDE_FROM_FEED):
            continue
        existing_mf = p.get("metafields", {})

        has_material = bool(existing_mf.get("material"))
        has_color    = bool(existing_mf.get("color"))
        has_gpc      = bool(existing_mf.get("google_product_category"))

        if has_material and has_color and has_gpc:
            continue  # already fully enriched

        material = existing_mf.get("material") or _infer_material(p["title"])
        color    = existing_mf.get("color") or _infer_color(p["title"], material)
        gpc      = existing_mf.get("google_product_category") or _infer_gpc(p.get("product_type"))

        # Skip rows where nothing can be inferred at all
        if not (material or color or gpc):
            continue

        proposals.append({
            "product_id":  p["id"],
            "gid":         p["gid"],
            "title":       p["title"],
            "material":    material or "",
            "color":       color or "",
            "gpc":         gpc or "",
            "action":      "approve",
            # Track which are already set (so apply can skip those)
            "has_material": has_material,
            "has_color":    has_color,
            "has_gpc":      has_gpc,
        })

    return proposals


# ── Report writing ─────────────────────────────────────────────────────────────

def write_report(products, audit, title_proposals=None, type_proposals=None, attr_proposals=None, applied_changes=None):
    os.makedirs(REPORT_DIR, exist_ok=True)
    today = date.today().isoformat()
    path  = os.path.join(REPORT_DIR, f"feed_optimisation_{today}.md")

    lines = [
        "---",
        f"tags: [feed-optimisation, shopify, {ACCOUNT.lower().replace(' ', '-')}]",
        f"date: {today}",
        "---",
        "",
        f"# Feed Optimisation — {ACCOUNT} — {today}",
        "",
        f"**Products audited:** {len(products):,}",
        "",
    ]

    if title_proposals is not None or type_proposals is not None or attr_proposals is not None:
        lines += [
            "> **How to review:** Change `approve` → `skip` to skip any change.",
            "> Edit the _Proposed_ column to adjust a suggestion.",
            "> When done, tell Claude: **\"apply the feed optimisation report\"**",
            "",
        ]

    lines += [
        "## Audit Summary",
        "",
        "| Check | Count | Impact |",
        "| --- | --- | --- |",
        f"| Zero-price variants | {len(audit['zero_price'])} | Merchant Center disapproval |",
        f"| Missing product_type | {len(audit['missing_type'])} | Feed misclassification |",
        f"| Thin descriptions (<{THIN_DESC_CHARS} chars) | {len(audit['thin_desc'])} | Lower quality score |",
        f"| Weak Shopping titles | {len(audit['weak_titles'])} | Reduced impression share |",
        f"| Missing attributes (material/color/GPC) | {len(audit['missing_attributes'])} | Lower ranking signals |",
        "",
    ]

    # Title proposals table
    if title_proposals:
        approve_count = sum(1 for t in title_proposals if t["action"] == "approve")
        lines += [
            f"## SEO Title Improvements ({approve_count} to apply, {len(title_proposals)} total)",
            "",
            "> Changes apply to **product.seo.title** only — storefront product name is unchanged.",
            "",
            "| Product ID | Current SEO Title | Proposed SEO Title | Action |",
            "| --- | --- | --- | --- |",
        ]
        for tp in title_proposals:
            lines.append(
                f"| {tp['product_id']} | {tp['current']} | {tp['proposed']} | {tp['action']} |"
            )
        lines.append("")

    # product_type proposals table
    if type_proposals:
        fixable = [t for t in type_proposals if t["action"] == "approve"]
        lines += [
            f"## Missing product_type ({len(fixable)} fixable, {len(type_proposals) - len(fixable)} manual/skip)",
            "",
            "| Product ID | Product | Suggested Type | Action | Note |",
            "| --- | --- | --- | --- | --- |",
        ]
        for tp in type_proposals:
            lines.append(
                f"| {tp['product_id']} | {tp['title']} | {tp['suggested_type']} | {tp['action']} | {tp['note']} |"
            )
        lines.append("")

    # Attribute enrichment proposals
    if attr_proposals:
        approve_count = sum(1 for a in attr_proposals if a["action"] == "approve")
        lines += [
            f"## Attribute Enrichment ({approve_count} to apply, {len(attr_proposals)} total)",
            "",
            "> Writes `custom.material`, `custom.color`, `custom.google_product_category` as Shopify metafields.",
            "> After applying, map these in Simprosys: **Feed Settings → Custom Attributes** → Metafield mapping.",
            "> Cells already set are shown as-is and will not be overwritten on apply.",
            "",
            "| Product ID | Product | Material | Color | Google Product Category | Action |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for a in attr_proposals:
            lines.append(
                f"| {a['product_id']} | {a['title']} | {a['material']} | {a['color']} | {a['gpc']} | {a['action']} |"
            )
        lines.append("")

    # Fallback: show weak titles when no proposals were generated
    if title_proposals is None and audit["weak_titles"]:
        lines += [
            f"## Weak Shopping Titles ({len(audit['weak_titles'])} candidates)",
            "",
            "These titles lack metal/material keywords. Run `--propose` to generate improvements.",
            "",
            "| Score | Title |",
            "| --- | --- |",
        ]
        for x in audit["weak_titles"][:20]:
            lines.append(f"| {x['score']} | {x['product']['title']} |")
        lines.append("")

    # Fallback: show missing types when no proposals
    if type_proposals is None and audit["missing_type"]:
        lines += [
            "## Missing product_type",
            "",
            "Run `--propose` to generate fix suggestions, or set manually:",
            "",
        ]
        for p in audit["missing_type"]:
            lines.append(
                f"- [{p['title']}](https://{SHOP}/admin/products/{p['id']})"
            )
        lines.append("")

    # Zero-price variants (always shown if present)
    if audit["zero_price"]:
        lines += [
            "## Zero-Price Variants",
            "",
            "These variants have a £0 price — causes Merchant Center disapprovals.",
            "Fix by setting the correct price or marking the variant Unavailable in Shopify.",
            "",
        ]
        for item in audit["zero_price"]:
            p, v = item["product"], item["variant"]
            lines.append(
                f"- [{p['title']} — {v['title']}]"
                f"(https://{SHOP}/admin/products/{p['id']})"
            )
        lines.append("")

    # Applied changes log
    if applied_changes:
        title_applied = [c for c in applied_changes if c["type"] == "seo_title"]
        type_applied  = [c for c in applied_changes if c["type"] == "product_type"]
        attr_applied  = [c for c in applied_changes if c["type"] == "attributes"]

        if title_applied:
            lines += [
                f"## SEO Title Changes Applied ({len(title_applied)})",
                "",
                "| Before (seo.title) | After (seo.title) |",
                "| --- | --- |",
            ]
            for c in title_applied:
                lines.append(f"| {c['before']} | {c['after']} |")
            lines.append("")

        if type_applied:
            lines += [
                f"## product_type Changes Applied ({len(type_applied)})",
                "",
                "| Product | Type Set |",
                "| --- | --- |",
            ]
            for c in type_applied:
                lines.append(f"| {c['title']} | {c['value']} |")
            lines.append("")

        if attr_applied:
            lines += [
                f"## Attribute Metafields Applied ({len(attr_applied)})",
                "",
                "| Product | Material | Color | Google Product Category |",
                "| --- | --- | --- | --- |",
            ]
            for c in attr_applied:
                lines.append(f"| {c['title']} | {c['material']} | {c['color']} | {c['gpc']} |")
            lines.append("")

    if audit["thin_desc"]:
        lines += [
            "## Thin Descriptions (top 20)",
            "",
            f"Descriptions under {THIN_DESC_CHARS} characters — improve in Shopify for better quality score.",
            "",
            "| Product | Chars |",
            "| --- | --- |",
        ]
        for item in audit["thin_desc"][:20]:
            p = item["product"]
            lines.append(
                f"| [{p['title']}]"
                f"(https://{SHOP}/admin/products/{p['id']}) | {item['desc_len']} |"
            )
        lines.append("")

    with open(path, "w") as f:
        f.write("\n".join(lines))

    return path


# ── Apply phase ────────────────────────────────────────────────────────────────

def _parse_table(content, header_marker):
    """
    Extracts rows from the first markdown table whose header line contains header_marker.
    Returns list of lists (one per data row, split on |).
    """
    rows = []
    in_table = False
    past_separator = False

    for line in content.splitlines():
        stripped = line.strip()
        if not in_table:
            if stripped.startswith("|") and header_marker in stripped:
                in_table = True
                past_separator = False
        else:
            if stripped.startswith("| ---") or stripped.startswith("|---"):
                past_separator = True
                continue
            if not stripped.startswith("|"):
                break
            if past_separator:
                cells = [c.strip() for c in stripped.split("|")[1:-1]]
                rows.append(cells)

    return rows


def latest_report_path():
    """Returns the path to the most recent feed_optimisation_*.md report."""
    pattern = os.path.join(REPORT_DIR, "feed_optimisation_*.md")
    files   = sorted(glob.glob(pattern), reverse=True)
    return files[0] if files else None


def apply_approved_changes(report_path=None, dry_run=False):
    """
    Reads the review report, applies rows where Action = 'approve'.
    Returns list of applied change dicts.
    """
    if report_path is None:
        report_path = latest_report_path()
    if not report_path or not os.path.exists(report_path):
        print("  ERROR: No feed optimisation report found.")
        return []

    print(f"  Reading report: {report_path}")
    with open(report_path) as f:
        content = f.read()

    changes = []

    # --- SEO title changes (writes to product.seo.title — never product.title) ---
    title_rows = _parse_table(content, "Current SEO Title")
    approved_titles = [r for r in title_rows if len(r) >= 4 and r[3].lower() == "approve"]

    if approved_titles:
        print(f"\n  SEO title changes to apply: {len(approved_titles)}")
        for row in approved_titles:
            pid, current, proposed = row[0], row[1], row[2]
            try:
                pid_int = int(pid)
            except ValueError:
                print(f"  ⚠️  Skipping invalid product ID: {pid!r}")
                continue
            gid = f"gid://shopify/Product/{pid_int}"
            tag = "[DRY RUN] " if dry_run else ""
            print(f"  {tag}seo.title: {current!r} → {proposed!r}")
            if not dry_run:
                result = _gql(_GQL_UPDATE_SEO_TITLE, {
                    "input": {"id": gid, "seo": {"title": proposed}}
                })
                errs = (result.get("productUpdate") or {}).get("userErrors", [])
                if errs:
                    print(f"  ⚠️  {errs[0]['message']}")
                time.sleep(0.3)
            changes.append({
                "type":       "seo_title",
                "product_id": pid_int,
                "before":     current,
                "after":      proposed,
            })
    else:
        print("  No approved SEO title changes found.")

    # --- product_type changes ---
    type_rows = _parse_table(content, "Suggested Type")
    approved_types = [r for r in type_rows if len(r) >= 4 and r[3].lower() == "approve"]

    if approved_types:
        print(f"\n  product_type changes to apply: {len(approved_types)}")
        for row in approved_types:
            pid, title, suggested = row[0], row[1], row[2]
            if suggested in ("—", "?", ""):
                continue
            try:
                pid_int = int(pid)
            except ValueError:
                print(f"  ⚠️  Skipping invalid product ID: {pid!r}")
                continue
            gid = f"gid://shopify/Product/{pid_int}"
            tag = "[DRY RUN] " if dry_run else ""
            print(f"  {tag}{title!r} → product_type={suggested!r}")
            if not dry_run:
                try:
                    _update_product_type(gid, suggested)
                except RuntimeError as e:
                    print(f"  ⚠️  {e}")
                time.sleep(0.3)
            changes.append({
                "type":       "product_type",
                "product_id": pid_int,
                "title":      title,
                "value":      suggested,
            })
    else:
        print("  No approved product_type changes found.")

    # --- Attribute metafield changes (material / color / google_product_category) ---
    attr_rows = _parse_table(content, "Google Product Category")
    approved_attrs = [r for r in attr_rows if len(r) >= 6 and r[5].lower() == "approve"]

    if approved_attrs:
        print(f"\n  Attribute metafield changes to apply: {len(approved_attrs)}")
        for row in approved_attrs:
            pid, title, material, color, gpc = row[0], row[1], row[2], row[3], row[4]
            try:
                pid_int = int(pid)
            except ValueError:
                print(f"  ⚠️  Skipping invalid product ID: {pid!r}")
                continue
            gid = f"gid://shopify/Product/{pid_int}"
            tag = "[DRY RUN] " if dry_run else ""
            gpc_short = gpc[:40]
            print(f"  {tag}{title!r}: material={material!r} color={color!r} gpc={gpc_short!r}")
            if not dry_run:
                mf_inputs = []
                for key, value in [("material", material), ("color", color), ("google_product_category", gpc)]:
                    if value:
                        mf_inputs.append({
                            "ownerId":   gid,
                            "namespace": _METAFIELD_NS,
                            "key":       key,
                            "value":     value,
                            "type":      "single_line_text_field",
                        })
                if mf_inputs:
                    result = _gql(_GQL_UPDATE_METAFIELDS, {"metafields": mf_inputs})
                    errs = (result.get("metafieldsSet") or {}).get("userErrors", [])
                    if errs:
                        print(f"  ⚠️  {errs[0]['message']}")
                time.sleep(0.3)
            changes.append({
                "type":       "attributes",
                "product_id": pid_int,
                "title":      title,
                "material":   material,
                "color":      color,
                "gpc":        gpc,
            })
    else:
        print("  No approved attribute changes found.")

    return changes


# ── Main ───────────────────────────────────────────────────────────────────────

def run_feed_optimiser(propose=False, apply=False, dry_run=False):
    """
    Main entry point.

    Args:
        propose:  fetch products, audit, generate AI proposals, write review report
        apply:    read latest report and apply approved changes
        dry_run:  show what would change without writing to Shopify
    """
    sep = "=" * 65
    print(f"\n{sep}")
    print(f"  Shopify Feed Optimiser — {ACCOUNT}")
    if dry_run:
        print("  [DRY RUN — no changes written to Shopify]")
    print(f"{sep}\n")

    if apply:
        print("  Applying approved changes from report...\n")
        changes = apply_approved_changes(dry_run=dry_run)
        if changes:
            print(f"\n  ✅ Applied {len(changes)} change(s).")
        else:
            print("\n  Nothing to apply.")
        print(f"\n{sep}\n")
        return {"changes": changes}

    print("  Fetching all active products from Shopify...")
    products = fetch_all_products()
    print(f"  Fetched {len(products):,} products.\n")

    print("  Running audit...")
    audit = audit_products(products)

    print(f"\n  Results:")
    print(f"  • Zero-price variants:  {len(audit['zero_price'])}")
    print(f"  • Missing product_type: {len(audit['missing_type'])}")
    print(f"  • Thin descriptions:    {len(audit['thin_desc'])}")
    print(f"  • Weak titles:          {len(audit['weak_titles'])}")

    title_proposals = None
    type_proposals  = None
    attr_proposals  = None

    if propose:
        print()
        title_proposals, type_proposals, attr_proposals = propose_changes(products, audit)
        if attr_proposals:
            print(f"  Attribute proposals generated: {len(attr_proposals)}")

    print("\n  Writing Obsidian report...")
    report_path = write_report(products, audit, title_proposals, type_proposals, attr_proposals)
    print(f"  ✅ {report_path}")

    if propose:
        print(f"\n  Review the report in Obsidian, then tell Claude:")
        print(f"  \"apply the feed optimisation report\"")

    print(f"\n{sep}\n")
    return {
        "audit":            audit,
        "title_proposals":  title_proposals,
        "type_proposals":   type_proposals,
        "attr_proposals":   attr_proposals,
        "report_path":      report_path,
    }


def main():
    parser = argparse.ArgumentParser(
        description=f"Shopify Feed Optimiser — {ACCOUNT}"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--propose", action="store_true",
                       help="Audit + generate AI proposals in the review report")
    group.add_argument("--apply",   action="store_true",
                       help="Apply approved changes from the latest review report")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show proposed changes without writing to Shopify")
    args = parser.parse_args()

    run_feed_optimiser(
        propose=args.propose,
        apply=args.apply,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
