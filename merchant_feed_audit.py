"""
Merchant Feed Audit — Merchant Center Content API

What this script does:
  1. Pulls all product statuses from Merchant Center (Content API v2.1)
  2. Classifies products: Approved / Limited / Disapproved / Pending
  3. Groups disapproval and quality issues by type
  4. Uses Claude to analyse patterns and generate prioritised recommendations
  5. Writes a dated Markdown report to Obsidian

First run opens a browser for OAuth. Token cached in merchant_token.json.
Same OAuth client as GTM (gtm_client.json) — scope: auth/content.

Usage:
  python3 merchant_feed_audit.py
"""

import os
import json
from datetime import date
from collections import defaultdict

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import anthropic
from config import OBSIDIAN_BASE, load_env
from monday_helper import post_audit_issues

load_env()

# ── Client config — loaded from clients.py (gitignored, never committed) ───────
try:
    from clients import MERCHANT_ID, MERCHANT_ACCOUNT as ACCOUNT_NAME, MERCHANT_FOLDER as ACCOUNT_FOLDER_NAME
except ImportError:
    print("[config] ERROR: clients.py not found.")
    print("         Copy clients.example.py → clients.py and set MERCHANT_ID, MERCHANT_ACCOUNT, MERCHANT_FOLDER.")
    import sys; sys.exit(1)

from config import get_merchant_token_file
TOKEN_FILE   = get_merchant_token_file(MERCHANT_ACCOUNT)  # resolves merchant_token_<slug>.json
CLIENT_FILE  = os.path.join(os.path.dirname(__file__), "gtm_client.json")
SCOPES       = ["https://www.googleapis.com/auth/content"]

REPORT_DIR = os.path.join(
    OBSIDIAN_BASE, ACCOUNT_FOLDER_NAME, "Feed Audits"
)


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_content_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("content", "v2.1", credentials=creds)


# ── Data fetch ────────────────────────────────────────────────────────────────

def fetch_all_product_statuses(service):
    """Paginate through all product statuses. Returns list of status dicts."""
    statuses = []
    request  = service.productstatuses().list(
        merchantId=MERCHANT_ID,
        maxResults=250,
    )
    while request is not None:
        response = request.execute()
        statuses.extend(response.get("resources", []))
        request = service.productstatuses().list_next(request, response)
    return statuses


def fetch_sample_products(service, product_ids, max_items=50):
    """Fetch full product data for a sample of product IDs."""
    results = {}
    for pid in product_ids[:max_items]:
        try:
            p = service.products().get(merchantId=MERCHANT_ID, productId=pid).execute()
            results[pid] = p
        except HttpError:
            pass
    return results


# ── Classification ────────────────────────────────────────────────────────────

def classify_statuses(statuses):
    """
    Returns:
      approved   — list of status dicts
      limited    — list of status dicts
      disapproved— list of status dicts
      pending    — list of status dicts
      issues_map — {issue_code: [status, ...]}  (across limited + disapproved)
    """
    approved    = []
    limited     = []
    disapproved = []
    pending     = []
    issues_map  = defaultdict(list)

    for s in statuses:
        dest_statuses = s.get("destinationStatuses", [])
        # Determine worst status across all destinations
        # Content API uses 'status' field (not 'approvalStatus')
        all_statuses = {d.get("status", "pending") for d in dest_statuses}
        if "disapproved" in all_statuses:
            disapproved.append(s)
        elif "limited" in all_statuses:
            limited.append(s)
        elif "approved" in all_statuses:
            approved.append(s)
        else:
            pending.append(s)

        # Index issues
        for issue in s.get("itemLevelIssues", []):
            code = issue.get("code", "unknown")
            issues_map[code].append({
                "productId": s.get("productId"),
                "title":     s.get("title", ""),
                "issue":     issue,
            })

    return approved, limited, disapproved, pending, issues_map


def summarise_issues(issues_map):
    """
    Returns list of issue summary dicts sorted by count desc:
      {code, count, servability, description, resolution, attribute, products}
    """
    summaries = []
    for code, items in issues_map.items():
        first = items[0]["issue"]
        summaries.append({
            "code":        code,
            "count":       len(items),
            "servability": first.get("servability", ""),
            "description": first.get("description", code),
            "resolution":  first.get("resolution", ""),
            "attribute":   first.get("attributeName", ""),
            "detail":      first.get("detail", ""),
            "products":    [i["title"] for i in items[:5]],
        })
    summaries.sort(key=lambda x: x["count"], reverse=True)
    return summaries


# ── AI analysis ───────────────────────────────────────────────────────────────

def ai_analysis(approved, limited, disapproved, pending, issue_summaries, sample_products):
    """Use Claude to generate feed health analysis and recommendations."""
    client = anthropic.Anthropic()

    total = len(approved) + len(limited) + len(disapproved) + len(pending)

    # Build a compact context for Claude
    issue_text = "\n".join(
        f"- [{s['servability'].upper()}] {s['description']} — {s['count']} products"
        + (f" (attribute: {s['attribute']})" if s['attribute'] else "")
        + (f"\n  Detail: {s['detail']}" if s['detail'] else "")
        + (f"\n  Resolution: {s['resolution']}" if s['resolution'] else "")
        + (f"\n  Examples: {', '.join(s['products'][:3])}" if s['products'] else "")
        for s in issue_summaries[:20]
    )

    sample_text = ""
    if sample_products:
        sample_items = list(sample_products.values())[:5]
        sample_text = "\n\nSample product titles from disapproved/limited items:\n" + "\n".join(
            f"- {p.get('title', 'no title')} | GTIN: {p.get('gtin', 'missing')} | Brand: {p.get('brand', 'missing')}"
            for p in sample_items
        )

    prompt = f"""You are a Google Shopping feed specialist auditing a jewellery brand's Merchant Center feed.

Feed summary for {ACCOUNT_NAME}:
- Total products: {total:,}
- Approved: {len(approved):,}
- Limited: {len(limited):,}
- Disapproved: {len(disapproved):,}
- Pending: {len(pending):,}

Top issues found:
{issue_text}
{sample_text}

Provide a concise feed health analysis with:
1. Overall assessment (2-3 sentences)
2. Priority fixes — the specific issues to resolve first and exactly how to fix them in Shopify/Simprosys
3. Optimisation opportunities — title/description/attribute improvements specific to fine jewellery
4. Expected impact of fixes (approval rate improvement, impression share)

Be specific and actionable. This is a Shopify store using Simprosys for feed management.
Format as clean Markdown with ## headings."""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ── Report writing ────────────────────────────────────────────────────────────

def write_report(approved, limited, disapproved, pending, issue_summaries, ai_text):
    os.makedirs(REPORT_DIR, exist_ok=True)
    today      = date.today().isoformat()
    report_path = os.path.join(REPORT_DIR, f"feed_audit_{today}.md")
    total = len(approved) + len(limited) + len(disapproved) + len(pending)

    lines = [
        f"---",
        f"tags: [feed-audit, google-shopping, {ACCOUNT_NAME.lower().replace(' ', '-')}]",
        f"date: {today}",
        f"merchant_id: {MERCHANT_ID}",
        f"---",
        f"",
        f"# Feed Audit — {ACCOUNT_NAME} — {today}",
        f"",
        f"## Product Status Overview",
        f"",
        f"| Status | Count | % |",
        f"| --- | --- | --- |",
        f"| ✅ Approved | {len(approved):,} | {len(approved)/total*100:.1f}% |",
        f"| ⚠️ Limited | {len(limited):,} | {len(limited)/total*100:.1f}% |",
        f"| ❌ Disapproved | {len(disapproved):,} | {len(disapproved)/total*100:.1f}% |",
        f"| 🕐 Pending | {len(pending):,} | {len(pending)/total*100:.1f}% |",
        f"| **Total** | **{total:,}** | |",
        f"",
        f"---",
        f"",
        f"## Issues Breakdown",
        f"",
        f"Issues ranked by product count affected. Servability: **disapprove** = item not shown, **demoted** = reduced visibility.",
        f"",
        f"| # | Issue | Affected | Servability | Attribute |",
        f"| --- | --- | --- | --- | --- |",
    ]

    for i, s in enumerate(issue_summaries[:25], 1):
        lines.append(
            f"| {i} | {s['description']} | {s['count']} | {s['servability']} | {s['attribute'] or '—'} |"
        )

    lines += [
        f"",
        f"---",
        f"",
        f"## AI Analysis & Recommendations",
        f"",
        ai_text,
        f"",
        f"---",
        f"",
        f"## Issue Detail",
        f"",
    ]

    for s in issue_summaries[:15]:
        lines += [
            f"### {s['description']} ({s['count']} products)",
            f"",
            f"- **Code**: `{s['code']}`",
            f"- **Servability**: {s['servability']}",
            f"- **Attribute**: {s['attribute'] or '—'}",
            f"- **Resolution**: {s['resolution'] or '—'}",
        ]
        if s['detail']:
            lines.append(f"- **Detail**: {s['detail']}")
        if s['products']:
            lines.append(f"- **Examples**: {', '.join(s['products'][:5])}")
        lines.append("")

    content = "\n".join(lines)
    with open(report_path, "w") as f:
        f.write(content)

    return report_path


# ── Monday.com helpers ────────────────────────────────────────────────────────

_SERVABILITY_SEVERITY = {
    "disapproved": "HIGH",
    "demoted":     "MEDIUM",
}

def issues_for_monday(issue_summaries):
    """
    Convert issue summaries into Monday.com-compatible issue dicts.
    Only surfaces issues with real servability impact (disapproved / demoted).
    One item per issue type (not per product).
    """
    result = []
    for s in issue_summaries:
        sev = _SERVABILITY_SEVERITY.get(s["servability"])
        if not sev:
            continue
        examples = ", ".join(s["products"][:3]) if s["products"] else ""
        msg = (
            f"Feed issue: {s['description']} — {s['count']} products affected"
            + (f" (e.g. {examples})" if examples else "")
            + (f". {s['detail']}" if s["detail"] else "")
            + (f" Resolution: {s['resolution']}." if s["resolution"] else "")
        )
        result.append({"severity": sev, "msg": msg})
    return result


def feed_health_md_block(approved, limited, disapproved, pending, issue_summaries, report_path):
    """Compact Markdown block for embedding in the weekly report."""
    total = len(approved) + len(limited) + len(disapproved) + len(pending)
    approval_pct = len(approved) / total * 100 if total else 0

    actionable = [s for s in issue_summaries if s["servability"] in ("disapproved", "demoted")]
    top_issues = "\n".join(
        f"  - [{s['servability'].upper()}] {s['description']} — {s['count']} products"
        for s in actionable[:5]
    ) or "  - No actionable issues found."

    return (
        f"## Feed Health — {ACCOUNT_NAME}\n\n"
        f"| Status | Count | % |\n"
        f"| --- | --- | --- |\n"
        f"| ✅ Approved | {len(approved):,} | {approval_pct:.1f}% |\n"
        f"| ❌ Disapproved | {len(disapproved):,} | {len(disapproved)/total*100:.1f}% |\n"
        f"| ⚠️ Limited | {len(limited):,} | {len(limited)/total*100:.1f}% |\n\n"
        f"**Actionable issues:**\n{top_issues}\n\n"
        f"→ [Full feed audit report]({report_path})\n"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def run_feed_audit(post_to_monday=False):
    """
    Run the full feed audit.

    Args:
        post_to_monday: if True, post actionable issues to Monday.com
                        (deduplication handled — existing items get due date bumped).

    Returns:
        dict with keys: report_path, approved, limited, disapproved, pending,
                        issue_summaries, ai_text, monday_issues
    """
    print("\n" + "=" * 65)
    print(f"  Merchant Feed Audit — {ACCOUNT_NAME}")
    print("=" * 65 + "\n")

    print("  Authenticating with Merchant Center...")
    service = get_content_service()

    print("  Fetching product statuses (this may take a moment)...")
    statuses = fetch_all_product_statuses(service)
    print(f"  Retrieved {len(statuses):,} product statuses.")

    print("  Classifying products...")
    approved, limited, disapproved, pending, issues_map = classify_statuses(statuses)
    issue_summaries = summarise_issues(issues_map)

    total = len(approved) + len(limited) + len(disapproved) + len(pending)
    print(f"\n  Approved:    {len(approved):,} ({len(approved)/total*100:.1f}%)")
    print(f"  Limited:     {len(limited):,} ({len(limited)/total*100:.1f}%)")
    print(f"  Disapproved: {len(disapproved):,} ({len(disapproved)/total*100:.1f}%)")
    print(f"  Pending:     {len(pending):,} ({len(pending)/total*100:.1f}%)")

    problem_ids = [
        s.get("productId") for s in (disapproved + limited)[:30]
        if s.get("productId")
    ]
    sample_products = {}
    if problem_ids:
        print(f"\n  Fetching product details for {len(problem_ids)} items...")
        sample_products = fetch_sample_products(service, problem_ids)

    print("\n  Generating AI analysis...")
    ai_text = ai_analysis(approved, limited, disapproved, pending, issue_summaries, sample_products)

    print("\n  Writing report to Obsidian...")
    report_path = write_report(approved, limited, disapproved, pending, issue_summaries, ai_text)
    print(f"  ✅ Report written: {report_path}")

    monday_issues = issues_for_monday(issue_summaries)

    if post_to_monday and monday_issues:
        print(f"\n  Posting {len(monday_issues)} feed issue(s) to Monday.com...")
        post_audit_issues(ACCOUNT_NAME, monday_issues)

    print("\n" + "=" * 65 + "\n")
    return {
        "report_path":    report_path,
        "approved":       approved,
        "limited":        limited,
        "disapproved":    disapproved,
        "pending":        pending,
        "issue_summaries": issue_summaries,
        "ai_text":        ai_text,
        "monday_issues":  monday_issues,
    }


def main():
    run_feed_audit(post_to_monday=True)


if __name__ == "__main__":
    main()
