"""
client_profile_update.py

Reads the latest audit files for each client and refreshes their Client Profile.md:
- GA4 audit score → Tracking & measurement health row
- Feed audit approval rate → Feed quality health row
- Latest issues → Known Issues section
- Latest open manual steps → Open Tasks section
- Bumps updated: date in frontmatter

Run after any audit, or automatically as the final step of the weekly update.
"""

import os
import re
import sys
import glob
from datetime import date
from config import load_env

load_env()

# ── Client config — loaded from clients.py (gitignored, never committed) ───────
try:
    from clients import CLIENT_CONFIG
except ImportError:
    print("[config] ERROR: clients.py not found.")
    print("         Copy clients.example.py → clients.py and fill in your client details.")
    sys.exit(1)


# ── File helpers ──────────────────────────────────────────────────────────────

def latest_file(directory, pattern="*.md"):
    if not directory or not os.path.isdir(directory):
        return None
    files = sorted(glob.glob(os.path.join(directory, pattern)))
    return files[-1] if files else None


def read_file(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── Parsers ───────────────────────────────────────────────────────────────────

def parse_ga4_score(content):
    """Returns (passed, total, label) from a GA4 audit report, or None."""
    if not content:
        return None
    m = re.search(r"\*\*Score:\*\*\s*(\d+)/(\d+)\s*checks passing", content)
    if m:
        passed, total = int(m.group(1)), int(m.group(2))
        pct = passed / total
        if pct >= 0.9:
            score, status = f"{round(pct * 10)}/10", "Good"
        elif pct >= 0.75:
            score, status = f"{round(pct * 10)}/10", "Mostly good"
        else:
            score, status = f"{round(pct * 10)}/10", "Needs work"
        return score, status, f"GA4 audit {passed}/{total} checks passing"
    return None


def parse_feed_approval(content):
    """Returns (score, status, note) from a feed audit report, or None."""
    if not content:
        return None
    m = re.search(r"Approved[^\d]*(\d+)\s*/\s*(\d+)", content)
    if not m:
        m = re.search(r"(\d+)\s*/\s*(\d+)\s*approved", content, re.IGNORECASE)
    if m:
        approved, total = int(m.group(1)), int(m.group(2))
        if total == 0:
            return None
        pct = approved / total
        if pct >= 0.95:
            score, status = "8/10", "Good"
        elif pct >= 0.85:
            score, status = "6/10", "Needs work"
        else:
            score, status = "4/10", "Poor"
        return score, status, f"{approved}/{total} products approved ({round(pct * 100)}%)"
    return None


def parse_issues(content, section_header="### ❌ Issues found"):
    """Returns list of issue strings from an audit report section."""
    if not content:
        return []
    idx = content.find(section_header)
    if idx == -1:
        return []
    section = content[idx + len(section_header):]
    next_section = re.search(r"\n### ", section)
    if next_section:
        section = section[:next_section.start()]
    issues = []
    for line in section.splitlines():
        line = line.strip()
        if line.startswith("- "):
            text = line[2:].strip()
            text = re.sub(r"\s*\*(API fix available|manual fix required)\*", "", text)
            text = text.strip()
            if text:
                issues.append(text)
    return issues


def parse_open_tasks(content):
    """Returns unchecked manual step items from an audit report."""
    if not content:
        return []
    tasks = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("- [ ]"):
            text = line[5:].strip()
            if text:
                tasks.append(text)
    return tasks


def audit_date_from_filename(path):
    if not path:
        return None
    m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
    return m.group(1) if m else None


# ── Profile updater ───────────────────────────────────────────────────────────

def update_health_row(profile, row_name, new_score, new_status, new_notes):
    """Replace a row in the Account Health table. Matches on the Area column."""
    pattern = rf"(\|\s*{re.escape(row_name)}\s*\|)[^\n]*"
    replacement = f"| {row_name} | {new_score} | {new_status} | {new_notes} |"
    updated, n = re.subn(pattern, replacement, profile)
    return updated, n > 0


def replace_section(profile, section_header, new_items, item_prefix="- "):
    """Replace bullet list content under a ## or ### section heading."""
    pattern = rf"(^{re.escape(section_header)}\n)(.*?)(\n(?=##|\Z))"
    new_content = "\n".join(f"{item_prefix}{item}" for item in new_items) if new_items else "_None at this time._"
    replacement = rf"\g<1>{new_content}\3"
    updated = re.sub(pattern, replacement, profile, flags=re.MULTILINE | re.DOTALL)
    return updated


def bump_updated_date(profile, today_str):
    return re.sub(r"(updated:\s*)[\d-]+", rf"\g<1>{today_str}", profile)


# ── Main ──────────────────────────────────────────────────────────────────────

def update_client_profile(client_name, cfg):
    print(f"\n── {client_name} ────────────────────────────────────────────")

    profile_path = cfg["profile_path"]
    profile = read_file(profile_path)
    if not profile:
        print(f"  ❌  Client Profile not found: {profile_path}")
        return

    today = date.today().isoformat()
    changed = False

    # GA4 audit
    ga4_file = latest_file(cfg["ga4_audit_dir"])
    ga4_content = read_file(ga4_file)
    ga4_date = audit_date_from_filename(ga4_file)

    if ga4_content:
        result = parse_ga4_score(ga4_content)
        if result:
            score, status, notes = result
            if ga4_date:
                notes += f" (audit {ga4_date})"
            profile, hit = update_health_row(profile, "Tracking & measurement", score, status, notes)
            if hit:
                print(f"  ✅  Tracking & measurement → {score} / {status}")
                changed = True

        # Merge GA4 issues
        ga4_issues = parse_issues(ga4_content)
        ga4_tasks  = parse_open_tasks(ga4_content)
    else:
        ga4_issues = []
        ga4_tasks  = []

    # Feed audit
    feed_issues = []
    feed_tasks  = []
    if cfg.get("feed_audit_dir"):
        feed_file    = latest_file(cfg["feed_audit_dir"])
        feed_content = read_file(feed_file)
        feed_date    = audit_date_from_filename(feed_file)
        if feed_content:
            result = parse_feed_approval(feed_content)
            if result:
                score, status, notes = result
                if feed_date:
                    notes += f" (audit {feed_date})"
                profile, hit = update_health_row(profile, "Feed quality", score, status, notes)
                if hit:
                    print(f"  ✅  Feed quality → {score} / {status}")
                    changed = True
            feed_issues = parse_issues(feed_content)
            feed_tasks  = parse_open_tasks(feed_content)

    # Tracking audit
    tracking_file    = latest_file(cfg["tracking_audit_dir"])
    tracking_content = read_file(tracking_file)
    tracking_issues  = parse_issues(tracking_content)
    tracking_tasks   = parse_open_tasks(tracking_content)

    # Merge issues + tasks across all audits (deduplicate)
    all_issues = list(dict.fromkeys(ga4_issues + feed_issues + tracking_issues))
    all_tasks  = list(dict.fromkeys(ga4_tasks  + feed_tasks  + tracking_tasks))

    if all_issues:
        profile = replace_section(profile, "## Known Issues", all_issues)
        print(f"  ✅  Known Issues updated ({len(all_issues)} items)")
        changed = True

    if all_tasks:
        profile = replace_section(profile, "## Open Tasks", all_tasks)
        print(f"  ✅  Open Tasks updated ({len(all_tasks)} items)")
        changed = True

    # Bump updated date
    profile = bump_updated_date(profile, today)
    changed = True

    if changed:
        with open(profile_path, "w", encoding="utf-8") as f:
            f.write(profile)
        print(f"  ✅  Saved: {profile_path}")
    else:
        print(f"  ℹ️   No changes — profile is up to date")


def main():
    print("=" * 60)
    print("  Client Profile Update")
    print("=" * 60)

    for client_name, cfg in CLIENT_CONFIG.items():
        update_client_profile(client_name, cfg)

    print("\n" + "=" * 60)
    print("  ✅  Done. Open Obsidian to review changes.")
    print("=" * 60)


if __name__ == "__main__":
    main()
