"""
Apple Reminders helper — create subtasks under client/platform parent reminders.

Calls the compiled Swift binary at bin/add_reminder_subtask to create subtasks
through the private ReminderKit API (supports proper iCloud-synced subtasks).

Parent reminder UUIDs are the calendarItemIdentifier of the parent "Google Ads",
"Google Analytics" etc. reminders in the Work list.

To find parent UUIDs: check each reminder's calendarItemIdentifier via EventKit.
"""

import os
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

# ── Markdown parsing helpers ───────────────────────────────────────────────────

def _extract_priority_actions(deep_dive_md: str) -> list:
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

# ── Binary path ────────────────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).parent
_BINARY     = _SCRIPT_DIR / "bin" / "add_reminder_subtask"

# ── Parent reminder UUIDs ──────────────────────────────────────────────────────
# Maps (client_name, platform) → calendarItemIdentifier of the parent reminder
# in the Work list. "platform" should match the reminder card name exactly.
PARENT_UUIDS = {
    # Lee Renée Jewellery
    ("Lee Renee Jewellery", "Google Ads"):      "16CDABD8-AF61-4161-B112-5AAAEC0E6538",
    ("Lee Renée Jewellery", "Google Ads"):      "16CDABD8-AF61-4161-B112-5AAAEC0E6538",
    ("Lee Renee Jewellery", "Google Analytics"): "C068AD33-8FCF-4A32-B351-773AEA61E5F3",
    ("Lee Renée Jewellery", "Google Analytics"): "C068AD33-8FCF-4A32-B351-773AEA61E5F3",

    # Engine House
    ("Engine House", "Google Ads"):     "9EF1B110-A37B-4DCB-ACBA-F041043CA6E8",
    ("Engine House", "Shopify"):        "6101CEBC-AB81-4D03-8D11-C1C2D41746D5",

    # Phillips. (internal / audit tool)
    ("Phillips.", "Audit Tool"):        "E352E57F-F21A-4778-BE4B-1B8161FB465B",
    ("Phillips.", "Meta Tool"):         "FAE0F435-ED3B-4B5D-B9DE-0BA37849EB1C",
}

# ── Priority → due-date offset (business days) ────────────────────────────────
DUE_DAYS = {"CRITICAL": 3, "HIGH": 7, "MEDIUM": 21, "LOW": 45}

# ── Issue title parsing ────────────────────────────────────────────────────────
# Matched in order — first match wins
_TITLE_MAP = [
    # Conversion tracking
    ("enhanced conversions not detected",       "Set up Enhanced Conversions"),
    ("enhanced conversions",                    "Set up Enhanced Conversions"),
    ("consent mode v2 not detected",            "Implement Consent Mode v2"),
    ("no cmp",                                  "Add Consent Management Platform (CMP)"),
    ("no google ads conversion or google tag",  "Add Google Ads conversion tag to GTM"),
    ("all google ads tags are paused",          "Unpause Google Ads tracking tags"),
    ("no firing trigger",                       "Fix orphaned tags — no trigger set"),
    ("no ga4 'purchase' event",                 "Add GA4 purchase event tag"),
    ("universal analytics tags still present",  "Remove legacy UA tags from GTM"),
    ("excluded from 'conversions'",             "Include conversion action in Smart Bidding"),
    ("last click attribution",                  "Update attribution model to Data-Driven"),
    ("no consent settings configured",          "Add consent settings to tracking tags"),
    # Campaign / bidding
    ("no rsa",                                  "Add RSA to ad group"),
    ("low quality score",                       "Improve Quality Score"),
    ("poor impression share",                   "Review budget and bid cap for IS loss"),
    ("high cpa",                                "Investigate high CPA — review bids and audiences"),
    ("zero conversions",                        "Investigate zero-conversion campaigns"),
    # Feed
    ("disapproved",                             "Fix disapproved products in Merchant Center"),
    ("price mismatch",                          "Fix price mismatch in feed"),
    ("missing required attribute",              "Add missing feed attributes"),
    # SEO
    ("sc quick win",                            "Optimise content for SC quick-win query"),
    ("sc ctr fix",                              "Rewrite title tag and meta description"),
    ("sc decline watch",                        "Monitor declining SC queries"),
    ("sc technical",                            "Fix technical SEO issue"),
    ("seo: orphan products",                    "Add orphan products to collections"),
    ("seo: missing meta descriptions",          "Write meta descriptions for products"),
    ("seo: duplicate seo titles",               "Fix duplicate SEO titles"),
    ("seo: thin descriptions",                  "Expand thin product descriptions"),
    ("seo: missing alt text",                   "Add alt text to product images"),
    ("robots.txt blocking",                     "Fix robots.txt blocking critical paths"),
    ("poor pagespeed",                          "Improve PageSpeed / Core Web Vitals"),
]


def _parse_issue(msg: str) -> str:
    """Return a short task title from a raw issue message."""
    lower = msg.lower()
    for keyword, title in _TITLE_MAP:
        if keyword in lower:
            return title
    # Fallback: trim to 70 chars
    return msg[:70].rstrip(" .,")


def _next_business_day(start: date, offset_days: int) -> datetime:
    """Return a datetime `offset_days` business days after `start`, at 09:00."""
    d = start
    added = 0
    while added < offset_days:
        d += timedelta(days=1)
        if d.weekday() < 5:  # Mon–Fri
            added += 1
    # If the result falls on a weekend (e.g. start was Friday), push to Monday
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, 9, 0)


def _add_subtask(parent_uuid: str, title: str, notes: str = "", due: datetime = None) -> bool:
    """
    Create a subtask under the given parent reminder UUID.
    Returns True on success.
    """
    if not _BINARY.exists():
        print(f"  [Reminders] Binary not found at {_BINARY} — skipping.")
        return False

    cmd = [str(_BINARY), "--parent", parent_uuid, "--title", title]
    if notes:
        cmd += ["--notes", notes]
    if due:
        cmd += ["--due", due.strftime("%Y-%m-%d %H:%M")]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            new_uuid = result.stdout.strip()
            print(f"  [Reminders] ✅  Created: {title!r} → {new_uuid}")
            return True
        else:
            print(f"  [Reminders] ❌  Failed to create {title!r}: {result.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
        print(f"  [Reminders] ⏱  Timed out creating {title!r}")
        return False
    except Exception as e:
        print(f"  [Reminders] ❌  Error: {e}")
        return False


def _get_parent(account_name: str, platform: str):
    """Return the parent UUID for a client/platform pair, or None."""
    uuid = PARENT_UUIDS.get((account_name, platform))
    if not uuid:
        print(f"  [Reminders] No parent UUID for ({account_name!r}, {platform!r}) — skipping.")
    return uuid


# ── Public API ─────────────────────────────────────────────────────────────────

def post_audit_actions(account_name: str, issues: list, report_path: str = "") -> list:
    """
    Create Reminders subtasks from a tracking audit issues list.

    Each issue dict should have: severity (CRITICAL/HIGH/MEDIUM/LOW), message (str).
    Returns list of created reminder titles.
    """
    parent_uuid = _get_parent(account_name, "Google Ads")
    if not parent_uuid:
        return []

    today = date.today()
    created = []

    for issue in issues:
        severity = issue.get("severity", "MEDIUM").upper()
        message  = issue.get("message", issue.get("msg", ""))
        if not message:
            continue

        title   = _parse_issue(message)
        days    = DUE_DAYS.get(severity, 21)
        due     = _next_business_day(today, days)
        notes   = message[:200]
        if report_path:
            notes += f"\n\nReport: {report_path}"

        if _add_subtask(parent_uuid, title, notes, due):
            created.append(title)

    return created


def post_weekly_report_actions(
    account_name: str,
    priority_actions: list,
    irrelevant_terms: list = None,
    report_path: str = "",
) -> list:
    """
    Create Reminders subtasks from weekly report priority actions.

    priority_actions: list of dicts with keys: title, notes (optional), severity (optional)
    irrelevant_terms: list of search term dicts with cost field (optional)
    Returns list of created reminder titles.
    """
    parent_uuid = _get_parent(account_name, "Google Ads")
    if not parent_uuid:
        return []

    today = date.today()
    created = []

    for action in priority_actions:
        title    = action.get("title", "")[:70]
        notes    = action.get("notes", action.get("description", ""))[:200]
        severity = action.get("severity", "HIGH").upper()
        if not title:
            continue

        if report_path and notes:
            notes += f"\n\nReport: {report_path}"
        elif report_path:
            notes = f"Report: {report_path}"

        days = DUE_DAYS.get(severity, 7)
        due  = _next_business_day(today, days)

        if _add_subtask(parent_uuid, title, notes, due):
            created.append(title)

    # Wasted spend item
    if irrelevant_terms:
        wasted = sum(r.get("cost", 0) for r in irrelevant_terms)
        neg_title = f"Review search term negatives (£{wasted:,.0f} wasted)"
        neg_notes = (
            f"{len(irrelevant_terms)} irrelevant search terms with £{wasted:,.2f} in wasted spend. "
            "Review in the weekly report and apply as negatives in Google Ads → Search terms."
        )
        if report_path:
            neg_notes += f"\n\nReport: {report_path}"
        due = _next_business_day(today, DUE_DAYS["HIGH"])
        if _add_subtask(parent_uuid, neg_title, neg_notes, due):
            created.append(neg_title)

    return created


def post_feed_actions(account_name: str, issues: list, report_path: str = "") -> list:
    """
    Create Reminders subtasks from feed health issues.

    issues: list of dicts with keys: title (str), notes (str), severity (str)
    Returns list of created reminder titles.
    """
    parent_uuid = _get_parent(account_name, "Shopify")
    if not parent_uuid:
        return []

    today = date.today()
    created = []

    for issue in issues:
        title    = issue.get("title", issue.get("message", ""))[:70]
        notes    = issue.get("notes", issue.get("description", ""))[:200]
        severity = issue.get("severity", "MEDIUM").upper()
        if not title:
            continue

        if report_path:
            notes = (notes + f"\n\nReport: {report_path}").strip()

        days = DUE_DAYS.get(severity, 21)
        due  = _next_business_day(today, days)

        if _add_subtask(parent_uuid, title, notes, due):
            created.append(title)

    return created


def post_ga4_actions(account_name: str, issues: list, report_path: str = "") -> list:
    """
    Create Reminders subtasks from GA4 audit issues.

    issues: list of dicts with keys: title (str), notes (str), severity (str)
    Returns list of created reminder titles.
    """
    parent_uuid = _get_parent(account_name, "Google Analytics")
    if not parent_uuid:
        return []

    today = date.today()
    created = []

    for issue in issues:
        title    = issue.get("title", issue.get("message", ""))[:70]
        notes    = issue.get("notes", issue.get("description", ""))[:200]
        severity = issue.get("severity", "MEDIUM").upper()
        if not title:
            continue

        if report_path:
            notes = (notes + f"\n\nReport: {report_path}").strip()

        days = DUE_DAYS.get(severity, 21)
        due  = _next_business_day(today, days)

        if _add_subtask(parent_uuid, title, notes, due):
            created.append(title)

    return created


def post_sqr_actions(
    account_name: str,
    month_str: str,
    negatives_count: int,
    negatives_cost: float,
    keywords_count: int,
    report_path: str = "",
) -> list:
    """Create Reminders subtasks after an SQR run."""
    parent_uuid = _get_parent(account_name, "Google Ads")
    if not parent_uuid:
        return []

    today = date.today()
    created = []

    if negatives_count:
        title = f"Apply SQR negatives — {month_str}"
        notes = (
            f"{negatives_count} negative keywords identified in {month_str} SQR "
            f"(£{negatives_cost:,.2f} at-risk spend). "
            "Apply via Google Ads → Campaigns → Keywords → Negative keywords."
        )
        if report_path:
            notes += f"\n\nReport: {report_path}"
        due = _next_business_day(today, DUE_DAYS["HIGH"])
        if _add_subtask(parent_uuid, title, notes, due):
            created.append(title)

    if keywords_count:
        title = f"Add SQR keywords — {month_str}"
        notes = (
            f"{keywords_count} new exact-match keywords identified in {month_str} SQR. "
            "Add via Google Ads → Ad groups → Keywords."
        )
        if report_path:
            notes += f"\n\nReport: {report_path}"
        due = _next_business_day(today, DUE_DAYS["MEDIUM"])
        if _add_subtask(parent_uuid, title, notes, due):
            created.append(title)

    return created


def add_manual_task(
    account_name: str,
    platform: str,
    title: str,
    notes: str = "",
    severity: str = "MEDIUM",
    due: datetime = None,
) -> bool:
    """
    Manually add a single task under any client/platform parent.
    Use this for one-off tasks from scripts or Claude sessions.
    """
    parent_uuid = _get_parent(account_name, platform)
    if not parent_uuid:
        return False
    if due is None:
        days = DUE_DAYS.get(severity.upper(), 21)
        due  = _next_business_day(date.today(), days)
    return _add_subtask(parent_uuid, title, notes, due)


def post_weekly_actions(
    account_name: str,
    deep_dive_md: str,
    irrelevant_terms: list = None,
    report_path: str = "",
) -> list:
    """
    Drop-in replacement for monday_helper.post_weekly_actions.

    Parses '## Priority Actions This Week' from the deep-dive markdown,
    then calls post_weekly_report_actions.
    """
    actions = _extract_priority_actions(deep_dive_md)

    # Explicit "no actions / on track" marker — skip posting
    if (len(actions) == 1
            and "no priority actions" in actions[0].lower()
            and "on track" in actions[0].lower()):
        print(f"  [Reminders] No priority actions this week for '{account_name}' — skipping.")
        actions = []

    priority_actions = []
    for idx, action_text in enumerate(actions, start=1):
        # Priority degrades by rank: 1–2 = HIGH, 3–5 = MEDIUM, 6+ = LOW
        if idx <= 2:
            severity = "HIGH"
        elif idx <= 5:
            severity = "MEDIUM"
        else:
            severity = "LOW"
        first_sentence = re.split(r"(?<=[.!?])\s", action_text)[0]
        title = first_sentence[:70].rstrip(" .,")
        priority_actions.append({"title": title, "notes": action_text[:200], "severity": severity})

    return post_weekly_report_actions(account_name, priority_actions, irrelevant_terms, report_path)


def post_feed_mc_issues(
    account_name: str,
    mc_issues: list,
    report_path: str = "",
) -> list:
    """
    Create Reminders subtasks from Merchant Center issue summaries.

    Accepts the raw issue_summaries list from merchant_feed_audit (dicts with keys:
    description, count, servability, products, detail, resolution).
    Only surfaces disapproved / demoted items. Posts under the Shopify parent.
    """
    _SERVABILITY_SEVERITY = {
        "disapproved": "CRITICAL",
        "demoted":     "HIGH",
    }
    issues = []
    for s in mc_issues:
        sev = _SERVABILITY_SEVERITY.get(s.get("servability", ""))
        if not sev:
            continue
        examples = ", ".join(s.get("products", [])[:3])
        desc     = s.get("description", "")
        count    = s.get("count", 0)
        detail   = s.get("detail", "")
        res      = s.get("resolution", "")
        title    = f"Feed: {desc} — {count} products"[:70]
        notes    = (
            f"{desc} — {count} products affected"
            + (f" (e.g. {examples})" if examples else "")
            + (f". {detail}" if detail else "")
            + (f" Resolution: {res}." if res else "")
        )[:200]
        issues.append({"title": title, "notes": notes, "severity": sev})

    return post_feed_actions(account_name, issues, report_path)
