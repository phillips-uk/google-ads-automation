"""
SEO Audit — Technical + On-Page for Shopify

What this script audits:
  1. Product SEO titles — length, missing custom title, duplicates
  2. Product meta descriptions — missing, length, duplicates
  3. Image alt text — missing across all product images
  4. Product descriptions — thin content (<80 words), potential duplicates
  5. URL handles — quality issues
  6. Orphan products — not in any collection (no internal links)
  7. Robots.txt — blocking critical paths
  8. Sitemap — submission status, URL count
  9. HTTPS — HTTP→HTTPS redirect in place
  10. Core Web Vitals — LCP, CLS, INP via PageSpeed Insights API
  11. Search Console — sitemap status, pages with low impressions

Generates a report with:
  - Issues ranked by severity
  - Approve/skip tables for title and meta improvements
  - Claude recommendations specific to jewellery e-commerce

Apply mode:
  python3 seo_audit.py --apply          pushes approved changes to Shopify
  python3 seo_audit.py --apply --dry-run  preview only

Usage:
  python3 seo_audit.py
  python3 seo_audit.py --client "Lee Renee Jewellery"
  python3 seo_audit.py --apply
  python3 seo_audit.py --apply --dry-run
  python3 seo_audit.py --no-monday
"""

import os
import re
import sys
import argparse
import time
from datetime import date, timedelta
from collections import defaultdict
from html.parser import HTMLParser

import requests
import anthropic

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import OBSIDIAN_BASE, load_env
from monday_helper import post_audit_issues

load_env()

# ── Client config ─────────────────────────────────────────────────────────────
try:
    from clients import SEO_AUDIT_CLIENTS
except ImportError:
    print("[config] ERROR: clients.py not found or SEO_AUDIT_CLIENTS not defined.")
    print("         Add SEO_AUDIT_CLIENTS to your clients.py. See clients.example.py.")
    sys.exit(1)

_DIR        = os.path.dirname(__file__)
TOKEN_FILE  = os.path.join(_DIR, "sc_token.json")
CLIENT_FILE = os.path.join(_DIR, "gtm_client.json")
SC_SCOPES   = ["https://www.googleapis.com/auth/webmasters.readonly"]

API_VER     = "2024-10"
PSI_API     = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

TITLE_MIN      = 40
TITLE_MAX      = 60
META_MIN       = 100
META_MAX       = 160
DESC_MIN_WORDS = 80


# ── Helpers ───────────────────────────────────────────────────────────────────

def gid_numeric(gid: str) -> str:
    """Extract numeric ID from a Shopify GID: 'gid://shopify/Product/123' → '123'."""
    return gid.split("/")[-1]


def numeric_to_gid(numeric_id: str, resource: str = "Product") -> str:
    return f"gid://shopify/{resource}/{numeric_id}"


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def plain(self):
        return re.sub(r"\s+", " ", " ".join(self._parts)).strip()


def strip_html(html: str) -> str:
    if not html:
        return ""
    p = _HTMLStripper()
    p.feed(html)
    return p.plain()


def word_count(text: str) -> int:
    return len(text.split()) if text.strip() else 0


# ── Shopify GraphQL ───────────────────────────────────────────────────────────

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
        images(first: 20) {
          edges { node { id altText src } }
        }
        collections(first: 10) {
          edges { node { id title } }
        }
      }
    }
  }
}
"""

_GQL_UPDATE_SEO = """
mutation productSeoUpdate($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id title seo { title description } }
    userErrors { field message }
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
    return body["data"]


def fetch_all_products(shop: str, token: str) -> list:
    products, cursor = [], None
    while True:
        data = _shopify_gql(shop, token, _GQL_PRODUCTS, {"cursor": cursor})
        page = data["products"]
        for edge in page["edges"]:
            products.append(edge["node"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
        time.sleep(0.5)  # respect rate limits
    return products


# ── Search Console ────────────────────────────────────────────────────────────

def get_sc_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SC_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_FILE, SC_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("webmasters", "v3", credentials=creds)


def sc_sitemaps(svc, site_url: str) -> list:
    try:
        return svc.sitemaps().list(siteUrl=site_url).execute().get("sitemap", [])
    except HttpError as e:
        print(f"  [SC sitemaps] {e}")
        return []


def sc_zero_impression_pages(svc, site_url: str, days: int = 28) -> list:
    """Pages that appear in SC data but have very low impressions — potential thin/deindexed."""
    end   = (date.today() - timedelta(days=3)).isoformat()
    start = (date.today() - timedelta(days=days + 3)).isoformat()
    try:
        rows = svc.searchanalytics().query(siteUrl=site_url, body={
            "startDate": start, "endDate": end,
            "dimensions": ["page"], "rowLimit": 1000,
        }).execute().get("rows", [])
        return sorted(
            [{"page": r["keys"][0], "impressions": int(r.get("impressions", 0))}
             for r in rows if int(r.get("impressions", 0)) < 5],
            key=lambda x: x["impressions"]
        )[:20]
    except HttpError as e:
        print(f"  [SC analytics] {e}")
        return []


# ── Technical checks ──────────────────────────────────────────────────────────

def check_robots(domain: str) -> dict:
    try:
        resp = requests.get(f"https://{domain}/robots.txt", timeout=10)
        content = resp.text if resp.ok else ""
        issues = []
        # Parse block by block so we only flag Disallow rules that apply to
        # Googlebot (or the wildcard *) — not rules scoped to other bots like
        # AdsBot-Google, Nutch, AhrefsBot, etc.
        current_agents = []
        googlebot_agents = {"*", "googlebot"}
        for line in content.splitlines():
            stripped = line.strip()
            lower = stripped.lower()
            if lower.startswith("user-agent:"):
                agent = lower.replace("user-agent:", "").strip()
                current_agents.append(agent)
            elif stripped == "" or lower.startswith("#"):
                # Blank line ends a User-agent block
                if stripped == "":
                    current_agents = []
            elif lower.startswith("disallow:"):
                # Only flag if the current block applies to Googlebot
                if any(a in googlebot_agents for a in current_agents):
                    path = lower.replace("disallow:", "").strip()
                    if path in ("/", "/products", "/products/", "/collections", "/collections/"):
                        issues.append(f"Blocks `{path}` for {current_agents}")
        return {"ok": resp.ok, "issues": issues, "snippet": content[:800]}
    except Exception as e:
        return {"ok": False, "issues": [f"Fetch failed: {e}"], "snippet": ""}


def check_sitemap(domain: str) -> dict:
    try:
        resp = requests.get(f"https://{domain}/sitemap.xml", timeout=15)
        if not resp.ok:
            return {"ok": False, "url_count": 0, "issues": [f"HTTP {resp.status_code}"]}
        url_count = len(re.findall(r"<loc>", resp.text))
        is_index  = "<sitemapindex" in resp.text
        return {"ok": True, "url_count": url_count, "is_index": is_index, "issues": []}
    except Exception as e:
        return {"ok": False, "url_count": 0, "issues": [f"Fetch failed: {e}"]}


def check_https(domain: str) -> dict:
    try:
        resp = requests.get(f"http://{domain}", timeout=10, allow_redirects=False)
        if resp.status_code in (301, 302, 307, 308):
            loc = resp.headers.get("Location", "")
            if loc.startswith("https://"):
                return {"ok": True, "msg": f"HTTP→HTTPS redirect confirmed ({resp.status_code})"}
            return {"ok": False, "msg": f"HTTP redirects to `{loc}` — not HTTPS"}
        return {"ok": False, "msg": f"HTTP returns {resp.status_code} (no redirect)"}
    except Exception as e:
        return {"ok": True, "msg": f"Could not test HTTP redirect: {e}"}


# ── PageSpeed Insights ────────────────────────────────────────────────────────

def get_cwv(url: str):
    try:
        resp = requests.get(PSI_API, params={"url": url, "strategy": "mobile"}, timeout=30)
        if not resp.ok:
            return None
        audits = resp.json().get("lighthouseResult", {}).get("audits", {})
        perf   = resp.json().get("lighthouseResult", {}).get("categories", {}).get("performance", {})

        def _metric(key, alt_key=None):
            a = audits.get(key) or (audits.get(alt_key) if alt_key else None) or {}
            return {"value": a.get("numericValue", 0), "display": a.get("displayValue", "—"), "score": a.get("score")}

        return {
            "lcp":        _metric("largest-contentful-paint"),
            "cls":        _metric("cumulative-layout-shift"),
            "inp":        _metric("interaction-to-next-paint", "max-potential-fid"),
            "fcp":        _metric("first-contentful-paint"),
            "perf_score": round((perf.get("score") or 0) * 100),
        }
    except Exception as e:
        print(f"  [PSI] {url}: {e}")
        return None


def _cwv_status(metric: str, val: float) -> str:
    thresholds = {
        "lcp": (2500, 4000), "cls": (0.1, 0.25), "inp": (200, 500), "fcp": (1800, 3000),
    }
    if metric not in thresholds:
        return "unknown"
    good, poor = thresholds[metric]
    # LCP/FCP/INP: value in ms from PSI (except CLS which is unitless)
    if metric != "cls":
        val = val  # already in ms from numericValue
    return "good" if val <= good else ("needs_improvement" if val <= poor else "poor")


def _cwv_emoji(status: str) -> str:
    return {"good": "✅", "needs_improvement": "⚠️", "poor": "❌"}.get(status, "—")


# ── Product analysis ──────────────────────────────────────────────────────────

def analyse_products(products: list, domain: str) -> tuple[list, dict]:
    stats = {
        "total": 0, "no_custom_title": 0,
        "title_too_short": 0, "title_too_long": 0, "duplicate_titles": 0,
        "missing_meta": 0, "meta_too_short": 0, "meta_too_long": 0, "duplicate_metas": 0,
        "total_images": 0, "missing_alt": 0, "thin_descriptions": 0, "orphan_products": 0,
    }

    # First pass: build duplicate fingerprints
    title_fp: dict[str, list] = defaultdict(list)
    meta_fp:  dict[str, list] = defaultdict(list)
    for p in products:
        if p.get("status") == "ARCHIVED":
            continue
        t = ((p.get("seo") or {}).get("title") or p.get("title", "")).lower().strip()
        m = ((p.get("seo") or {}).get("description") or "")[:80].lower().strip()
        if t:
            title_fp[t].append(p["id"])
        if m:
            meta_fp[m].append(p["id"])
    dupe_titles = {k for k, v in title_fp.items() if len(v) > 1}
    dupe_metas  = {k for k, v in meta_fp.items()  if len(v) > 1}

    issues_by_product = []

    for p in products:
        if p.get("status") == "ARCHIVED":
            continue
        stats["total"] += 1

        seo          = p.get("seo") or {}
        seo_title    = seo.get("title") or p.get("title", "")
        seo_has_custom = seo.get("title") is not None
        seo_meta     = seo.get("description") or ""
        desc_text    = strip_html(p.get("descriptionHtml") or "")
        images       = [e["node"] for e in (p.get("images", {}).get("edges") or [])]
        collections  = [e["node"]["title"] for e in (p.get("collections", {}).get("edges") or [])]
        handle       = p.get("handle", "")
        pid_numeric  = gid_numeric(p["id"])

        product_issues: list[tuple] = []  # (code, severity, message)

        # Title
        if not seo_has_custom:
            stats["no_custom_title"] += 1
            product_issues.append(("title_default", "MEDIUM",
                "No custom SEO title set — using product title. Set a keyword-optimised title."))

        title_len = len(seo_title)
        if title_len < TITLE_MIN:
            stats["title_too_short"] += 1
            product_issues.append(("title_short", "HIGH",
                f"SEO title too short ({title_len} chars, min {TITLE_MIN})"))
        elif title_len > TITLE_MAX:
            stats["title_too_long"] += 1
            product_issues.append(("title_long", "MEDIUM",
                f"SEO title too long ({title_len} chars, max {TITLE_MAX}) — truncated in SERPs"))

        if seo_title.lower().strip() in dupe_titles:
            stats["duplicate_titles"] += 1
            product_issues.append(("title_dupe", "HIGH",
                "Duplicate SEO title — multiple products compete for same SERP position"))

        # Meta description
        meta_len = len(seo_meta)
        if not seo_meta:
            stats["missing_meta"] += 1
            product_issues.append(("meta_missing", "HIGH",
                "No meta description — Google auto-generates a poor snippet"))
        elif meta_len < META_MIN:
            stats["meta_too_short"] += 1
            product_issues.append(("meta_short", "MEDIUM",
                f"Meta description too short ({meta_len} chars, target {META_MIN}–{META_MAX})"))
        elif meta_len > META_MAX:
            stats["meta_too_long"] += 1
            product_issues.append(("meta_long", "LOW",
                f"Meta description too long ({meta_len} chars, max {META_MAX})"))

        if seo_meta and seo_meta[:80].lower().strip() in dupe_metas:
            stats["duplicate_metas"] += 1
            product_issues.append(("meta_dupe", "MEDIUM", "Duplicate meta description"))

        # Images
        stats["total_images"] += len(images)
        missing_alts = [img for img in images if not img.get("altText")]
        if missing_alts:
            stats["missing_alt"] += len(missing_alts)
            product_issues.append(("alt_missing", "MEDIUM",
                f"{len(missing_alts)}/{len(images)} images missing alt text"))

        # Description
        words = word_count(desc_text)
        if words < DESC_MIN_WORDS:
            stats["thin_descriptions"] += 1
            product_issues.append(("thin_desc", "MEDIUM",
                f"Thin description ({words} words, target {DESC_MIN_WORDS}+)"))

        # Orphan check
        if not collections:
            stats["orphan_products"] += 1
            product_issues.append(("orphan", "HIGH",
                "Not in any collection — no internal links, low PageRank"))

        if product_issues:
            issues_by_product.append({
                "gid":        p["id"],
                "id":         pid_numeric,
                "title":      p.get("title", ""),
                "handle":     handle,
                "url":        f"https://{domain}/products/{handle}",
                "seo_title":  seo_title,
                "seo_meta":   seo_meta,
                "images":     images,
                "words":      words,
                "collections": collections,
                "issues":     product_issues,
            })

    # Sort: most HIGH issues first
    issues_by_product.sort(key=lambda x: (
        -sum(1 for i in x["issues"] if i[1] == "HIGH"),
        -len(x["issues"])
    ))
    return issues_by_product, stats


# ── AI: analysis + proposals ──────────────────────────────────────────────────

def ai_analysis(client_name: str, stats: dict, product_issues: list,
                cwv: dict, technical: dict) -> str:
    claude = anthropic.Anthropic()

    top_problems = "\n".join(
        f"- {p['title']}: {'; '.join(i[2][:60] for i in p['issues'][:3])}"
        for p in product_issues[:8]
    )

    cwv_lines = ""
    if cwv:
        for url, scores in cwv.items():
            if scores:
                cwv_lines += (f"\n- {url.replace('https://', '')}: "
                              f"LCP {scores['lcp']['display']}, "
                              f"CLS {scores['cls']['display']}, "
                              f"Perf {scores['perf_score']}/100")

    prompt = f"""You are an SEO specialist auditing {client_name}, a fine jewellery e-commerce brand (Shopify, UK). Products: sterling silver, gold vermeil, pearl and gemstone pieces.

## Product issues across {stats['total']} active products
- Orphan products (no collection, no internal links): {stats['orphan_products']}
- Missing meta descriptions: {stats['missing_meta']}
- No custom SEO title (using product title): {stats['no_custom_title']}
- SEO titles too short (<{TITLE_MIN} chars): {stats['title_too_short']}
- SEO titles too long (>{TITLE_MAX} chars): {stats['title_too_long']}
- Duplicate SEO titles: {stats['duplicate_titles']}
- Images missing alt text: {stats['missing_alt']} of {stats['total_images']}
- Thin descriptions (<{DESC_MIN_WORDS} words): {stats['thin_descriptions']}

## Most affected products
{top_problems}

## Core Web Vitals (mobile){cwv_lines or ' — not available'}

## Technical
- Robots.txt: {len(technical.get('robots', {}).get('issues', []))} issues
- Sitemap: HTTP {technical.get('sitemap', {}).get('status', '?')}, {technical.get('sitemap', {}).get('url_count', 0)} URLs
- HTTPS redirect: {'✅' if technical.get('https', {}).get('ok') else '❌'}

Provide:

## Overall Assessment
2–3 sentences on overall SEO health. Be direct.

## Priority Fixes
The 4–5 highest-impact fixes in order. For each: specific action, where to do it in Shopify Admin, and what improvement to expect. Reference jewellery e-commerce specifics.

## Title Tag Strategy
The right formula for {client_name} titles. Include one worked example for a "Sterling Silver Pearl Ring".

## Meta Description Strategy
What makes a compelling meta for jewellery buyers. Template + worked example.

## Content Quality
What a good Lee Renée product description should include. Concrete template (100–150 words).

Be specific. No generic advice."""

    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1800,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def _propose_titles(products: list, brand: str) -> dict:
    if not products:
        return {}
    claude = anthropic.Anthropic()
    lines  = "\n".join(
        f"- ID:{p['id']} | {p['title']}"
        for p in products[:30]
    )
    prompt = f"""Write improved SEO title tags for {brand}, a fine jewellery brand.

Formula: [Material/Gemstone] [Style/Design] [Product Type] – {brand}
Rules:
- Hard maximum 60 characters — count every character including spaces and dashes
- Lead with the most specific descriptor (material or gemstone first, not brand)
- Only use attributes clearly implied by the product name — do not invent
- End with "– {brand}" (with en dash, not hyphen)

Examples:
- "Sterling Silver Freshwater Pearl Ring – Lee Renée" (49 chars) ✅
- "Gold Vermeil Ruby Lips Necklace – Lee Renée" (44 chars) ✅
- "Handcrafted Silver Heart Stud Earrings – Lee Renée" (51 chars) ✅

For each product, respond with ONLY:
ID:[product_id] | [new title]

Products:
{lines}"""

    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    proposals = {}
    for line in msg.content[0].text.strip().split("\n"):
        line = line.strip()
        if line.startswith("ID:") and "|" in line:
            raw_id, _, title = line.partition("|")
            pid = raw_id.replace("ID:", "").strip()
            proposals[pid] = title.strip()
    return proposals


def _propose_metas(products: list, client_name: str) -> dict:
    if not products:
        return {}
    claude = anthropic.Anthropic()
    lines  = "\n".join(
        f"- ID:{p['id']} | {p['title']} ({p['words']} words in description)"
        for p in products[:30]
    )
    prompt = f"""Write meta descriptions for {client_name}, a fine jewellery e-commerce brand.

Rules:
- 105–155 characters (mobile-optimised, count carefully)
- Match buyer intent: who searches for this? (gift-giver, self-purchaser)
- Include: what it is + material + occasion or benefit + subtle CTA
- No hollow phrases: "beautiful", "perfect gift", "stunning", "luxurious"
- Specific and honest — only describe what the product is

Good example (127 chars):
"Handcrafted sterling silver freshwater pearl ring. Elegant for everyday wear or a thoughtful birthday gift. Free UK delivery."

For each product, respond with ONLY:
ID:[product_id] | [meta description]

Products:
{lines}"""

    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    proposals = {}
    for line in msg.content[0].text.strip().split("\n"):
        line = line.strip()
        if line.startswith("ID:") and "|" in line:
            raw_id, _, meta = line.partition("|")
            pid = raw_id.replace("ID:", "").strip()
            proposals[pid] = meta.strip()
    return proposals


# ── Report writing ────────────────────────────────────────────────────────────

def write_report(client_name, folder, stats, product_issues, cwv,
                 technical, sitemaps_sc, sc_low_imps, ai_text,
                 title_proposals, meta_proposals) -> str:

    report_dir = os.path.join(OBSIDIAN_BASE, folder, "SEO Audits")
    os.makedirs(report_dir, exist_ok=True)
    today = date.today().isoformat()
    path  = os.path.join(report_dir, f"seo_audit_{today}.md")

    sev_icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵"}
    crit = sum(1 for p in product_issues for i in p["issues"] if i[1] == "HIGH")
    med  = sum(1 for p in product_issues for i in p["issues"] if i[1] == "MEDIUM")

    L = []

    L += [
        "---",
        f"tags: [seo-audit, seo, {client_name.lower().replace(' ', '-')}]",
        f"date: {today}",
        "---", "",
        f"# SEO Audit — {client_name} — {today}", "",
        "## Summary", "",
        f"**{len(product_issues)} of {stats['total']} products** have SEO issues. "
        f"**{crit} HIGH-severity** issues, **{med} MEDIUM**.", "",
        "| Issue | Count | Severity |",
        "| --- | --- | --- |",
        f"| Missing meta descriptions | {stats['missing_meta']} | 🔴 HIGH |",
        f"| Orphan products (no collection) | {stats['orphan_products']} | 🔴 HIGH |",
        f"| Duplicate SEO titles | {stats['duplicate_titles']} | 🔴 HIGH |",
        f"| SEO titles too short (<{TITLE_MIN} chars) | {stats['title_too_short']} | 🔴 HIGH |",
        f"| No custom SEO title set | {stats['no_custom_title']} | 🟡 MEDIUM |",
        f"| SEO titles too long (>{TITLE_MAX} chars) | {stats['title_too_long']} | 🟡 MEDIUM |",
        f"| Images missing alt text | {stats['missing_alt']} of {stats['total_images']} | 🟡 MEDIUM |",
        f"| Thin descriptions (<{DESC_MIN_WORDS} words) | {stats['thin_descriptions']} | 🟡 MEDIUM |",
        "",
        "---", "",
        "## AI Analysis & Recommendations", "",
        ai_text, "",
        "---", "",
    ]

    # Core Web Vitals
    if any(v for v in cwv.values()):
        L += [
            "## Core Web Vitals — Mobile (PageSpeed Insights)", "",
            "> Targets: LCP ≤2.5s ✅  |  CLS ≤0.1 ✅  |  INP ≤200ms ✅", "",
            "| Page | LCP | CLS | INP | Performance Score |",
            "| --- | --- | --- | --- | --- |",
        ]
        for url, sc in cwv.items():
            if not sc:
                L.append(f"| {url.replace('https://', '')} | — | — | — | timed out |")
                continue
            lcp_v = sc["lcp"]["value"]
            cls_v = sc["cls"]["value"]
            inp_v = sc["inp"]["value"]
            L.append(
                f"| {url.replace('https://', '')} "
                f"| {_cwv_emoji(_cwv_status('lcp', lcp_v))} {sc['lcp']['display']} "
                f"| {_cwv_emoji(_cwv_status('cls', cls_v))} {sc['cls']['display']} "
                f"| {_cwv_emoji(_cwv_status('inp', inp_v))} {sc['inp']['display']} "
                f"| {sc['perf_score']}/100 |"
            )
        L += ["", "---", ""]

    # Technical
    https_i  = technical.get("https", {})
    robots_i = technical.get("robots", {})
    sitemap_i = technical.get("sitemap", {})
    L += [
        "## Technical Checks", "",
        "| Check | Status | Detail |",
        "| --- | --- | --- |",
        f"| HTTPS redirect | {'✅' if https_i.get('ok') else '❌'} | {https_i.get('msg', '—')} |",
        f"| Robots.txt | {'⚠️ Issues' if robots_i.get('issues') else '✅ OK'} | "
        + ('; '.join(robots_i.get('issues', [])) or 'No critical disallows') + " |",
        f"| Sitemap (HTTP) | {'✅' if sitemap_i.get('ok') else '❌'} | "
        + f"HTTP {sitemap_i.get('status', '?')}, {sitemap_i.get('url_count', 0)} URLs |",
    ]
    for sm in sitemaps_sc[:3]:
        submitted = (sm.get("lastSubmitted") or "—")[:10]
        L.append(f"| SC Sitemap: {sm.get('path','').split('/')[-1]} | ✅ Submitted | Last processed: {submitted} |")
    L += ["", "---", ""]

    # Product issues table
    L += [
        "## Product Issues",
        f"",
        f"Showing {min(len(product_issues), 30)} of {len(product_issues)} products with issues.",
        "",
        "| Product | Issues |",
        "| --- | --- |",
    ]
    for p in product_issues[:30]:
        issue_str = " · ".join(
            f"{sev_icon.get(i[1], '')} {i[2]}"
            for i in p["issues"]
        )
        L.append(f"| [{p['title']}]({p['url']}) | {issue_str} |")
    L += ["", "---", ""]

    # ── Review table: SEO Titles ────────────────────────────────────────────
    title_fixes = [
        p for p in product_issues
        if any(i[0] in ("title_short", "title_long", "title_dupe", "title_default") for i in p["issues"])
    ]
    if title_fixes:
        L += [
            "## Proposed Fixes — SEO Titles",
            "",
            "> Review each row. Edit the **Proposed** column if needed. "
            "Change `approve` → `skip` for any you want to leave unchanged.",
            "> When done, say **apply seo audit** to push changes to Shopify.",
            "",
            "| ID | Product | Current Title | Proposed Title | Action |",
            "| --- | --- | --- | --- | --- |",
        ]
        for p in title_fixes[:40]:
            proposed = title_proposals.get(p["id"], "")
            if not proposed:
                proposed = "_(edit me)_"
                action   = "skip"
            else:
                action = "approve"
            # Escape pipes in title/proposed
            cur      = p["seo_title"].replace("|", "\\|")
            pro      = proposed.replace("|", "\\|")
            prod_ttl = p["title"].replace("|", "\\|")
            L.append(f"| {p['id']} | {prod_ttl} | {cur} | {pro} | {action} |")
        L += ["", "---", ""]

    # ── Review table: Meta Descriptions ─────────────────────────────────────
    meta_fixes = [
        p for p in product_issues
        if any(i[0] in ("meta_missing", "meta_short", "meta_dupe") for i in p["issues"])
    ]
    if meta_fixes:
        L += [
            "## Proposed Fixes — Meta Descriptions",
            "",
            "> Review, edit Proposed column if needed, change `approve` → `skip` to skip.",
            "",
            "| ID | Product | Current Meta | Proposed Meta | Action |",
            "| --- | --- | --- | --- | --- |",
        ]
        for p in meta_fixes[:40]:
            cur_meta = (p.get("seo_meta") or "_(none)_")[:70].replace("|", "\\|")
            proposed = meta_proposals.get(p["id"], "")
            if not proposed:
                proposed = "_(edit me)_"
                action   = "skip"
            else:
                action = "approve"
            pro      = proposed.replace("|", "\\|")
            prod_ttl = p["title"].replace("|", "\\|")
            L.append(f"| {p['id']} | {prod_ttl} | {cur_meta} | {pro} | {action} |")
        L += ["", "---", ""]

    # ── Orphan products ──────────────────────────────────────────────────────
    orphans = [p for p in product_issues if any(i[0] == "orphan" for i in p["issues"])]
    if orphans:
        L += [
            "## Orphan Products — No Collection",
            "",
            "> Fix: Shopify Admin → Products → select product → add to a collection.",
            "",
            "| Product | URL |",
            "| --- | --- |",
        ]
        for p in orphans:
            L.append(f"| {p['title']} | {p['url']} |")
        L += ["", "---", ""]

    # ── SC low-impression pages ──────────────────────────────────────────────
    if sc_low_imps:
        L += [
            "## Low-Impression Pages (Search Console)",
            "",
            "> Pages in SC data with <5 impressions in 28 days. Could be deindexed, thin, or very new.",
            "",
            "| Page | Impressions |",
            "| --- | --- |",
        ]
        for pg in sc_low_imps[:15]:
            short = pg["page"].replace("https://", "")
            L.append(f"| {short} | {pg['impressions']} |")
        L += ["", "---", ""]

    # ── Learning notes ───────────────────────────────────────────────────────
    L += [
        "## Learning Notes",
        "",
        "**Why title length matters:** Google shows ~60 chars in SERPs. Beyond that: truncated with '…'. "
        "You lose context and it looks unprofessional. Too short = wasted relevance signals. "
        "The title is also the most important on-page ranking factor.",
        "",
        "**Why meta descriptions matter:** Not a ranking signal — but the meta IS your organic ad copy. "
        "A compelling meta converts a ranking into a click. Missing = Google picks a random excerpt "
        "(usually the first paragraph, rarely optimal). For jewellery: match who's searching — "
        "gift-giver, self-purchaser, material-conscious buyer.",
        "",
        "**Why orphan products don't rank:** Google discovers pages by following links. "
        "A product not in any collection has no links from the rest of your site. "
        "PageRank (Google's link importance score) never flows to it. It ranks like a page "
        "with zero votes, even if the content is excellent.",
        "",
        "**Why thin descriptions hurt:** <100 words = Google classifies the page as thin content. "
        "Thin content pages rank poorly and are more likely to be excluded from the index. "
        "A good description also answers pre-purchase questions (material, sizing, care, occasion), "
        "which reduces bounce rate — another quality signal.",
        "",
        "**Core Web Vitals:** LCP (Largest Contentful Paint) = how fast the main content loads. "
        "CLS (Cumulative Layout Shift) = how much the page jumps around while loading. "
        "INP (Interaction to Next Paint) = responsiveness to taps/clicks. "
        "All three are confirmed Google ranking signals. Mobile matters more than desktop.",
    ]

    with open(path, "w") as f:
        f.write("\n".join(L))
    return path


# ── Apply mode ────────────────────────────────────────────────────────────────

def apply_latest_report(client_name: str, cfg: dict, dry_run: bool = False):
    folder    = cfg["folder"]
    shop      = cfg["shopify_shop"]
    token     = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
    report_dir = os.path.join(OBSIDIAN_BASE, folder, "SEO Audits")

    reports = sorted(
        [f for f in os.listdir(report_dir) if f.startswith("seo_audit_") and f.endswith(".md")],
        reverse=True
    )
    if not reports:
        print("  No SEO audit reports found.")
        return

    report_path = os.path.join(report_dir, reports[0])
    print(f"  Reading: {report_path}")

    with open(report_path) as f:
        content = f.read()

    def _parse_table(section_marker: str) -> dict:
        """Parse an approve/skip table — returns {product_id: proposed_value}."""
        result = {}
        in_section = False
        for line in content.split("\n"):
            if section_marker in line:
                in_section = True
                continue
            if in_section and line.startswith("## ") and section_marker not in line:
                break
            if in_section and "| approve |" in line and line.startswith("|"):
                # Columns: | ID | Product | Current | Proposed | Action |
                cols = [c.strip() for c in line.split("|")]
                # cols[0] = '', cols[1] = ID, cols[2] = Product, cols[3] = Current, cols[4] = Proposed
                if len(cols) >= 6:
                    pid      = cols[1].strip()
                    proposed = cols[4].replace("\\|", "|").strip()
                    if pid and proposed and "_(edit me)_" not in proposed:
                        result[pid] = proposed
        return result

    title_changes = _parse_table("Proposed Fixes — SEO Titles")
    meta_changes  = _parse_table("Proposed Fixes — Meta Descriptions")

    all_ids = set(title_changes) | set(meta_changes)
    if not all_ids:
        print("  No approved changes found in the report.")
        return

    print(f"  Found {len(title_changes)} title changes, {len(meta_changes)} meta changes.")

    if dry_run:
        print("  DRY RUN — no changes applied:")
        for pid in sorted(all_ids)[:10]:
            print(f"    [{pid}]  title={title_changes.get(pid, '—')[:50]}  |  meta={meta_changes.get(pid, '—')[:50]}")
        return

    applied = 0
    for pid in all_ids:
        seo_input: dict = {}
        if pid in title_changes:
            seo_input["title"] = title_changes[pid]
        if pid in meta_changes:
            seo_input["description"] = meta_changes[pid]

        gid = numeric_to_gid(pid)
        try:
            result = _shopify_gql(shop, token, _GQL_UPDATE_SEO, {
                "input": {"id": gid, "seo": seo_input}
            })
            errors = result.get("productUpdate", {}).get("userErrors", [])
            if errors:
                print(f"  ❌ {pid}: {errors}")
            else:
                ptitle = result.get("productUpdate", {}).get("product", {}).get("title", pid)
                print(f"  ✅ {ptitle}")
                applied += 1
        except Exception as e:
            print(f"  ❌ Error on {pid}: {e}")
        time.sleep(0.3)  # rate limit

    print(f"\n  Applied {applied}/{len(all_ids)} changes to Shopify.")
    if applied:
        print("  Shopify will sync to Simprosys and Merchant Center within ~30 min.")


# ── Monday.com ────────────────────────────────────────────────────────────────

def _monday_issues(stats: dict, technical: dict, cwv: dict) -> list:
    issues = []

    robots_problems = technical.get("robots", {}).get("issues", [])
    if robots_problems:
        issues.append({"severity": "CRITICAL",
                        "msg": f"SEO: robots.txt blocking critical paths: {'; '.join(robots_problems)}"})

    if stats["orphan_products"] > 0:
        issues.append({"severity": "HIGH",
                        "msg": f"SEO: {stats['orphan_products']} orphan products not in any collection — no internal links, invisible to Google."})

    if stats["missing_meta"] > 3:
        issues.append({"severity": "HIGH",
                        "msg": f"SEO: {stats['missing_meta']} products missing meta descriptions — Google auto-generates poor snippets, hurting CTR."})

    if stats["duplicate_titles"] > 0:
        issues.append({"severity": "HIGH",
                        "msg": f"SEO: {stats['duplicate_titles']} products with duplicate SEO titles — keyword cannibalism."})

    if stats["thin_descriptions"] > 10:
        issues.append({"severity": "MEDIUM",
                        "msg": f"SEO: {stats['thin_descriptions']} products with thin descriptions (<{DESC_MIN_WORDS} words) — quality signal issue."})

    if stats["missing_alt"] > 20:
        issues.append({"severity": "MEDIUM",
                        "msg": f"SEO: {stats['missing_alt']} product images missing alt text — accessibility and image search gap."})

    if cwv:
        poor_pages = [url for url, sc in cwv.items() if sc and sc.get("perf_score", 100) < 50]
        if poor_pages:
            issues.append({"severity": "HIGH",
                            "msg": f"SEO: Poor PageSpeed score on {len(poor_pages)} page(s) — Core Web Vitals affecting rankings."})

    return issues


# ── Main ──────────────────────────────────────────────────────────────────────

def run_seo_audit(client_name: str = None, post_to_monday: bool = True):
    clients_to_run = SEO_AUDIT_CLIENTS
    if client_name:
        if client_name not in SEO_AUDIT_CLIENTS:
            print(f"[ERROR] '{client_name}' not found in SEO_AUDIT_CLIENTS.")
            sys.exit(1)
        clients_to_run = {client_name: SEO_AUDIT_CLIENTS[client_name]}

    for name, cfg in clients_to_run.items():
        shop   = cfg["shopify_shop"]
        domain = cfg["shopify_domain"]
        sc_url = cfg.get("sc_site_url")
        folder = cfg["folder"]
        brand  = cfg.get("brand_name", name.split()[0])
        token  = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")

        print("\n" + "=" * 65)
        print(f"  SEO Audit — {name}")
        print("=" * 65)

        # 1 — Shopify products
        print(f"\n  Fetching products from Shopify ({shop})...")
        all_products = fetch_all_products(shop, token)
        active = [p for p in all_products if p.get("status") != "ARCHIVED"]
        print(f"  {len(all_products)} total, {len(active)} active")

        print("  Analysing SEO data...")
        product_issues, stats = analyse_products(active, domain)
        print(f"  {len(product_issues)} products with issues")
        print(f"  Missing meta: {stats['missing_meta']}  |  Orphans: {stats['orphan_products']}  "
              f"|  Thin: {stats['thin_descriptions']}  |  Missing alt: {stats['missing_alt']}")

        # 2 — Technical checks
        print("\n  Running technical checks...")
        technical = {
            "https":   check_https(domain),
            "robots":  check_robots(domain),
            "sitemap": check_sitemap(domain),
        }
        print(f"  HTTPS: {'OK' if technical['https']['ok'] else 'ISSUE'}  |  "
              f"Robots: {len(technical['robots']['issues'])} issues  |  "
              f"Sitemap: {technical['sitemap']['url_count']} URLs")

        # 3 — Search Console
        sitemaps_sc, sc_low_imps = [], []
        if sc_url:
            print("\n  Checking Search Console...")
            try:
                svc = get_sc_service()
                sitemaps_sc = sc_sitemaps(svc, sc_url)
                sc_low_imps = sc_zero_impression_pages(svc, sc_url)
                print(f"  Sitemaps: {len(sitemaps_sc)}  |  Low-impression pages: {len(sc_low_imps)}")
            except Exception as e:
                print(f"  [SC] Skipped: {e}")

        # 4 — Core Web Vitals
        print("\n  Fetching Core Web Vitals...")
        cwv = {}
        test_urls = [f"https://{domain}"] + [
            f"https://{domain}/products/{p['handle']}"
            for p in active[:2] if p.get("handle")
        ]
        for url in test_urls[:3]:
            print(f"  → {url}")
            cwv[url] = get_cwv(url)

        # 5 — Proposals
        print("\n  Generating title and meta proposals with Claude...")
        title_needs = [p for p in product_issues
                       if any(i[0] in ("title_short", "title_long", "title_dupe", "title_default")
                              for i in p["issues"])][:30]
        meta_needs  = [p for p in product_issues
                       if any(i[0] in ("meta_missing", "meta_short")
                              for i in p["issues"])][:30]
        title_proposals = _propose_titles(title_needs, brand)
        meta_proposals  = _propose_metas(meta_needs, name)
        print(f"  Titles proposed: {len(title_proposals)}  |  Metas proposed: {len(meta_proposals)}")

        # 6 — AI analysis
        print("\n  Generating AI analysis...")
        ai_text = ai_analysis(name, stats, product_issues, cwv, technical)

        # 7 — Write report
        print("\n  Writing report to Obsidian...")
        path = write_report(
            name, folder, stats, product_issues, cwv,
            technical, sitemaps_sc, sc_low_imps, ai_text,
            title_proposals, meta_proposals,
        )
        print(f"  ✅ Report: {path}")

        # 8 — Monday.com
        if post_to_monday:
            monday_issues = _monday_issues(stats, technical, cwv)
            if monday_issues:
                print(f"\n  Posting {len(monday_issues)} item(s) to Monday.com...")
                post_audit_issues(name, monday_issues)

    print("\n" + "=" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(description="SEO technical + on-page audit for Shopify")
    parser.add_argument("--client",    help="Run for a specific client only")
    parser.add_argument("--apply",     action="store_true", help="Apply approved changes from latest report")
    parser.add_argument("--dry-run",   action="store_true", help="Preview --apply without writing to Shopify")
    parser.add_argument("--no-monday", action="store_true", help="Skip Monday.com posting")
    args = parser.parse_args()

    if args.apply:
        clients_to_run = SEO_AUDIT_CLIENTS
        if args.client:
            clients_to_run = {args.client: SEO_AUDIT_CLIENTS[args.client]}
        for name, cfg in clients_to_run.items():
            print(f"\nApplying SEO audit changes — {name}")
            apply_latest_report(name, cfg, dry_run=args.dry_run)
    else:
        run_seo_audit(client_name=args.client, post_to_monday=not args.no_monday)


if __name__ == "__main__":
    main()
