"""
Feed Health — Shopify + Merchant Center

Weekly unified feed check combining Merchant Center status and Shopify data quality.

Workflow:
  1. python3 feed_health.py
     Audits both Merchant Center and Shopify, generates a review report in Obsidian
     with proposed fixes (titles, product_types) pre-filled and ready to approve.
  2. Open the report in Obsidian. Change approve → skip to skip anything, or edit
     the Proposed column. When ready, tell Claude: "apply feed report"
  3. Claude runs --apply, reads approved rows, writes them to Shopify.
     Simprosys picks up Shopify changes within ~30 min.

Modes:
  python3 feed_health.py              full audit + propose (default — run weekly)
  python3 feed_health.py --apply      apply approved changes from latest report
  python3 feed_health.py --audit-only audit only, no proposals generated
  python3 feed_health.py --apply --dry-run  preview without writing to Shopify
"""

import os
import re
import sys
import json
import time
import glob
import argparse
from datetime import date
from html.parser import HTMLParser

import requests
import anthropic
from config import OBSIDIAN_BASE, load_env

# Merchant Center imports
from merchant_feed_audit import (
    get_content_service,
    fetch_all_product_statuses,
    classify_statuses,
    summarise_issues,
    issues_for_monday,
    MERCHANT_ID,
    ACCOUNT_NAME as MC_ACCOUNT,
)
from monday_helper import post_audit_issues

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
REPORT_DIR = os.path.join(OBSIDIAN_BASE, ACCOUNT, "Feed Audits")

METALS = {
    "silver", "gold", "rose gold", "platinum", "titanium",
    "copper", "bronze", "rhodium", "vermeil", "diamond",
}
THIN_DESC_CHARS = 150
WEAK_TITLE_SCORE = 70

_TYPE_KEYWORDS = [
    ("ring",     "Rings"),
    ("necklace", "Necklaces"),
    ("pendant",  "Necklaces"),
    ("bracelet", "Bracelets"),
    ("earring",  "Earrings"),
    ("stud",     "Earrings"),
    ("cufflink", "Cufflinks"),
    ("brooch",   "Brooches"),
    ("bangle",   "Bangles"),
    ("anklet",   "Anklets"),
    ("tie pin",  "Tie Pins"),
    ("charm",    "Charms"),
]
_EXCLUDE_FROM_FEED = {"gift card", "ring sizer", "ring sizer gauge"}


# ── Shopify API ────────────────────────────────────────────────────────────────

def _shopify_headers():
    # Try client-specific token first (SHOPIFY_ACCESS_TOKEN_LEE_RENEE),
    # then fall back to generic SHOPIFY_ACCESS_TOKEN for backwards compat.
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN_LEE_RENEE") or os.environ.get("SHOPIFY_ACCESS_TOKEN")
    if not token:
        print("ERROR: SHOPIFY_ACCESS_TOKEN_LEE_RENEE not set. Add it to .env.")
        sys.exit(1)
    return {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}


def fetch_all_shopify_products():
    products = []
    url    = f"{BASE_URL}/products.json"
    params = {
        "limit":  250,
        "status": "active",
        "fields": "id,title,vendor,product_type,body_html,variants,tags",
    }
    headers = _shopify_headers()
    while url:
        resp = requests.get(url, params=params, headers=headers)
        resp.raise_for_status()
        batch = resp.json().get("products", [])
        products.extend(batch)
        link     = resp.headers.get("Link", "")
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = re.search(r"<([^>]+)>", part).group(1)
        url    = next_url
        params = {}
        if next_url:
            time.sleep(0.4)
    return products


def _shopify_update(product_id, payload):
    resp = requests.put(
        f"{BASE_URL}/products/{product_id}.json",
        headers=_shopify_headers(),
        json={"product": payload},
    )
    resp.raise_for_status()
    return resp.json().get("product")


# ── Shopify audit ──────────────────────────────────────────────────────────────

class _StripHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def plain_text(self):
        return " ".join(self._parts).strip()


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
    lower = title.lower()
    if any(e in lower for e in _EXCLUDE_FROM_FEED):
        return None, True
    for keyword, ptype in _TYPE_KEYWORDS:
        if keyword in lower:
            return ptype, False
    return None, False


def audit_shopify(products):
    zero_price   = []
    missing_type = []
    thin_desc    = []
    weak_titles  = []

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

        score = _title_score(p["title"])
        if score < WEAK_TITLE_SCORE:
            weak_titles.append({"product": p, "score": score})

    weak_titles.sort(key=lambda x: x["score"])
    return {
        "zero_price":   zero_price,
        "missing_type": missing_type,
        "thin_desc":    thin_desc,
        "weak_titles":  weak_titles,
    }


# ── Proposals ─────────────────────────────────────────────────────────────────

def _ai_propose_titles(batch):
    client = anthropic.Anthropic()

    items = "\n".join(
        f"{i+1}. ID={p['id']} | {p['title']!r}"
        + (f" | type={p['product_type']!r}" if p.get("product_type") else "")
        + (f" | tags={p['tags']!r}" if p.get("tags") else "")
        for i, p in enumerate(batch)
    )

    prompt = f"""Improve these Google Shopping product titles for {ACCOUNT}.

Title formula: [Metal/Material] [Design Name] [Product Type] – {ACCOUNT.split()[0]}
- Brand goes at the END — small brand, product attributes should lead
- Lead with metal/material: Sterling Silver, 9ct Gold, Rose Gold, Gold Vermeil, etc.
  Default to "Sterling Silver" if metal is ambiguous
- Include design name and product type
- 60–110 characters, UK spelling, no promotional words

Products:
{items}

Return only a JSON array:
[{{"id": <int>, "new_title": "<improved>", "reason": "<one-line>"}}]"""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
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


def propose_fixes(products, shopify_audit):
    """
    Generates AI title proposals and infers product_types.
    Returns (title_proposals, type_proposals).
    """
    candidates = [x["product"] for x in shopify_audit["weak_titles"]]
    title_proposals = []

    if candidates:
        print(f"  Generating title proposals for {len(candidates)} products...")
        improvements = []
        for i in range(0, len(candidates), 20):
            batch = candidates[i:i+20]
            print(f"  Batch {i//20 + 1}/{-(-len(candidates)//20)}...")
            improvements.extend(_ai_propose_titles(batch))
            if i + 20 < len(candidates):
                time.sleep(1)

        # Deduplicate and restrict to actual candidates
        candidate_ids = {p["id"] for p in candidates}
        seen = {}
        for imp in improvements:
            if imp["id"] in candidate_ids:
                seen[imp["id"]] = imp

        product_map = {p["id"]: p for p in candidates}
        for pid, imp in seen.items():
            title  = product_map.get(pid, {}).get("title", "?")
            action = "skip" if any(e in title.lower() for e in _EXCLUDE_FROM_FEED) else "approve"
            title_proposals.append({
                "product_id": pid,
                "current":    title,
                "proposed":   imp["new_title"],
                "reason":     imp.get("reason", ""),
                "action":     action,
            })

    type_proposals = []
    for p in shopify_audit["missing_type"]:
        suggested, exclude = _infer_product_type(p["title"])
        if exclude:
            type_proposals.append({
                "product_id":     p["id"],
                "title":          p["title"],
                "suggested_type": "—",
                "action":         "skip",
                "note":           "exclude from feed",
            })
        elif suggested:
            type_proposals.append({
                "product_id":     p["id"],
                "title":          p["title"],
                "suggested_type": suggested,
                "action":         "approve",
                "note":           "",
            })
        else:
            type_proposals.append({
                "product_id":     p["id"],
                "title":          p["title"],
                "suggested_type": "?",
                "action":         "skip",
                "note":           "could not infer — set manually",
            })

    return title_proposals, type_proposals


# ── Report ─────────────────────────────────────────────────────────────────────

def write_report(
    mc_approved, mc_limited, mc_disapproved, mc_pending, mc_issues,
    mc_ai_text,
    shopify_products, shopify_audit,
    title_proposals=None, type_proposals=None,
):
    os.makedirs(REPORT_DIR, exist_ok=True)
    today = date.today().isoformat()
    path  = os.path.join(REPORT_DIR, f"feed_audit_{today}.md")

    total_mc = len(mc_approved) + len(mc_limited) + len(mc_disapproved) + len(mc_pending)
    approval_pct = len(mc_approved) / total_mc * 100 if total_mc else 0

    has_proposals = title_proposals is not None or type_proposals is not None

    lines = [
        "---",
        f"tags: [feed-health, google-shopping, {ACCOUNT.lower().replace(' ', '-')}]",
        f"date: {today}",
        "---",
        "",
        f"# Feed Health — {ACCOUNT} — {today}",
        "",
    ]

    if has_proposals:
        lines += [
            "> **How to review:** Change `approve` → `skip` to skip a fix, or edit the Proposed column.",
            "> When done, tell Claude: **\"apply feed report\"**",
            "",
        ]

    # ── Merchant Center ──
    lines += [
        "## Merchant Center",
        "",
        f"| Status | Count | % |",
        f"| --- | --- | --- |",
        f"| ✅ Approved | {len(mc_approved):,} | {approval_pct:.1f}% |",
        f"| ⚠️ Limited | {len(mc_limited):,} | {len(mc_limited)/total_mc*100:.1f}% |",
        f"| ❌ Disapproved | {len(mc_disapproved):,} | {len(mc_disapproved)/total_mc*100:.1f}% |",
        f"| 🕐 Pending | {len(mc_pending):,} | {len(mc_pending)/total_mc*100:.1f}% |",
        f"| **Total** | **{total_mc:,}** | |",
        "",
    ]

    if mc_issues:
        lines += [
            "### Issues",
            "",
            "| # | Issue | Affected | Servability | Attribute |",
            "| --- | --- | --- | --- | --- |",
        ]
        for i, s in enumerate(mc_issues[:15], 1):
            lines.append(
                f"| {i} | {s['description']} | {s['count']} "
                f"| {s['servability']} | {s['attribute'] or '—'} |"
            )
        lines.append("")

    if mc_ai_text:
        lines += [
            "### Analysis & Recommendations",
            "",
            mc_ai_text,
            "",
        ]

    # ── Shopify data quality ──
    sa = shopify_audit
    lines += [
        "## Shopify Data Quality",
        "",
        f"**Products audited:** {len(shopify_products):,}",
        "",
        "| Check | Count | Impact |",
        "| --- | --- | --- |",
        f"| Zero-price variants | {len(sa['zero_price'])} | Merchant Center disapproval |",
        f"| Missing product_type | {len(sa['missing_type'])} | Feed misclassification |",
        f"| Thin descriptions (<{THIN_DESC_CHARS} chars) | {len(sa['thin_desc'])} | Lower quality score |",
        f"| Weak Shopping titles | {len(sa['weak_titles'])} | Reduced impression share |",
        "",
    ]

    if sa["zero_price"]:
        lines += [
            "### Zero-Price Variants",
            "",
            "These cause Merchant Center disapprovals — set correct price or mark Unavailable:",
            "",
        ]
        for item in sa["zero_price"]:
            p, v = item["product"], item["variant"]
            lines.append(
                f"- [{p['title']} — {v['title']}]"
                f"(https://{SHOP}/admin/products/{p['id']})"
            )
        lines.append("")

    # ── Proposed fixes ──
    if has_proposals:
        lines += [
            "---",
            "",
            "## Proposed Fixes",
            "",
        ]

    if title_proposals:
        approve_count = sum(1 for t in title_proposals if t["action"] == "approve")
        lines += [
            f"### Title Improvements ({approve_count} to apply, {len(title_proposals)} total)",
            "",
            "> ⚠️ Verify each proposed title matches its current title — AI occasionally swaps IDs.",
            "> The Current Title column is your ground truth.",
            "",
            "| Product ID | Current Title | Proposed Title | Action |",
            "| --- | --- | --- | --- |",
        ]
        for tp in title_proposals:
            lines.append(
                f"| {tp['product_id']} | {tp['current']} | {tp['proposed']} | {tp['action']} |"
            )
        lines.append("")

    if type_proposals:
        fixable = sum(1 for t in type_proposals if t["action"] == "approve")
        lines += [
            f"### Missing product_type ({fixable} to apply, {len(type_proposals)} total)",
            "",
            "| Product ID | Product | Suggested Type | Action | Note |",
            "| --- | --- | --- | --- | --- |",
        ]
        for tp in type_proposals:
            lines.append(
                f"| {tp['product_id']} | {tp['title']} | {tp['suggested_type']} "
                f"| {tp['action']} | {tp['note']} |"
            )
        lines.append("")

    # Fallback audit-only sections (no proposals)
    if not has_proposals:
        if sa["weak_titles"]:
            lines += [
                "### Weak Shopping Titles",
                "",
                "Run without `--audit-only` to generate proposals.",
                "",
                "| Score | Title |",
                "| --- | --- |",
            ]
            for x in sa["weak_titles"][:15]:
                lines.append(f"| {x['score']} | {x['product']['title']} |")
            lines.append("")

        if sa["missing_type"]:
            lines += ["### Missing product_type", ""]
            for p in sa["missing_type"]:
                lines.append(
                    f"- [{p['title']}](https://{SHOP}/admin/products/{p['id']})"
                )
            lines.append("")

    with open(path, "w") as f:
        f.write("\n".join(lines))

    return path


# ── Apply phase ────────────────────────────────────────────────────────────────

def _parse_table(content, header_marker):
    rows = []
    in_table = False
    past_sep  = False
    for line in content.splitlines():
        s = line.strip()
        if not in_table:
            if s.startswith("|") and header_marker in s:
                in_table = True
                past_sep  = False
        else:
            if s.startswith("| ---") or s.startswith("|---"):
                past_sep = True
                continue
            if not s.startswith("|"):
                break
            if past_sep:
                cells = [c.strip() for c in s.split("|")[1:-1]]
                rows.append(cells)
    return rows


def latest_report():
    pattern = os.path.join(REPORT_DIR, "feed_audit_*.md")
    files   = sorted(glob.glob(pattern), reverse=True)
    return files[0] if files else None


def apply_approved(report_path=None, dry_run=False):
    if report_path is None:
        report_path = latest_report()
    if not report_path or not os.path.exists(report_path):
        print("  ERROR: No feed health report found to apply.")
        return []

    print(f"  Reading: {os.path.basename(report_path)}\n")
    with open(report_path) as f:
        content = f.read()

    changes = []

    # Titles
    title_rows    = _parse_table(content, "Current Title")
    approved_titles = [r for r in title_rows if len(r) >= 4 and r[3].lower() == "approve"]

    if approved_titles:
        print(f"  Title changes ({len(approved_titles)}):")
        for row in approved_titles:
            pid, current, proposed = row[0], row[1], row[2]
            try:
                pid_int = int(pid)
            except ValueError:
                print(f"  ⚠️  Bad product ID: {pid!r}")
                continue
            tag = "[DRY RUN] " if dry_run else "✅ "
            print(f"  {tag}{current!r}  →  {proposed!r}")
            if not dry_run:
                _shopify_update(pid_int, {"title": proposed})
                time.sleep(0.3)
            changes.append({"type": "title", "product_id": pid_int, "before": current, "after": proposed})
    else:
        print("  No approved title changes.")

    # product_type
    type_rows     = _parse_table(content, "Suggested Type")
    approved_types = [r for r in type_rows if len(r) >= 4 and r[3].lower() == "approve"]

    if approved_types:
        print(f"\n  product_type changes ({len(approved_types)}):")
        for row in approved_types:
            pid, title, suggested = row[0], row[1], row[2]
            if suggested in ("—", "?", ""):
                continue
            try:
                pid_int = int(pid)
            except ValueError:
                print(f"  ⚠️  Bad product ID: {pid!r}")
                continue
            tag = "[DRY RUN] " if dry_run else "✅ "
            print(f"  {tag}{title!r}  →  product_type={suggested!r}")
            if not dry_run:
                _shopify_update(pid_int, {"product_type": suggested})
                time.sleep(0.3)
            changes.append({"type": "product_type", "product_id": pid_int, "title": title, "value": suggested})
    else:
        print("\n  No approved product_type changes.")

    return changes


# ── Main ───────────────────────────────────────────────────────────────────────

def run_feed_health(propose=True, audit_only=False, apply=False, dry_run=False, post_to_monday=True):
    """
    Main entry point for the unified feed health check.

    Args:
        propose:           generate AI proposals in the report (default True)
        audit_only:        skip proposals, audit only
        apply:             apply approved changes from latest report
        dry_run:           show changes without writing
        post_to_monday: post actionable MC issues to Monday.com (Shopify group)
    """
    sep = "=" * 65
    print(f"\n{sep}")
    print(f"  Feed Health — {ACCOUNT}")
    if dry_run:
        print("  [DRY RUN]")
    print(f"{sep}\n")

    if apply:
        print("  Applying approved changes...\n")
        changes = apply_approved(dry_run=dry_run)
        total = len(changes)
        if total:
            print(f"\n  ✅ {total} change(s) applied.")
            if not dry_run:
                print("  Simprosys will sync to Merchant Center within ~30 min.")
        else:
            print("\n  Nothing to apply — check the report has rows marked 'approve'.")
        print(f"\n{sep}\n")
        return {"changes": changes}

    # ── Merchant Center ──
    print("  Fetching Merchant Center product statuses...")
    mc_service  = get_content_service()
    mc_statuses = fetch_all_product_statuses(mc_service)
    print(f"  {len(mc_statuses):,} product statuses retrieved.")

    mc_approved, mc_limited, mc_disapproved, mc_pending, mc_issues_map = classify_statuses(mc_statuses)
    mc_issues = summarise_issues(mc_issues_map)
    mc_total  = len(mc_approved) + len(mc_limited) + len(mc_disapproved) + len(mc_pending)

    print(f"\n  MC Approved:    {len(mc_approved):,} ({len(mc_approved)/mc_total*100:.1f}%)")
    print(f"  MC Limited:     {len(mc_limited):,}")
    print(f"  MC Disapproved: {len(mc_disapproved):,}")
    print(f"  MC Issues:      {len(mc_issues)} types")

    print("\n  Running AI analysis of Merchant Center issues...")
    mc_ai_text = ""
    if mc_issues:
        from merchant_feed_audit import ai_analysis, fetch_sample_products
        problem_ids    = [s.get("productId") for s in (mc_disapproved + mc_limited)[:30] if s.get("productId")]
        sample_products = fetch_sample_products(mc_service, problem_ids) if problem_ids else {}
        mc_ai_text = ai_analysis(mc_approved, mc_limited, mc_disapproved, mc_pending, mc_issues, sample_products)

    # ── Shopify ──
    print("\n  Fetching Shopify products...")
    shopify_products = fetch_all_shopify_products()
    print(f"  {len(shopify_products):,} products fetched.")

    print("  Auditing Shopify data quality...")
    shopify_audit = audit_shopify(shopify_products)
    print(f"  Weak titles: {len(shopify_audit['weak_titles'])} | "
          f"Missing type: {len(shopify_audit['missing_type'])} | "
          f"Zero-price: {len(shopify_audit['zero_price'])}")

    # ── Proposals ──
    title_proposals = None
    type_proposals  = None

    if propose and not audit_only:
        print()
        title_proposals, type_proposals = propose_fixes(shopify_products, shopify_audit)

    # ── Report ──
    print("\n  Writing Obsidian report...")
    report_path = write_report(
        mc_approved, mc_limited, mc_disapproved, mc_pending, mc_issues,
        mc_ai_text,
        shopify_products, shopify_audit,
        title_proposals, type_proposals,
    )
    print(f"  ✅ {report_path}")

    # ── Monday.com ──
    if post_to_monday:
        monday_issues = issues_for_monday(mc_issues)
        if monday_issues:
            print(f"\n  Posting {len(monday_issues)} issue(s) to Monday.com...")
            try:
                post_audit_issues(MC_ACCOUNT, monday_issues, platform="Shopify",
                                  report_path=str(report_path))
            except Exception as exc:
                print(f"  ⚠️  Monday.com post failed: {exc}")

    if propose and not audit_only:
        print(f"\n  Review the report in Obsidian, then tell Claude:")
        print(f'  "apply feed report"')

    print(f"\n{sep}\n")
    return {
        "report_path":     report_path,
        "mc_approved":     mc_approved,
        "mc_limited":      mc_limited,
        "mc_disapproved":  mc_disapproved,
        "mc_issues":       mc_issues,
        "shopify_audit":   shopify_audit,
        "title_proposals": title_proposals,
        "type_proposals":  type_proposals,
    }


def main():
    parser = argparse.ArgumentParser(description=f"Feed Health — {ACCOUNT}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply",      action="store_true", help="Apply approved changes from latest report")
    mode.add_argument("--audit-only", action="store_true", help="Audit only — no proposals generated")
    parser.add_argument("--dry-run",  action="store_true", help="Preview without writing to Shopify")
    parser.add_argument("--no-monday", action="store_true", help="Skip Monday.com posting")
    args = parser.parse_args()

    run_feed_health(
        propose=not args.audit_only,
        audit_only=args.audit_only,
        apply=args.apply,
        dry_run=args.dry_run,
        post_to_monday=not args.no_monday,
    )


if __name__ == "__main__":
    main()
