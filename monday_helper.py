"""
Monday.com helper — create and update action items on the Google Ads Clients board.

Requires MONDAY_API_TOKEN in .env
Get it: monday.com → profile avatar → Administration → API → Personal API Token
Add to .env: MONDAY_API_TOKEN=your_token_here
"""

import os
import re
import json
import requests
from datetime import date, timedelta

MONDAY_API_URL = "https://api.monday.com/v2"

# Board ID and group IDs — loaded from clients.py (gitignored, never committed)
try:
    from clients import MONDAY_BOARD_ID as BOARD_ID, CLIENT_GROUP_MAP as GROUPS
except ImportError:
    BOARD_ID = None
    GROUPS   = {}  # Monday.com posting will be skipped if not configured

# Column IDs
COL_PRIORITY    = "color_mm2xa2z8"
COL_STATUS      = "color_mm2x56tz"
COL_TIME        = "numeric_mm2xkn5h"
COL_DUE         = "date_mm2xbcqv"
COL_DESCRIPTION = "text_mm2xz0pk"

PRIORITY_LABELS = {
    "CRITICAL": "Critical ⚠️️",
    "HIGH":     "High",
    "MEDIUM":   "Medium",
    "LOW":      "Low",
}

DUE_DAYS = {"CRITICAL": 5, "HIGH": 14, "MEDIUM": 30, "LOW": 60}

# Issue keyword patterns → (action item title, estimated hours)
# Matched in order — first match wins
_TITLE_MAP = [
    ("enhanced conversions not detected",       ("Set up Enhanced Conversions",                  2.0)),
    ("enhanced conversions",                    ("Set up Enhanced Conversions",                  2.0)),
    ("consent mode v2 not detected",            ("Implement Consent Mode v2",                    3.0)),
    ("no cmp",                                  ("Add Consent Management Platform (CMP)",         3.0)),
    ("no google ads conversion or google tag",  ("Add Google Ads conversion tag to GTM",         1.0)),
    ("all google ads tags are paused",          ("Unpause Google Ads tracking tags",             0.5)),
    ("no firing trigger",                       ("Fix orphaned tags with no trigger",            0.5)),
    ("no ga4 'purchase' event",                 ("Add GA4 purchase event tag",                   1.0)),
    ("universal analytics tags still present",  ("Remove legacy UA tags from GTM",               0.5)),
    ("excluded from 'conversions'",             ("Include conversion action in Smart Bidding",   0.5)),
    ("last click attribution",                  ("Update attribution model to Data-Driven",      0.5)),
    ("no consent settings configured",          ("Add consent settings to tracking tags",        0.5)),
    # SEO / Search Console
    ("sc quick win",                             ("Optimise content for SC quick-win query",       1.0)),
    ("sc ctr fix",                               ("Rewrite title tag and meta description",        0.5)),
    ("sc decline watch",                         ("Monitor declining SC queries",                  0.5)),
    ("sc technical",                             ("Fix technical SEO issue",                       2.0)),
    # SEO Audit
    ("seo: orphan products",                     ("Add orphan products to collections",            1.0)),
    ("seo: missing meta descriptions",           ("Write meta descriptions for products",          3.0)),
    ("seo: duplicate seo titles",                ("Fix duplicate SEO titles",                      2.0)),
    ("seo: thin descriptions",                   ("Expand thin product descriptions",              4.0)),
    ("seo: missing alt text",                    ("Add alt text to product images",                2.0)),
    ("robots.txt blocking",                      ("Fix robots.txt blocking critical paths",        0.5)),
    ("poor pagespeed",                           ("Improve PageSpeed / Core Web Vitals",           4.0)),
]


def _parse_issue(msg):
    """Return (short title, estimated hours) from a raw issue message."""
    lower = msg.lower()
    for keyword, (title, hours) in _TITLE_MAP:
        if keyword in lower:
            return title, hours
    # Fallback: first 60 chars of the message
    return msg[:60].rstrip(" .,"), 1.0


def _graphql(query, variables=None):
    token = os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        raise RuntimeError(
            "MONDAY_API_TOKEN not set.\n"
            "  1. Go to monday.com → profile avatar → Administration → API\n"
            "  2. Copy your Personal API Token\n"
            "  3. Add MONDAY_API_TOKEN=<token> to .env"
        )
    resp = requests.post(
        MONDAY_API_URL,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": token, "API-Version": "2024-04"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Monday API error: {data['errors']}")
    return data["data"]


def _existing_items(group_id):
    """Return dict of {item_name: item_id} for all items in the group."""
    q = """
    query($board: ID!, $group: String!) {
      boards(ids: [$board]) {
        groups(ids: [$group]) {
          items_page(limit: 500) { items { id name } }
        }
      }
    }
    """
    try:
        data   = _graphql(q, {"board": str(BOARD_ID), "group": group_id})
        groups = data["boards"][0]["groups"]
        if not groups:
            return {}
        return {i["name"]: i["id"] for i in groups[0]["items_page"]["items"]}
    except Exception:
        return {}


def _bump_existing_item(item_id, title, priority, due_days, description):
    """Bump the due date on an existing item and leave an update note."""
    new_due  = (date.today() + timedelta(days=due_days)).isoformat()
    today_str = date.today().isoformat()

    # Refresh due date
    _graphql(
        """
        mutation($board: ID!, $item: ID!, $col: String!, $val: JSON!) {
          change_column_value(board_id: $board, item_id: $item, column_id: $col, value: $val) { id }
        }
        """,
        {"board": str(BOARD_ID), "item": str(item_id),
         "col": COL_DUE, "val": json.dumps({"date": new_due})},
    )

    # Post an update note so there's a visible audit trail
    body = (
        f"🔁 Still outstanding — re-raised {today_str}. "
        f"Due date pushed to **{new_due}**.\n\n{description}"
    )
    _graphql(
        """mutation($item: ID!, $body: String!) {
             create_update(item_id: $item, body: $body) { id }
           }""",
        {"item": str(item_id), "body": body},
    )
    print(f"  [Monday] 🔁  Still open: '{title}' — due bumped to {new_due}")


def post_audit_issues(account_name, issues):
    """
    Create Monday.com action items for a list of audit issue dicts.

    Each issue: {"severity": "HIGH", "msg": "..."}
    Returns list of created item URLs. Skips items that already exist by name.
    """
    group_id = GROUPS.get(account_name)
    if not group_id:
        print(f"  [Monday] No group mapped for '{account_name}' — skipping.")
        return []

    token = os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        print("  [Monday] MONDAY_API_TOKEN not set — skipping. See monday_helper.py for setup.")
        return []

    existing = _existing_items(group_id)
    created  = []

    for issue in issues:
        severity         = issue.get("severity", "MEDIUM")
        msg              = issue.get("msg", "")
        title, est_hours = _parse_issue(msg)
        url = _create_item(group_id, title, severity, DUE_DAYS.get(severity, 30),
                           est_hours, msg, existing)
        if url:
            created.append(url)

    return created


def _create_item(group_id, title, priority, due_days, est_hours, description, existing):
    """
    Create a new Monday.com item, or bump the due date if it already exists.
    `existing` is a {name: item_id} dict — updated in place on creation.
    Returns the item URL, or None on failure.
    """
    if title in existing:
        item_id = existing[title]
        _bump_existing_item(item_id, title, priority, due_days, description)
        return f"https://lewis-phillips-company.monday.com/boards/{BOARD_ID}/pulses/{item_id}"

    col_values = json.dumps({
        COL_PRIORITY:    {"label": PRIORITY_LABELS.get(priority, "Medium")},
        COL_STATUS:      {"label": "Not Started"},
        COL_TIME:        str(est_hours),
        COL_DUE:         {"date": (date.today() + timedelta(days=due_days)).isoformat()},
        COL_DESCRIPTION: description,
    })
    mutation = """
    mutation($board: ID!, $group: String!, $name: String!, $cols: JSON!) {
      create_item(board_id: $board, group_id: $group, item_name: $name, column_values: $cols) {
        id name
      }
    }
    """
    try:
        result  = _graphql(mutation, {"board": str(BOARD_ID), "group": group_id,
                                      "name": title, "cols": col_values})
        item_id = result["create_item"]["id"]
        url     = f"https://lewis-phillips-company.monday.com/boards/{BOARD_ID}/pulses/{item_id}"
        existing[title] = item_id
        print(f"  [Monday] ✅  '{title}' → {url}")
        return url
    except Exception as e:
        print(f"  [Monday] ❌  Failed to create '{title}': {e}")
        return None


def _extract_priority_actions(deep_dive_md):
    """
    Pull numbered items from the '## Priority Actions This Week' section.
    Returns list of strings, one per action.
    """
    match = re.search(
        r"##\s+Priority Actions This Week\n(.*?)(?=\n##\s|\Z)",
        deep_dive_md, re.DOTALL
    )
    if not match:
        return []
    section = match.group(1).strip()
    # Numbered items: "1. text…" — may span multiple lines until next digit or end
    items = re.split(r"\n(?=\d+\.)", section)
    cleaned = []
    for item in items:
        text = re.sub(r"^\d+\.\s*", "", item.strip())
        text = " ".join(text.split())   # flatten any wrapped lines
        if text:
            cleaned.append(text)
    return cleaned


def post_weekly_actions(account_name, deep_dive_md, irrelevant_terms=None):
    """
    Create Monday.com action items from a weekly deep-dive report.

    - Extracts 'Priority Actions This Week' numbered list → one item per action
    - If irrelevant_terms is non-empty, adds a 'Review search term negatives' item
    Returns list of created item URLs.
    """
    group_id = GROUPS.get(account_name)
    if not group_id:
        print(f"  [Monday] No group mapped for '{account_name}' — skipping.")
        return []

    if not os.environ.get("MONDAY_API_TOKEN", ""):
        print("  [Monday] MONDAY_API_TOKEN not set — skipping.")
        return []

    existing = _existing_items(group_id)
    created  = []

    actions = _extract_priority_actions(deep_dive_md)

    # If the section explicitly states no actions, skip Monday.com posting for it
    if (len(actions) == 1 and
            "no priority actions" in actions[0].lower() and
            "on track" in actions[0].lower()):
        print(f"  [Monday] No priority actions this week for '{account_name}' — skipping action items.")
        actions = []

    for idx, action_text in enumerate(actions, start=1):
        # Priority degrades by rank: 1-2 = HIGH, 3-5 = MEDIUM, 6+ = LOW
        if idx <= 2:
            priority, due_days = "HIGH",   7
        elif idx <= 5:
            priority, due_days = "MEDIUM", 14
        else:
            priority, due_days = "LOW",    30

        # Title: first sentence or first 70 chars, whichever is shorter
        first_sentence = re.split(r"(?<=[.!?])\s", action_text)[0]
        title = first_sentence[:70].rstrip(" .,")

        url = _create_item(group_id, title, priority, due_days, 1.0, action_text, existing)
        if url:
            created.append(url)

    # Wasted spend item
    if irrelevant_terms:
        wasted = sum(r.get("cost", 0) for r in irrelevant_terms)
        neg_title = f"Review search term negatives (£{wasted:,.0f} wasted)"
        url = _create_item(
            group_id, neg_title, "HIGH", 7, 0.5,
            f"{len(irrelevant_terms)} irrelevant search terms identified with £{wasted:,.2f} in "
            "wasted spend. Review the weekly report and apply as negatives in Google Ads → "
            "Campaigns → Search terms.",
            existing,
        )
        if url:
            created.append(url)

    return created


def post_sqr_actions(account_name, month_str, negatives_count, negatives_cost,
                     keywords_count, report_path=""):
    """
    Create Monday.com items after an SQR run.

    - 'Apply SQR negatives — <month>' (HIGH, +7 days)
    - 'Add SQR keywords — <month>' (MEDIUM, +14 days)
    Returns list of created item URLs.
    """
    group_id = GROUPS.get(account_name)
    if not group_id:
        print(f"  [Monday] No group mapped for '{account_name}' — skipping.")
        return []

    if not os.environ.get("MONDAY_API_TOKEN", ""):
        print("  [Monday] MONDAY_API_TOKEN not set — skipping.")
        return []

    existing = _existing_items(group_id)
    created  = []

    if negatives_count:
        neg_title = f"Apply SQR negatives — {month_str}"
        neg_desc  = (
            f"{negatives_count} negative keywords identified in {month_str} SQR "
            f"(£{negatives_cost:,.2f} at-risk spend). "
            "Apply via Google Ads → Campaigns → Keywords → Negative keywords, "
            f"or from the Suggested negatives tab in {report_path or 'the SQR report'}."
        )
        url = _create_item(group_id, neg_title, "HIGH", 7, 0.5, neg_desc, existing)
        if url:
            created.append(url)

    if keywords_count:
        kw_title = f"Add SQR keywords — {month_str}"
        kw_desc  = (
            f"{keywords_count} new exact-match keywords identified in {month_str} SQR. "
            "Add via Google Ads → Ad groups → Keywords, mapped to the recommended ad group. "
            f"Full list in the 'Search terms to add' tab of {report_path or 'the SQR report'}."
        )
        url = _create_item(group_id, kw_title, "MEDIUM", 14, 1.0, kw_desc, existing)
        if url:
            created.append(url)

    return created


def update_item_status(item_id, status_label):
    """Update the Status column of an existing action item."""
    mutation = """
    mutation($board: ID!, $item: ID!, $col: String!, $val: JSON!) {
      change_column_value(board_id: $board, item_id: $item, column_id: $col, value: $val) {
        id
      }
    }
    """
    _graphql(mutation, {
        "board": str(BOARD_ID),
        "item":  str(item_id),
        "col":   COL_STATUS,
        "val":   json.dumps({"label": status_label}),
    })
    print(f"  [Monday] ✅  Item {item_id} status → '{status_label}'")
