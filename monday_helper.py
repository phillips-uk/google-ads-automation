"""
Monday.com helper — create and update action items across client boards.

Board structure (three separate boards, one per client):
  - Phillips.           board: 5096627841
  - Lee Renée Jewellery board: 5096627917
  - Engine House        board: 5096627918

Each board has groups organised by platform:
  To Do, Google Ads, Google Analytics, Shopify (LRJ) / Website (EH),
  Search Console, Meta Ads, Brand

Action items from scripts go to the relevant platform group on the client board.
The "To Do" group (id: topics) is reserved — items placed there by the user are
synced to Apple Calendar/Reminders. Automation must never write to it.

Requires MONDAY_API_TOKEN in .env
Get it: monday.com → avatar → Administration → API → Personal API Token
"""

import os
import re
import json
import requests
from datetime import date, timedelta

MONDAY_API_URL = "https://api.monday.com/v2"

# ── Board + group routing ─────────────────────────────────────────────────────
# (account_name, platform) → (board_id, group_id)
# "To Do" group intentionally excluded — never posted to by automation.

_ROUTE = {
    # ── Phillips. ─────────────────────────────────────────────────────────────
    ("Phillips.", "Google Ads"):       (5096627841, "group_mm3eh3za"),
    ("Phillips.", "Google Analytics"): (5096627841, "group_mm3esptb"),
    ("Phillips.", "Search Console"):   (5096627841, "group_mm3eg1st"),
    ("Phillips.", "Meta Ads"):         (5096627841, "group_mm3ec8zv"),
    ("Phillips.", "Brand"):            (5096627841, "group_mm3ev3q0"),

    # ── Lee Renée Jewellery (both spellings) ──────────────────────────────────
    ("Lee Renée Jewellery", "Google Ads"):       (5096627917, "group_mm3edgsp"),
    ("Lee Renée Jewellery", "Google Analytics"): (5096627917, "group_mm3eb6pk"),
    ("Lee Renée Jewellery", "Shopify"):          (5096627917, "group_mm3egbk2"),
    ("Lee Renée Jewellery", "Search Console"):   (5096627917, "group_mm3efe9m"),
    ("Lee Renée Jewellery", "Meta Ads"):         (5096627917, "group_mm3ed0c6"),
    ("Lee Renée Jewellery", "Brand"):            (5096627917, "group_mm3e4xrg"),
    ("Lee Renee Jewellery", "Google Ads"):       (5096627917, "group_mm3edgsp"),
    ("Lee Renee Jewellery", "Google Analytics"): (5096627917, "group_mm3eb6pk"),
    ("Lee Renee Jewellery", "Shopify"):          (5096627917, "group_mm3egbk2"),
    ("Lee Renee Jewellery", "Search Console"):   (5096627917, "group_mm3efe9m"),
    ("Lee Renee Jewellery", "Meta Ads"):         (5096627917, "group_mm3ed0c6"),
    ("Lee Renee Jewellery", "Brand"):            (5096627917, "group_mm3e4xrg"),

    # ── Engine House ──────────────────────────────────────────────────────────
    ("Engine House", "Google Ads"):       (5096627918, "group_mm3ebjzx"),
    ("Engine House", "Website"):          (5096627918, "group_mm3eewg8"),
    ("Engine House", "Google Analytics"): (5096627918, "group_mm3eeb43"),
    ("Engine House", "Search Console"):   (5096627918, "group_mm3er0wa"),
    ("Engine House", "Meta Ads"):         (5096627918, "group_mm3e61p3"),
    ("Engine House", "Brand"):            (5096627918, "group_mm3ex57a"),
}

# ── Column IDs per board ──────────────────────────────────────────────────────

_COLS = {
    5096627841: {  # Phillips.
        "type":          "dropdown_mm3eqpem",
        "area":          "dropdown_mm3ena2f",
        "platform":      "dropdown_mm3egxge",
        "estimate":      "dropdown_mm3e5sa6",
        "due_date":      "date_mm3eahwr",
        "obsidian_note": "text_mm3evfvc",
        "calendar_id":   "text_mm3emyt7",
        "priority":      "color_mm3eesmy",
    },
    5096627917: {  # Lee Renée Jewellery
        "type":          "dropdown_mm3esfpv",
        "platform":      "dropdown_mm3etbmm",
        "estimate":      "dropdown_mm3earph",
        "due_date":      "date_mm3esgqj",
        "obsidian_note": "text_mm3eww3s",
        "calendar_id":   "text_mm3eks5e",
        "priority":      "color_mm3etfqk",
    },
    5096627918: {  # Engine House
        "type":          "dropdown_mm3ef2yq",
        "platform":      "dropdown_mm3eesvt",
        "estimate":      "dropdown_mm3egex3",
        "due_date":      "date_mm3etrb3",
        "obsidian_note": "text_mm3ebc52",
        "calendar_id":   "text_mm3e2a4w",
        "priority":      "color_mm3emy97",
    },
}

# ── Priority labels ───────────────────────────────────────────────────────────
# Phillips. has: High, Medium, Low  (no Critical)
# Lee Renée / Engine House have: Critical, High, Normal, Low  (no Medium)
_PRIORITY_LABELS = {
    5096627841: {"CRITICAL": "High",     "HIGH": "High",   "MEDIUM": "Medium", "LOW": "Low"},
    5096627917: {"CRITICAL": "Critical", "HIGH": "High",   "MEDIUM": "Normal", "LOW": "Low"},
    5096627918: {"CRITICAL": "Critical", "HIGH": "High",   "MEDIUM": "Normal", "LOW": "Low"},
}

# ── Platform labels (group name → Platform dropdown label) ────────────────────
# Engine House has no "Website" in its Platform column — omit in that case.
_PLATFORM_LABELS = {
    5096627841: {  # Phillips.
        "Google Ads": "Google Ads", "Google Analytics": "Google Analytics",
        "Search Console": "Search Console", "Meta Ads": "Meta Ads",
        "Shopify": "Shopify", "Website": "Website", "Brand": "Brand",
    },
    5096627917: {  # Lee Renée Jewellery
        "Google Ads": "Google Ads", "Google Analytics": "Google Analytics",
        "Shopify": "Shopify", "Search Console": "Search Console",
        "Meta Ads": "Meta Ads", "Website": "Website",
    },
    5096627918: {  # Engine House
        "Google Ads": "Google Ads", "Google Analytics": "Google Analytics",
        "Search Console": "Search Console", "Meta Ads": "Meta Ads",
        "Shopify": "Shopify",
        # "Website" group has no matching Platform column label — omitted
    },
}


def _hours_to_estimate(est_hours, board_id):
    """Map float hours to the Estimate dropdown label for the given board."""
    if board_id == 5096627841:  # Phillips. — labels: 15m, 30m, 1h, 2h, Half Day
        if est_hours <= 0.25: return "15m"
        if est_hours <= 0.5:  return "30m"
        if est_hours <= 1.0:  return "1h"
        if est_hours <= 2.0:  return "2h"
        return "Half Day"
    else:  # Lee Renée / Engine House — labels: 15 min, 30 min, 1h, 2h, 3h, Half day, Full day
        if est_hours <= 0.25: return "15 min"
        if est_hours <= 0.5:  return "30 min"
        if est_hours <= 1.0:  return "1h"
        if est_hours <= 2.0:  return "2h"
        if est_hours <= 3.0:  return "3h"
        if est_hours <= 5.0:  return "Half day"
        return "Full day"

# Fallback due-date offsets (days) used when an issue doesn't match _ISSUE_MAP.
# CRITICAL = same-week fix; HIGH = this sprint; MEDIUM = this month; LOW = backlog.
_FALLBACK_DUE = {"CRITICAL": 3, "HIGH": 10, "MEDIUM": 21, "LOW": 45}

# ── Per-issue intelligence ────────────────────────────────────────────────────
# Each entry: keyword → (title, est_hours, priority, due_days)
#
# priority:  "HIGH" | "MEDIUM" | "LOW"  (mapped to board-specific labels later)
# due_days:  calendar days from today the task should be completed by
# est_hours: realistic time to implement the fix
#
# Rationale for each:
#   CRITICAL tracking gaps (no tag / all paused / zero conversions) → due in 3 days,
#   the account is flying blind and Smart Bidding will degrade within days.
#   Infrastructure gaps (EC, Consent Mode, CMP) → HIGH, 7–14 days, significant but
#   not immediate revenue loss.
#   Attribution / model improvements → MEDIUM, 21 days, improvement not emergency.
#   Feed disapprovals → HIGH, 7 days, products not serving.
#   SEO quick wins → HIGH, 7 days (money left on table); structural SEO → MEDIUM/LOW.

_ISSUE_MAP = [
    # ── Conversion tracking — critical gaps ────────────────────────────────────
    # No tracking whatsoever — Smart Bidding has no signal
    ("no google ads conversion or google tag",
        ("Add Google Ads conversion tag to GTM",        1.0, "HIGH", 3)),
    # Every tag paused — equivalent to no tracking
    ("all google ads tags are paused",
        ("Unpause Google Ads tracking tags",             0.5, "HIGH", 3)),
    # Zero conversions recorded — could be tag issue or genuine account problem
    ("zero conversions",
        ("Investigate zero-conversion campaigns",        1.5, "HIGH", 5)),

    # ── Conversion tracking — infrastructure gaps ──────────────────────────────
    # EC missing — hashed email/phone not being passed; impacts Smart Bidding quality
    ("enhanced conversions not detected",
        ("Set up Enhanced Conversions",                  2.0, "HIGH", 7)),
    ("enhanced conversions",
        ("Set up Enhanced Conversions",                  2.0, "HIGH", 7)),
    # GA4 purchase event missing — ecommerce reporting broken
    ("no ga4 'purchase' event",
        ("Add GA4 purchase event tag",                   1.0, "HIGH", 7)),
    # Orphaned tag — fires on nothing, could be a missed conversion
    ("no firing trigger",
        ("Fix orphaned tag — no trigger set",            0.5, "HIGH", 7)),
    # Conversion action excluded from Smart Bidding — wasted spend
    ("excluded from 'conversions'",
        ("Include conversion action in Smart Bidding",   0.5, "HIGH", 7)),

    # ── Consent & compliance ───────────────────────────────────────────────────
    # No Consent Mode v2 — EU legal requirement + modelled conversions unavailable
    ("consent mode v2 not detected",
        ("Implement Consent Mode v2",                    3.0, "HIGH", 14)),
    # No CMP — Consent Mode cannot function without one
    ("no cmp",
        ("Add Consent Management Platform (CMP)",        4.0, "HIGH", 14)),
    # Tags missing consent settings — may fire without consent in EU
    ("no consent settings configured",
        ("Add consent settings to tracking tags",        0.5, "MEDIUM", 21)),

    # ── Attribution & measurement improvements ────────────────────────────────
    # Last-click gives a distorted view of channel value
    ("last click attribution",
        ("Switch attribution model to Data-Driven",      0.5, "MEDIUM", 21)),
    # Legacy UA tags burn GTM quota and can conflict
    ("universal analytics tags still present",
        ("Remove legacy UA tags from GTM",               0.5, "MEDIUM", 21)),

    # ── Campaign / bidding ────────────────────────────────────────────────────
    # No RSA in ad group — Google will not serve ads competitively
    ("no rsa",
        ("Add RSA to ad group",                          1.0, "HIGH", 7)),
    # High CPA — budget being wasted, needs triage
    ("high cpa",
        ("Investigate high CPA — review bids and audiences", 1.5, "HIGH", 10)),
    # Poor IS — budget or bid cap limiting reach
    ("poor impression share",
        ("Review budget and bid strategy for IS loss",   1.0, "MEDIUM", 14)),
    # Low QS — affects CPC and ad rank
    ("low quality score",
        ("Improve Quality Score — review ad relevance",  2.0, "MEDIUM", 21)),

    # ── Feed / Merchant Center ────────────────────────────────────────────────
    # Disapproved products — not serving at all
    ("disapproved",
        ("Fix disapproved products in Merchant Center",  2.0, "HIGH", 7)),
    # Price mismatch — high-risk disapproval trigger
    ("price mismatch",
        ("Fix price mismatch between site and feed",     1.0, "HIGH", 7)),
    # Missing required attribute — products may be limited or disapproved
    ("missing required attribute",
        ("Add missing required feed attributes",         1.5, "MEDIUM", 14)),

    # ── SEO / Search Console ─────────────────────────────────────────────────
    # Quick wins = high-ranking queries underperforming on CTR — fast ROI
    ("sc quick win",
        ("Optimise content for SC quick-win query",      1.0, "HIGH", 7)),
    # Title/meta rewrite — CTR fix with measurable impact
    ("sc ctr fix",
        ("Rewrite title tag and meta description",       0.5, "HIGH", 10)),
    # Robots.txt blocking — could be hiding pages from Google
    ("robots.txt blocking",
        ("Fix robots.txt — critical paths may be blocked", 0.5, "HIGH", 7)),
    # Technical SEO issue — structural, variable complexity
    ("sc technical",
        ("Fix technical SEO issue",                      3.0, "MEDIUM", 14)),
    # Declining queries — monitor and respond before traffic loss compounds
    ("sc decline watch",
        ("Monitor and respond to declining SC queries",  1.0, "MEDIUM", 21)),
    # Orphan products — not in any collection, invisible to navigation + Sitemap
    ("seo: orphan products",
        ("Add orphan products to collections",           1.0, "MEDIUM", 21)),
    # Duplicate SEO titles — cannibalisation risk
    ("seo: duplicate seo titles",
        ("Fix duplicate SEO titles",                     2.0, "MEDIUM", 21)),
    # Missing meta descriptions — CTR impact on informational queries
    ("seo: missing meta descriptions",
        ("Write meta descriptions for products",         3.0, "MEDIUM", 30)),
    # PageSpeed — conversion rate and Core Web Vitals impact
    ("poor pagespeed",
        ("Improve PageSpeed / Core Web Vitals",          4.0, "MEDIUM", 30)),
    # Thin descriptions — content quality signal
    ("seo: thin descriptions",
        ("Expand thin product descriptions",             4.0, "LOW", 45)),
    # Alt text — accessibility + image search; lower urgency
    ("seo: missing alt text",
        ("Add alt text to product images",               2.0, "LOW", 45)),
]


def _clean_title(text):
    """
    Strip markdown formatting and bracket context from a title string.

    Handles patterns like:
      **[PMax — "Rings" asset group] Remove "Rings by Lee Renee Ltd" headline.**
      → Remove "Rings by Lee Renee Ltd" headline from PMax
    """
    # Remove ** bold markers
    text = text.replace("**", "")
    # Remove [context] prefix at the start (campaign/group context)
    text = re.sub(r"^\s*\[.*?\]\s*", "", text)
    # Remove trailing period / punctuation
    text = text.rstrip(" .,;:")
    # Collapse whitespace
    text = " ".join(text.split())
    # Truncate
    return text[:70].rstrip(" .,")


def _parse_issue(msg):
    """
    Return (title, est_hours, priority, due_days) for a raw issue message.

    Matches against _ISSUE_MAP first (specific, calibrated values).
    Falls back to truncated msg title + sensible defaults.
    """
    lower = msg.lower()
    for keyword, (title, hours, priority, due_days) in _ISSUE_MAP:
        if keyword in lower:
            return title, hours, priority, due_days
    # Fallback: treat as MEDIUM, 1h, 21 days
    return msg[:60].rstrip(" .,"), 1.0, "MEDIUM", 21


# ── API plumbing ──────────────────────────────────────────────────────────────

def _graphql(query, variables=None):
    token = os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        raise RuntimeError(
            "MONDAY_API_TOKEN not set.\n"
            "  1. Go to monday.com → avatar → Administration → API\n"
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


def _get_route(account_name, platform="Google Ads"):
    """Return (board_id, group_id) for the given client + platform, or (None, None)."""
    key = (account_name, platform)
    result = _ROUTE.get(key)
    if not result:
        print(f"  [Monday] No route for ({account_name!r}, {platform!r}) — skipping.")
    return result or (None, None)


def _existing_items(board_id, group_id):
    """Return {item_name: item_id} for all items in the group."""
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
        data = _graphql(q, {"board": str(board_id), "group": group_id})
        groups = data["boards"][0]["groups"]
        if not groups:
            return {}
        return {i["name"]: i["id"] for i in groups[0]["items_page"]["items"]}
    except Exception:
        return {}


def _bump_existing_item(board_id, item_id, title, due_days, description):
    """Bump the due date on an existing item and leave an update note."""
    cols     = _COLS[board_id]
    new_due  = (date.today() + timedelta(days=due_days)).isoformat()
    today_str = date.today().isoformat()

    try:
        _graphql(
            """
            mutation($board: ID!, $item: ID!, $col: String!, $val: JSON!) {
              change_column_value(board_id: $board, item_id: $item, column_id: $col, value: $val) { id }
            }
            """,
            {"board": str(board_id), "item": str(item_id),
             "col": cols["due_date"], "val": json.dumps({"date": new_due})},
        )
    except Exception as e:
        print(f"  [Monday] ⚠️  Could not bump due date for '{title}': {e}")

    body = (
        f"🔁 Still outstanding — re-raised {today_str}. "
        f"Due date pushed to **{new_due}**.\n\n{description}"
    )
    try:
        _graphql(
            "mutation($item: ID!, $body: String!) { create_update(item_id: $item, body: $body) { id } }",
            {"item": str(item_id), "body": body},
        )
    except Exception:
        pass

    print(f"  [Monday] 🔁  Still open: '{title}' — due bumped to {new_due}")


def _create_item(board_id, group_id, title, priority, due_days, description,
                 existing, est_hours=1.0, platform=None, obsidian_note=""):
    """
    Create a new Monday.com item in the specified board/group, or bump the due date
    if an item with the same title already exists.

    Sets: Priority, Due Date, Type (Project), Platform, Estimate, Obsidian Note.
    `existing` is a {name: item_id} dict — updated in place on creation.
    Returns the item URL, or None on failure.
    """
    clean = _clean_title(title)

    if clean in existing:
        item_id = existing[clean]
        _bump_existing_item(board_id, item_id, clean, due_days, description)
        return f"https://lewis-phillips-company.monday.com/boards/{board_id}/pulses/{item_id}"

    cols      = _COLS[board_id]
    p_labels  = _PRIORITY_LABELS.get(board_id, {})
    due_str   = (date.today() + timedelta(days=due_days)).isoformat()
    est_label = _hours_to_estimate(est_hours, board_id)

    col_values = {
        cols["type"]:     {"labels": ["Project"]},
        cols["priority"]: {"label": p_labels.get(priority, "Normal")},
        cols["due_date"]: {"date": due_str},
        cols["estimate"]: {"labels": [est_label]},
    }

    # Platform — set from the group being posted to if label exists on this board
    if platform:
        platform_label = _PLATFORM_LABELS.get(board_id, {}).get(platform)
        if platform_label:
            col_values[cols["platform"]] = {"labels": [platform_label]}

    note = obsidian_note or description
    if note:
        col_values[cols["obsidian_note"]] = note[:500]

    mutation = """
    mutation($board: ID!, $group: String!, $name: String!, $cols: JSON!) {
      create_item(board_id: $board, group_id: $group, item_name: $name, column_values: $cols) {
        id name
      }
    }
    """
    try:
        result  = _graphql(mutation, {
            "board": str(board_id), "group": group_id,
            "name":  clean, "cols": json.dumps(col_values),
        })
        item_id = result["create_item"]["id"]
        url     = f"https://lewis-phillips-company.monday.com/boards/{board_id}/pulses/{item_id}"
        existing[clean] = item_id
        print(f"  [Monday] ✅  '{clean}' → {url}")
        return url
    except Exception as e:
        print(f"  [Monday] ❌  Failed to create '{clean}': {e}")
        return None


# ── Markdown parsing ──────────────────────────────────────────────────────────

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
    items   = re.split(r"\n(?=\d+\.)", section)
    cleaned = []
    for item in items:
        text = re.sub(r"^\d+\.\s*", "", item.strip())
        text = " ".join(text.split())
        if text:
            cleaned.append(text)
    return cleaned


# ── Public API ────────────────────────────────────────────────────────────────

def post_audit_issues(account_name, issues, platform="Google Ads", report_path=""):
    """
    Create Monday.com action items from a list of audit issue dicts.

    Each issue: {"severity": "HIGH", "msg": "..."} or {"severity": ..., "message": ...}
    Priority, due date, and estimate are determined per-issue by _parse_issue —
    the severity field is used only as a fallback for unrecognised issue types.
    platform: which group to post to (default "Google Ads")
    Returns list of created item URLs.
    """
    token = os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        print("  [Monday] MONDAY_API_TOKEN not set — skipping.")
        return []

    board_id, group_id = _get_route(account_name, platform)
    if not board_id:
        return []

    existing = _existing_items(board_id, group_id)
    created  = []

    for issue in issues:
        severity_hint    = issue.get("severity", "MEDIUM").upper()
        msg              = issue.get("msg", issue.get("message", ""))
        if not msg:
            continue

        title, est_hours, priority, due_days = _parse_issue(msg)

        # For unrecognised issues the map returns MEDIUM/21d — let the severity
        # hint from the audit script override that if it's more specific.
        if priority == "MEDIUM" and severity_hint in ("CRITICAL", "HIGH", "LOW"):
            priority = severity_hint
            due_days = _FALLBACK_DUE.get(severity_hint, 21)

        obsidian_note = (msg[:497] + "...") if len(msg) > 500 else msg
        if report_path:
            obsidian_note += f"\n\nReport: {report_path}"

        url = _create_item(board_id, group_id, title, priority, due_days,
                           msg, existing, est_hours=est_hours, platform=platform,
                           obsidian_note=obsidian_note)
        if url:
            created.append(url)

    return created


def post_weekly_actions(account_name, deep_dive_md, irrelevant_terms=None, report_path=""):
    """
    Create Monday.com action items from a weekly deep-dive report.

    Parses 'Priority Actions This Week' numbered list from the markdown.
    If irrelevant_terms is non-empty, adds a 'Review search term negatives' item.
    Posts to the Google Ads group for the account.
    Returns list of created item URLs.
    """
    token = os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        print("  [Monday] MONDAY_API_TOKEN not set — skipping.")
        return []

    board_id, group_id = _get_route(account_name, "Google Ads")
    if not board_id:
        return []

    existing = _existing_items(board_id, group_id)
    created  = []

    actions = _extract_priority_actions(deep_dive_md)

    if (len(actions) == 1
            and "no priority actions" in actions[0].lower()
            and "on track" in actions[0].lower()):
        print(f"  [Monday] No priority actions this week for '{account_name}' — skipping.")
        actions = []

    for idx, action_text in enumerate(actions, start=1):
        # Rank 1–2: top priorities this week — should be done within the week
        # Rank 3–5: important but can follow — two-week window
        # Rank 6+:  lower priority — backlog, 30 days
        if idx <= 2:
            priority, due_days = "HIGH",   7
        elif idx <= 5:
            priority, due_days = "MEDIUM", 14
        else:
            priority, due_days = "LOW",    30

        first_sentence = re.split(r"(?<=[.!?])\s", action_text)[0]
        title          = _clean_title(first_sentence)
        note           = action_text[:500]
        if report_path:
            note += f"\n\nReport: {report_path}"

        url = _create_item(board_id, group_id, title, priority, due_days,
                           action_text, existing, est_hours=1.0,
                           platform="Google Ads", obsidian_note=note)
        if url:
            created.append(url)

    if irrelevant_terms:
        wasted    = sum(r.get("cost", 0) for r in irrelevant_terms)
        neg_title = f"Review search term negatives — £{wasted:,.0f} wasted"
        neg_note  = (
            f"{len(irrelevant_terms)} irrelevant search terms with £{wasted:,.2f} wasted spend. "
            "Review the weekly report and apply as negatives in Google Ads → Search terms."
        )
        if report_path:
            neg_note += f"\n\nReport: {report_path}"
        url = _create_item(board_id, group_id, neg_title, "HIGH", 7,
                           neg_note, existing, est_hours=0.5,
                           platform="Google Ads", obsidian_note=neg_note)
        if url:
            created.append(url)

    return created


def post_sqr_actions(account_name, month_str, negatives_count, negatives_cost,
                     keywords_count, report_path=""):
    """
    Create Monday.com items after an SQR run.

    - 'Apply SQR negatives — <month>' (HIGH, +14 days)
    - 'Add SQR keywords — <month>'   (MEDIUM, +30 days)
    Posts to Google Ads group. Returns list of created item URLs.
    """
    token = os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        print("  [Monday] MONDAY_API_TOKEN not set — skipping.")
        return []

    board_id, group_id = _get_route(account_name, "Google Ads")
    if not board_id:
        return []

    existing = _existing_items(board_id, group_id)
    created  = []

    if negatives_count:
        neg_title = f"Apply SQR negatives — {month_str}"
        neg_desc  = (
            f"{negatives_count} negative keywords identified in {month_str} SQR "
            f"(£{negatives_cost:,.2f} at-risk spend). "
            "Apply via Google Ads → Campaigns → Keywords → Negative keywords, "
            f"or from the Suggested negatives tab in {report_path or 'the SQR report'}."
        )
        url = _create_item(board_id, group_id, neg_title, "HIGH", 14,
                           neg_desc, existing, est_hours=0.5,
                           platform="Google Ads", obsidian_note=neg_desc)
        if url:
            created.append(url)

    if keywords_count:
        kw_title = f"Add SQR keywords — {month_str}"
        kw_desc  = (
            f"{keywords_count} new exact-match keywords identified in {month_str} SQR. "
            "Add via Google Ads → Ad groups → Keywords, mapped to the recommended ad group. "
            f"Full list in the 'Search terms to add' tab of {report_path or 'the SQR report'}."
        )
        url = _create_item(board_id, group_id, kw_title, "MEDIUM", 30,
                           kw_desc, existing, est_hours=1.0,
                           platform="Google Ads", obsidian_note=kw_desc)
        if url:
            created.append(url)

    return created
