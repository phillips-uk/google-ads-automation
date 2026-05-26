"""
Keyword Rank Tracker

Tracks specific target keywords for defined pages against Google Search Console data.
Designed for owned properties (not client sites) — Phillips portfolio, personal projects.

For each tracked page + keyword combo, pulls:
  - Average position (last 7 days, accounting for SC's ~3-day lag)
  - Impressions
  - Clicks
  - CTR

Writes a weekly snapshot to a running Obsidian Markdown file.
Each run appends a new dated row — the table grows over time and shows movement.

Usage:
  python3 rank_tracker.py                        # run all configured trackers
  python3 rank_tracker.py --config phillips      # run a specific config by key
  python3 rank_tracker.py --dry-run              # print data without writing to Obsidian
"""

import os
import sys
import argparse
from datetime import date, timedelta
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import load_env

load_env()

# ── Vault root (one level up from OBSIDIAN_BASE which points to the clients folder) ──
_CLIENTS_BASE = os.environ.get("OBSIDIAN_BASE", "")
_VAULT_ROOT   = str(Path(_CLIENTS_BASE).parent) if _CLIENTS_BASE else os.path.expanduser(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/My Brain"
)

# ── Auth (reuses seo_search_console.py OAuth setup) ──────────────────────────
_DIR = os.path.dirname(__file__)
_SC_TOKEN_FILE  = os.path.join(_DIR, "sc_token.json")
_SC_CLIENT_FILE = os.path.join(_DIR, "gtm_client.json")
_SC_SCOPES      = ["https://www.googleapis.com/auth/webmasters.readonly"]


def get_sc_service():
    import json
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    with open(_SC_TOKEN_FILE) as f:
        token_data = json.load(f)
    with open(_SC_CLIENT_FILE) as f:
        client_raw = json.load(f)
    client = client_raw.get("installed") or client_raw.get("web") or client_raw

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data["refresh_token"],
        client_id=client["client_id"],
        client_secret=client["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=_SC_SCOPES,
    )
    if not creds.valid:
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(_SC_TOKEN_FILE, "w") as f:
            json.dump(token_data, f, indent=2)
    return build("webmasters", "v3", credentials=creds)


# ── Date range (7 days, offset 3 days for SC lag) ────────────────────────────

def _date_range():
    today = date.today()
    end   = today - timedelta(days=3)
    start = end - timedelta(days=6)
    return start.isoformat(), end.isoformat()


# ── Fetch position for a specific query + page ───────────────────────────────

def fetch_keyword_data(service, site_url, start_date, end_date, keyword, page_url=None):
    """
    Fetch GSC data for a specific keyword.
    If page_url is set, filter to that page only (query + page dimensions).
    Returns dict with position, impressions, clicks, ctr — or None if no data.
    """
    dims = ["query", "page"] if page_url else ["query"]
    body = {
        "startDate":  start_date,
        "endDate":    end_date,
        "dimensions": dims,
        "rowLimit":   5000,
        "dimensionFilterGroups": [
            {
                "filters": [
                    {"dimension": "query", "operator": "equals", "expression": keyword}
                ]
            }
        ],
    }
    if page_url:
        body["dimensionFilterGroups"][0]["filters"].append(
            {"dimension": "page", "operator": "equals", "expression": page_url}
        )

    try:
        resp = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        rows = resp.get("rows", [])
        if not rows:
            return None
        row = rows[0]
        return {
            "position":    round(float(row.get("position", 0)), 1),
            "impressions": int(row.get("impressions", 0)),
            "clicks":      int(row.get("clicks", 0)),
            "ctr":         round(float(row.get("ctr", 0)) * 100, 1),
        }
    except HttpError as e:
        print(f"  [SC API error] keyword='{keyword}': {e}")
        return None


# ── Obsidian report: append a row to a running table ─────────────────────────

def _obsidian_path(config):
    return os.path.join(_VAULT_ROOT, config["obsidian_path"])


def _section_header(page_label):
    return f"## {page_label}"


def _table_header():
    return (
        "| Date | Keyword | Position | Impressions | Clicks | CTR |\n"
        "| --- | --- | --- | --- | --- | --- |"
    )


def append_to_report(config, snapshot_rows, start_date, end_date, dry_run=False):
    """
    Append snapshot rows to the Obsidian tracking file.
    Each page gets its own section. If the section doesn't exist, it's created.
    The table header is written once per section; subsequent runs append rows.
    """
    path = _obsidian_path(config)
    today_str = date.today().isoformat()
    period_str = f"{start_date} → {end_date}"

    # Load existing content
    if os.path.exists(path):
        with open(path) as f:
            content = f.read()
    else:
        # Create the file with a frontmatter header
        content = (
            "---\n"
            "tags: [seo, rank-tracking, phillips]\n"
            f"date: {today_str}\n"
            "---\n\n"
            f"# Rank Tracking — {config['name']}\n\n"
            "Tracking keyword positions from Google Search Console. "
            "Each section is a tracked page. Rows append weekly.\n\n"
        )

    # Group rows by page label
    from collections import defaultdict
    by_page = defaultdict(list)
    for row in snapshot_rows:
        by_page[row["page_label"]].append(row)

    new_content = content.rstrip("\n")

    for page_label, rows in by_page.items():
        section_header = _section_header(page_label)

        if section_header not in new_content:
            # Add new section
            new_content += f"\n\n{section_header}\n\n"
            new_content += f"*Period: {period_str}*\n\n"
            new_content += _table_header() + "\n"
        else:
            # Section exists — just append rows (table header already there)
            pass

        for row in rows:
            pos_str   = str(row["position"]) if row["data"] else "—"
            imps_str  = str(row["data"]["impressions"]) if row["data"] else "—"
            clicks_str = str(row["data"]["clicks"]) if row["data"] else "—"
            ctr_str   = f"{row['data']['ctr']}%" if row["data"] else "—"

            table_row = (
                f"| {today_str} | {row['keyword']} | {pos_str} | "
                f"{imps_str} | {clicks_str} | {ctr_str} |"
            )

            # If section exists in original, append after last row in that section
            if section_header in new_content and section_header in content:
                # Find the section and append to it
                lines = new_content.split("\n")
                section_idx = None
                last_row_idx = None
                in_section = False
                for i, line in enumerate(lines):
                    if line.strip() == section_header:
                        in_section = True
                        section_idx = i
                    elif in_section and line.startswith("## "):
                        in_section = False
                    elif in_section and line.startswith("| "):
                        last_row_idx = i

                insert_idx = last_row_idx + 1 if last_row_idx else (section_idx + 4)
                lines.insert(insert_idx, table_row)
                new_content = "\n".join(lines)
            else:
                # New section — just append
                new_content += table_row + "\n"

    if dry_run:
        print("\n[DRY RUN] Would write to:", path)
        print("-" * 60)
        print(new_content[-2000:])  # show last 2000 chars
        return

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(new_content + "\n")
    print(f"  ✅ Updated: {path}")


# ── Config ────────────────────────────────────────────────────────────────────

try:
    from clients import RANK_TRACKER_CONFIGS
except ImportError:
    print("[config] RANK_TRACKER_CONFIGS not found in clients.py.")
    sys.exit(1)


# ── Main ──────────────────────────────────────────────────────────────────────

def run_tracker(config_key=None, dry_run=False):
    configs = RANK_TRACKER_CONFIGS
    if config_key:
        if config_key not in configs:
            print(f"[ERROR] '{config_key}' not found in RANK_TRACKER_CONFIGS.")
            sys.exit(1)
        configs = {config_key: configs[config_key]}

    svc = get_sc_service()
    start_date, end_date = _date_range()
    print(f"\nRank Tracker — period: {start_date} → {end_date}\n")

    for key, config in configs.items():
        print("=" * 60)
        print(f"  {config['name']}")
        print("=" * 60)

        snapshot_rows = []

        for page in config["pages"]:
            page_label = page["label"]
            page_url   = page.get("url")  # None = all pages
            keywords   = page["keywords"]

            print(f"\n  Page: {page_label}")
            print(f"  URL:  {page_url or '(all pages)'}")
            print(f"  {'Keyword':<45} {'Position':>10} {'Imps':>8} {'Clicks':>8} {'CTR':>8}")
            print(f"  {'-'*45} {'-'*10} {'-'*8} {'-'*8} {'-'*8}")

            for keyword in keywords:
                data = fetch_keyword_data(
                    svc,
                    config["site_url"],
                    start_date,
                    end_date,
                    keyword,
                    page_url,
                )

                if data:
                    print(
                        f"  {keyword:<45} {data['position']:>10} "
                        f"{data['impressions']:>8,} {data['clicks']:>8} {data['ctr']:>7}%"
                    )
                else:
                    print(f"  {keyword:<45} {'—':>10} {'—':>8} {'—':>8} {'—':>8}")

                snapshot_rows.append({
                    "page_label": page_label,
                    "keyword":    keyword,
                    "data":       data,
                    "position":   data["position"] if data else None,
                })

        append_to_report(config, snapshot_rows, start_date, end_date, dry_run=dry_run)

    print("\n" + "=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Keyword rank tracker — GSC")
    parser.add_argument("--config", help="Run a specific config key from RANK_TRACKER_CONFIGS")
    parser.add_argument("--dry-run", action="store_true", help="Print output without writing to Obsidian")
    args = parser.parse_args()
    run_tracker(config_key=args.config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
