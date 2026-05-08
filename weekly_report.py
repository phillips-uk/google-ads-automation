"""
Weekly Google Ads report — single Monday run, one combined note per account.

Combines performance summary, search term audit, and account manager deep dive
into a single Obsidian note: weekly_report_YYYY-MM-DD.md

Produces:
  - Performance xlsx  → <account>/Performance Reports/performance_YYYY-MM-DD.xlsx
  - Search terms xlsx → <account>/Performance Reports/irrelevant_terms_YYYY-MM-DD.xlsx
  - Combined note     → <account>/Performance Reports/weekly_report_YYYY-MM-DD.md
"""

import os
from datetime import date, timedelta

from google.ads.googleads.client import GoogleAdsClient

from config import MCC_ID, OBSIDIAN_BASE, LOOKBACK_DAYS, load_env
from monday_helper import post_weekly_actions
from obsidian_writer import write_weekly_report
from tracking_audit import AUDIT_ACCOUNTS

# Performance data functions
from performance_report import get_daily_performance

# Deep analysis functions
from deep_analysis import (
    get_campaign_overview,
    get_keyword_quality_scores,
    get_ad_copy,
    get_pmax_assets,
    get_device_breakdown,
    get_day_of_week,
    get_ad_group_performance,
    generate_deep_dive,
    PMAX_CHANNEL_TYPE,
    SEARCH_CHANNEL_TYPE,
)

# Search term functions
from search_term_optimiser import (
    get_keywords_by_adgroup,
    get_search_terms,
    _claude_classify,
    _keyword_overlap_classify,
    MIN_SPEND_TO_REVIEW,
    MIN_CLICKS_TO_REVIEW,
)

load_env()


def get_client_accounts(client):
    from google.ads.googleads.errors import GoogleAdsException
    ga_service = client.get_service("GoogleAdsService")
    query = """
        SELECT customer_client.id, customer_client.descriptive_name,
               customer_client.manager, customer_client.status
        FROM customer_client
        WHERE customer_client.level <= 1 AND customer_client.status = 'ENABLED'
    """
    try:
        response = ga_service.search(customer_id=MCC_ID, query=query)
    except GoogleAdsException as ex:
        print(f"  [ERROR] Could not fetch accounts: {ex.failure.errors[0].message}")
        return []
    return [
        {"id": str(row.customer_client.id),
         "name": row.customer_client.descriptive_name or str(row.customer_client.id)}
        for row in response
        if not row.customer_client.manager and row.customer_client.id != int(MCC_ID)
    ]


def main():
    client = GoogleAdsClient.load_from_storage("google-ads.yaml")

    # Performance window: last 7 days vs prior 7 days
    current_end   = date.today() - timedelta(days=1)
    current_start = current_end - timedelta(days=6)
    prev_end      = current_start - timedelta(days=1)
    prev_start    = prev_end - timedelta(days=6)

    # Deep analysis + search term window: last 7 days
    da_from = current_start.strftime("%Y-%m-%d")
    da_to   = current_end.strftime("%Y-%m-%d")

    # Search term window: rolling 30 days
    st_to   = date.today() - timedelta(days=1)
    st_from = st_to - timedelta(days=LOOKBACK_DAYS - 1)
    st_from_str = st_from.strftime("%Y-%m-%d")
    st_to_str   = st_to.strftime("%Y-%m-%d")

    period_str = f"{current_start.strftime('%d %b')} – {current_end.strftime('%d %b %Y')}"
    using_ai   = bool(os.environ.get("ANTHROPIC_API_KEY", ""))

    print(f"\nWeekly Report — {period_str}")
    print(f"  Performance:  {current_start} → {current_end}")
    print(f"  Previous:     {prev_start} → {prev_end}")
    print(f"  Search terms: {st_from_str} → {st_to_str}")
    print("=" * 70)

    accounts = get_client_accounts(client)
    if not accounts:
        print("No client accounts found.")
        return

    for account in accounts:
        name = account["name"]
        cid  = account["id"]
        print(f"\n  [{name}]  ID: {cid}")

        # ── 1. Performance data ───────────────────────────────────────────────
        print("    Pulling performance data (14-day window)...")
        perf_from = prev_start.strftime("%Y-%m-%d")
        perf_to   = current_end.strftime("%Y-%m-%d")
        daily_rows = get_daily_performance(client, cid, perf_from, perf_to)
        if not daily_rows:
            print("    No performance data — skipping account.")
            continue

        # ── 2. Deep analysis data ─────────────────────────────────────────────
        print("    Pulling campaign overview...")
        campaigns_da = get_campaign_overview(client, cid, da_from, da_to)
        has_pmax     = any(c["type"] == PMAX_CHANNEL_TYPE   for c in campaigns_da)
        has_search   = any(c["type"] == SEARCH_CHANNEL_TYPE for c in campaigns_da)
        print(f"    Campaign types — PMax: {has_pmax}, Search: {has_search}")

        print("    Pulling keyword quality scores...")
        keywords = get_keyword_quality_scores(client, cid, da_from, da_to)
        print(f"    Keywords with QS data: {len(keywords)}")

        ad_copy = []
        if has_search:
            print("    Pulling ad copy...")
            ad_copy = get_ad_copy(client, cid, da_from, da_to)
            print(f"    RSAs found: {len(ad_copy)}")

        pmax_assets = {}
        if has_pmax:
            print("    Pulling PMax assets...")
            pmax_assets = get_pmax_assets(client, cid)
            print(f"    Asset groups: {len(pmax_assets)}")

        print("    Pulling device & day-of-week data...")
        devices   = get_device_breakdown(client, cid, da_from, da_to)
        days      = get_day_of_week(client, cid, da_from, da_to)
        ad_groups = get_ad_group_performance(client, cid, da_from, da_to)

        print("    Generating deep dive with Claude Sonnet...")
        deep_data = {
            "campaigns": campaigns_da, "keywords": keywords,
            "ad_copy": ad_copy, "pmax_assets": pmax_assets,
            "devices": devices, "days": days, "ad_groups": ad_groups,
        }
        deep_dive_md = generate_deep_dive(name, has_pmax, has_search, deep_data, period_str)

        # ── 3. Search terms ───────────────────────────────────────────────────
        print("    Pulling search terms...")
        kws_by_ag    = get_keywords_by_adgroup(client, cid)
        search_terms = get_search_terms(client, cid, st_from_str, st_to_str)

        reviewable = [
            r for r in search_terms
            if r["cost"] >= MIN_SPEND_TO_REVIEW or r["clicks"] >= MIN_CLICKS_TO_REVIEW
        ]
        print(f"    Search terms: {len(search_terms)} total, {len(reviewable)} above threshold")

        irrelevant_terms = []
        if reviewable:
            if using_ai:
                print(f"    Classifying {len(reviewable)} terms with Claude...")
                classifications = _claude_classify(name, kws_by_ag, reviewable)
                if classifications is not None:
                    for row in reviewable:
                        result = classifications.get(row["search_term"].lower())
                        if result and not result["relevant"]:
                            irrelevant_terms.append({**row, "reason": result["reason"]})
                else:
                    print("    [WARN] AI classification failed — using keyword overlap fallback")
                    for row in reviewable:
                        is_irrel, reason = _keyword_overlap_classify(kws_by_ag, row["search_term"])
                        if is_irrel:
                            irrelevant_terms.append({**row, "reason": reason})
            else:
                for row in reviewable:
                    is_irrel, reason = _keyword_overlap_classify(kws_by_ag, row["search_term"])
                    if is_irrel:
                        irrelevant_terms.append({**row, "reason": reason})

        wasted = sum(r["cost"] for r in irrelevant_terms)
        print(f"    Irrelevant terms: {len(irrelevant_terms)} (£{wasted:,.2f} wasted spend)")

        # ── 4. Write combined note ────────────────────────────────────────────
        account_config = AUDIT_ACCOUNTS.get(name, {})
        folder_name    = account_config.get("folder")
        print("    Writing combined weekly note...")
        md_path = write_weekly_report(
            base             = OBSIDIAN_BASE,
            account_name     = name,
            daily_rows       = daily_rows,
            current_start    = current_start,
            current_end      = current_end,
            prev_start       = prev_start,
            prev_end         = prev_end,
            irrelevant_terms = irrelevant_terms,
            date_from_str    = st_from_str,
            date_to_str      = st_to_str,
            deep_dive_md     = deep_dive_md,
            folder_name      = folder_name,
        )
        print(f"    → {md_path}")

        # ── Post to Monday.com ────────────────────────────────────────────────
        print("    Posting action items to Monday.com...")
        post_weekly_actions(name, deep_dive_md, irrelevant_terms)

        # ── 5. Feed audit (accounts with a Merchant Center ID) ────────────────
        if account_config.get("merchant_id"):
            print("    Running feed audit (Merchant Center)...")
            try:
                from merchant_feed_audit import run_feed_audit, feed_health_md_block
                feed_result = run_feed_audit(post_to_monday=True)
                feed_block  = feed_health_md_block(
                    feed_result["approved"], feed_result["limited"],
                    feed_result["disapproved"], feed_result["pending"],
                    feed_result["issue_summaries"], feed_result["report_path"],
                )
                # Append feed health block to the weekly note
                with open(md_path, "a") as f:
                    f.write(f"\n\n---\n\n{feed_block}")
                print(f"    Feed health appended to weekly note.")
            except Exception as e:
                print(f"    [WARN] Feed audit skipped: {e}")

    print("\n" + "=" * 70)
    print("Weekly report complete.")


if __name__ == "__main__":
    main()
