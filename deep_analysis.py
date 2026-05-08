"""
Monday deep-dive analysis — pulls expanded metrics per account and writes
a comprehensive account manager brief to Obsidian.

Runs at 7:30am every Monday (before performance_report.py at 8:00am).
Data pulled: impression share, keyword QS, ad copy, PMax asset content,
             device breakdown, day-of-week breakdown, keyword performance.
Claude Sonnet synthesises everything into structured recommendations.
"""

import os
import json
from collections import defaultdict
from datetime import date, timedelta

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from config import MCC_ID, OBSIDIAN_BASE, load_env

load_env()

PMAX_CHANNEL_TYPE   = 10
SEARCH_CHANNEL_TYPE = 2

DEVICE_NAMES = {2: "Mobile", 3: "Tablet", 4: "Desktop", 5: "Connected TV"}
DAY_NAMES    = {2: "Mon", 3: "Tue", 4: "Wed", 5: "Thu", 6: "Fri", 7: "Sat", 8: "Sun"}

# AssetFieldType values used by PMax
ASSET_FIELD_NAMES = {
    5: "Headline", 6: "Description", 8: "Long Headline",
    22: "Business Name", 23: "Square Image", 24: "Marketing Image",
    26: "Video", 27: "Call To Action",
}

QS_COMPONENT_LABELS = {2: "Below Average", 3: "Average", 4: "Above Average"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe(fn, fallback=None):
    try:
        return fn()
    except Exception:
        return fallback


def _search(client, customer_id, query, label=""):
    ga = client.get_service("GoogleAdsService")
    try:
        return list(ga.search(customer_id=customer_id, query=query))
    except GoogleAdsException as ex:
        msg = ex.failure.errors[0].message if ex.failure.errors else str(ex)
        print(f"    [SKIP {label}] {msg[:120]}")
        return []


# ── Data fetchers ─────────────────────────────────────────────────────────────

def get_client_accounts(client):
    rows = _search(client, MCC_ID, """
        SELECT customer_client.id, customer_client.descriptive_name,
               customer_client.manager, customer_client.status
        FROM customer_client
        WHERE customer_client.level <= 1 AND customer_client.status = 'ENABLED'
    """, "accounts")
    return [
        {"id": str(r.customer_client.id),
         "name": r.customer_client.descriptive_name or str(r.customer_client.id)}
        for r in rows
        if not r.customer_client.manager and r.customer_client.id != int(MCC_ID)
    ]


def get_campaign_overview(client, customer_id, date_from, date_to):
    """Returns list of dicts with campaign name, type, IS metrics."""
    rows = _search(client, customer_id, f"""
        SELECT
            campaign.name,
            campaign.advertising_channel_type,
            campaign.bidding_strategy_type,
            campaign.status,
            metrics.search_impression_share,
            metrics.search_rank_lost_impression_share,
            metrics.search_budget_lost_impression_share,
            metrics.search_top_impression_share,
            metrics.search_absolute_top_impression_share,
            metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
        FROM campaign
        WHERE campaign.status = 'ENABLED'
            AND segments.date BETWEEN '{date_from}' AND '{date_to}'
    """, "campaign_overview")
    out = []
    for r in rows:
        out.append({
            "name":           r.campaign.name,
            "type":           r.campaign.advertising_channel_type,    # 2=Search, 10=PMax
            "bidding":        r.campaign.bidding_strategy_type,
            "impressions":    r.metrics.impressions,
            "clicks":         r.metrics.clicks,
            "cost":           round(r.metrics.cost_micros / 1_000_000, 2),
            "conversions":    round(r.metrics.conversions, 2),
            "is_pct":         round(r.metrics.search_impression_share * 100, 1),
            "rank_lost_pct":  round(r.metrics.search_rank_lost_impression_share * 100, 1),
            "budget_lost_pct":round(r.metrics.search_budget_lost_impression_share * 100, 1),
            "top_is_pct":     round(r.metrics.search_top_impression_share * 100, 1),
            "abs_top_is_pct": round(r.metrics.search_absolute_top_impression_share * 100, 1),
        })
    return out


def get_keyword_quality_scores(client, customer_id, date_from, date_to):
    """Returns keywords with QS, component scores, and recent performance."""
    qs_rows = _search(client, customer_id, """
        SELECT
            campaign.name, ad_group.name,
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.quality_info.quality_score,
            ad_group_criterion.quality_info.creative_quality_score,
            ad_group_criterion.quality_info.post_click_quality_score,
            ad_group_criterion.quality_info.search_predicted_ctr
        FROM ad_group_criterion
        WHERE ad_group_criterion.type = 'KEYWORD'
            AND ad_group_criterion.status != 'REMOVED'
            AND campaign.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
    """, "keyword_qs")

    perf_rows = _search(client, customer_id, f"""
        SELECT
            ad_group_criterion.keyword.text,
            ad_group.name,
            metrics.impressions, metrics.clicks, metrics.cost_micros,
            metrics.conversions, metrics.average_cpc
        FROM keyword_view
        WHERE ad_group_criterion.status != 'REMOVED'
            AND campaign.status = 'ENABLED'
            AND segments.date BETWEEN '{date_from}' AND '{date_to}'
    """, "keyword_perf")

    perf_map = {}
    for r in perf_rows:
        key = (r.ad_group.name, r.ad_group_criterion.keyword.text)
        perf_map[key] = {
            "impressions": r.metrics.impressions,
            "clicks":      r.metrics.clicks,
            "cost":        round(r.metrics.cost_micros / 1_000_000, 2),
            "conversions": round(r.metrics.conversions, 2),
            "avg_cpc":     round(r.metrics.average_cpc / 1_000_000, 2),
        }

    out = []
    seen = set()
    for r in qs_rows:
        qs = r.ad_group_criterion.quality_info.quality_score
        if qs == 0:
            continue  # not enough data
        key = (r.ad_group.name, r.ad_group_criterion.keyword.text)
        if key in seen:
            continue
        seen.add(key)
        perf = perf_map.get(key, {})
        out.append({
            "campaign":     r.campaign.name,
            "ad_group":     r.ad_group.name,
            "keyword":      r.ad_group_criterion.keyword.text,
            "match_type":   r.ad_group_criterion.keyword.match_type,
            "qs":           qs,
            "ad_relevance": QS_COMPONENT_LABELS.get(r.ad_group_criterion.quality_info.creative_quality_score, "N/A"),
            "landing_page": QS_COMPONENT_LABELS.get(r.ad_group_criterion.quality_info.post_click_quality_score, "N/A"),
            "exp_ctr":      QS_COMPONENT_LABELS.get(r.ad_group_criterion.quality_info.search_predicted_ctr, "N/A"),
            **perf,
        })
    return sorted(out, key=lambda x: x["qs"])


def get_ad_copy(client, customer_id, date_from, date_to):
    """Returns RSA headlines/descriptions with performance metrics."""
    rows = _search(client, customer_id, f"""
        SELECT
            campaign.name, ad_group.name,
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.responsive_search_ad.descriptions,
            ad_group_ad.ad.final_urls,
            metrics.impressions, metrics.clicks, metrics.ctr,
            metrics.cost_micros, metrics.conversions
        FROM ad_group_ad
        WHERE ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
            AND campaign.status = 'ENABLED'
            AND ad_group_ad.status = 'ENABLED'
            AND segments.date BETWEEN '{date_from}' AND '{date_to}'
    """, "ad_copy")

    out = []
    for r in rows:
        headlines    = [a.text for a in r.ad_group_ad.ad.responsive_search_ad.headlines]
        descriptions = [a.text for a in r.ad_group_ad.ad.responsive_search_ad.descriptions]
        out.append({
            "campaign":     r.campaign.name,
            "ad_group":     r.ad_group.name,
            "headlines":    headlines,
            "descriptions": descriptions,
            "final_url":    r.ad_group_ad.ad.final_urls[0] if r.ad_group_ad.ad.final_urls else "",
            "impressions":  r.metrics.impressions,
            "clicks":       r.metrics.clicks,
            "ctr":          round(r.metrics.ctr * 100, 2),
            "cost":         round(r.metrics.cost_micros / 1_000_000, 2),
            "conversions":  round(r.metrics.conversions, 2),
        })
    return out


def get_pmax_assets(client, customer_id):
    """Returns PMax asset text grouped by asset group and field type."""
    rows = _search(client, customer_id, """
        SELECT
            asset_group.name, asset_group.status,
            asset_group_asset.field_type,
            asset.type, asset.text_asset.text,
            asset.image_asset.full_size.url
        FROM asset_group_asset
        WHERE asset_group.status = 'ENABLED'
    """, "pmax_assets")

    groups = defaultdict(lambda: defaultdict(list))
    for r in rows:
        field   = r.asset_group_asset.field_type
        text    = r.asset.text_asset.text
        img_url = _safe(lambda: r.asset.image_asset.full_size.url, "")
        if text:
            groups[r.asset_group.name][field].append(text)
        elif img_url:
            groups[r.asset_group.name][field].append(f"[image: {img_url[:60]}...]")
    return {
        grp: {
            ASSET_FIELD_NAMES.get(ftype, f"field_{ftype}"): texts
            for ftype, texts in fields.items()
        }
        for grp, fields in groups.items()
    }


def get_device_breakdown(client, customer_id, date_from, date_to):
    rows = _search(client, customer_id, f"""
        SELECT segments.device,
            metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
        FROM campaign
        WHERE campaign.status = 'ENABLED'
            AND segments.date BETWEEN '{date_from}' AND '{date_to}'
    """, "device")
    agg = defaultdict(lambda: {"impressions": 0, "clicks": 0, "cost": 0.0, "conversions": 0.0})
    for r in rows:
        d = DEVICE_NAMES.get(r.segments.device, f"device_{r.segments.device}")
        agg[d]["impressions"]  += r.metrics.impressions
        agg[d]["clicks"]       += r.metrics.clicks
        agg[d]["cost"]         += r.metrics.cost_micros / 1_000_000
        agg[d]["conversions"]  += r.metrics.conversions
    return {k: {m: round(v, 2) for m, v in vals.items()} for k, vals in agg.items()}


def get_day_of_week(client, customer_id, date_from, date_to):
    rows = _search(client, customer_id, f"""
        SELECT segments.day_of_week,
            metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
        FROM campaign
        WHERE campaign.status = 'ENABLED'
            AND segments.date BETWEEN '{date_from}' AND '{date_to}'
    """, "day_of_week")
    agg = defaultdict(lambda: {"impressions": 0, "clicks": 0, "cost": 0.0, "conversions": 0.0})
    for r in rows:
        d = DAY_NAMES.get(r.segments.day_of_week, f"day_{r.segments.day_of_week}")
        agg[d]["impressions"]  += r.metrics.impressions
        agg[d]["clicks"]       += r.metrics.clicks
        agg[d]["cost"]         += r.metrics.cost_micros / 1_000_000
        agg[d]["conversions"]  += r.metrics.conversions
    day_order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return {d: {m: round(v, 2) for m, v in agg[d].items()} for d in day_order if d in agg}


def get_ad_group_performance(client, customer_id, date_from, date_to):
    rows = _search(client, customer_id, f"""
        SELECT campaign.name, ad_group.name,
            metrics.impressions, metrics.clicks, metrics.cost_micros,
            metrics.conversions, metrics.conversions_value
        FROM ad_group
        WHERE ad_group.status = 'ENABLED'
            AND campaign.status = 'ENABLED'
            AND segments.date BETWEEN '{date_from}' AND '{date_to}'
    """, "ad_group_perf")
    out = []
    for r in rows:
        cost = r.metrics.cost_micros / 1_000_000
        conv = r.metrics.conversions
        out.append({
            "campaign":   r.campaign.name,
            "ad_group":   r.ad_group.name,
            "impressions": r.metrics.impressions,
            "clicks":     r.metrics.clicks,
            "cost":       round(cost, 2),
            "conversions": round(conv, 2),
            "cpa":        round(cost / conv, 2) if conv > 0 else None,
        })
    return sorted(out, key=lambda x: -x["cost"])


# ── Claude analysis ───────────────────────────────────────────────────────────

def generate_deep_dive(account_name, has_pmax, has_search, data, period):
    """Call Claude Sonnet to synthesise all data into a structured deep-dive."""
    try:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return "*(Claude API key not configured — skipping AI analysis)*"

        # Build the data payload as a structured text block
        sections = []

        # Campaign IS
        camps = data.get("campaigns", [])
        if camps:
            lines = ["IMPRESSION SHARE & VISIBILITY (last 7 days):"]
            for c in camps:
                lines.append(
                    f"  {c['name']} | IS: {c['is_pct']}% | "
                    f"Rank Lost: {c['rank_lost_pct']}% | Budget Lost: {c['budget_lost_pct']}% | "
                    f"Top IS: {c['top_is_pct']}% | Abs Top IS: {c['abs_top_is_pct']}% | "
                    f"Spend: £{c['cost']} | Clicks: {c['clicks']} | Conv: {c['conversions']}"
                )
            sections.append("\n".join(lines))

        # Keyword QS
        kws = data.get("keywords", [])
        if kws:
            low_qs  = [k for k in kws if k["qs"] <= 5]
            mid_qs  = [k for k in kws if 6 <= k["qs"] <= 7]
            good_qs = [k for k in kws if k["qs"] >= 8]
            lines = [f"KEYWORD QUALITY SCORES ({len(kws)} keywords with data):"]
            lines.append(f"  QS ≤5 (needs work): {len(low_qs)} keywords")
            lines.append(f"  QS 6-7 (average):   {len(mid_qs)} keywords")
            lines.append(f"  QS ≥8 (strong):     {len(good_qs)} keywords")
            for k in low_qs[:10]:
                perf = f"£{k.get('cost', 0):.2f} spend, {k.get('clicks', 0)} clicks" if k.get("cost") else "no recent data"
                lines.append(
                    f"  [{k['campaign'][:25]}] \"{k['keyword']}\" | QS:{k['qs']} | "
                    f"Ad Rel:{k['ad_relevance']} | LP:{k['landing_page']} | ExpCTR:{k['exp_ctr']} | {perf}"
                )
            if mid_qs:
                lines.append(f"  Mid-QS sample (first 5):")
                for k in mid_qs[:5]:
                    lines.append(f"    \"{k['keyword']}\" QS:{k['qs']} — Ad:{k['ad_relevance']} LP:{k['landing_page']} CTR:{k['exp_ctr']}")
            sections.append("\n".join(lines))

        # Ad copy (Search)
        ads = data.get("ad_copy", [])
        if ads and has_search:
            lines = ["AD COPY (Responsive Search Ads):"]
            for ad in ads:
                lines.append(f"\n  [{ad['campaign']} / {ad['ad_group']}]")
                lines.append(f"  Performance: {ad['impressions']:,} impr | {ad['clicks']} clicks | {ad['ctr']}% CTR | £{ad['cost']} spend | {ad['conversions']} conv")
                lines.append(f"  Headlines ({len(ad['headlines'])}): {' | '.join(ad['headlines'])}")
                lines.append(f"  Descriptions ({len(ad['descriptions'])}): {' | '.join(ad['descriptions'])}")
                lines.append(f"  URL: {ad['final_url']}")
            sections.append("\n".join(lines))

        # PMax assets
        assets = data.get("pmax_assets", {})
        if assets and has_pmax:
            lines = ["PMAX ASSET GROUPS:"]
            for grp_name, fields in assets.items():
                lines.append(f"\n  Asset Group: {grp_name}")
                for field_type, texts in fields.items():
                    # Only show text assets to keep context manageable
                    text_items = [t for t in texts if not t.startswith("[image")]
                    img_count  = sum(1 for t in texts if t.startswith("[image"))
                    if text_items:
                        lines.append(f"    {field_type} ({len(text_items)}): {' | '.join(text_items[:8])}")
                    if img_count:
                        lines.append(f"    {field_type} — {img_count} image asset(s)")
            sections.append("\n".join(lines))

        # Device breakdown
        devices = data.get("devices", {})
        if devices:
            lines = ["DEVICE PERFORMANCE:"]
            total_cost  = sum(v["cost"] for v in devices.values()) or 1
            total_conv  = sum(v["conversions"] for v in devices.values())
            for dev, m in sorted(devices.items(), key=lambda x: -x[1]["cost"]):
                cpa = f"£{m['cost']/m['conversions']:.2f}" if m["conversions"] > 0 else "—"
                lines.append(
                    f"  {dev}: {m['clicks']} clicks | £{m['cost']:.2f} ({m['cost']/total_cost*100:.0f}% spend) | "
                    f"{m['conversions']} conv | CPA {cpa}"
                )
            sections.append("\n".join(lines))

        # Day of week
        days = data.get("days", {})
        if days:
            lines = ["DAY-OF-WEEK PERFORMANCE:"]
            for day, m in days.items():
                cpa = f"£{m['cost']/m['conversions']:.2f}" if m["conversions"] > 0 else "—"
                lines.append(f"  {day}: {m['clicks']} clicks | £{m['cost']:.2f} spend | {m['conversions']} conv | CPA {cpa}")
            sections.append("\n".join(lines))

        # Ad group performance
        ag_perf = data.get("ad_groups", [])
        if ag_perf:
            lines = ["AD GROUP PERFORMANCE (sorted by spend):"]
            for ag in ag_perf[:10]:
                cpa = f"£{ag['cpa']:.2f}" if ag["cpa"] else "—"
                lines.append(
                    f"  [{ag['campaign'][:25]}] {ag['ad_group']} | "
                    f"£{ag['cost']} spend | {ag['clicks']} clicks | {ag['conversions']} conv | CPA {cpa}"
                )
            sections.append("\n".join(lines))

        data_block = "\n\n".join(sections)

        account_context = []
        if has_pmax:
            account_context.append("PMax campaign (Shopping + Display + Search combined — shopping feed quality is critical)")
        if has_search:
            account_context.append("Search campaign(s) (keyword-targeted text ads)")

        system_prompt = (
            "You are a senior PPC analyst with 10+ years of Google Ads experience. "
            "You write rigorous, specific account manager briefs — no filler, no generic advice. "
            "Every recommendation must reference the actual data provided. "
            "Use markdown with clear headers. Be direct and concise."
        )

        pmax_section = (
            "\n## Shopping & Asset Analysis (PMax)\n"
            "Assess the PMax asset groups: are headlines/descriptions varied and compelling? "
            "Are there enough assets in each field type? Suggest specific improvements to asset text copy. "
            "Assess whether asset group segmentation (by product category) looks appropriate."
        ) if has_pmax else ""

        search_section = (
            "\n## Ad Copy & Messaging (Search)\n"
            "Review each RSA: headline variety, keyword inclusion, USP clarity, CTAs. "
            "Flag any ads with low CTR. Suggest 3-5 specific headline or description improvements "
            "based on what is missing."
        ) if has_search else ""

        campaign_types_str = " + ".join(account_context)

        user_prompt = (
            f"Analyse the Google Ads account data below for **{account_name}** "
            f"and write a comprehensive account manager deep-dive.\n\n"
            f"**Period:** {period}\n"
            f"**Campaign types:** {campaign_types_str}\n\n"
            f"---\n\n{data_block}\n\n---\n\n"
            "Write a deep-dive brief using exactly these sections (include all that are relevant):\n\n"
            "## Visibility & Impression Share\n"
            "Diagnose what % of available impressions are being captured per campaign. "
            "Distinguish rank-lost IS (bid/QS issue) from budget-lost IS (budget issue) — they require different fixes. "
            "Flag if abs top IS is below 20% for brand terms.\n\n"
            "## Keyword Quality Score Audit\n"
            "List all keywords with QS 5 or below with specific diagnosis (which component is dragging it: "
            "ad relevance, landing page, or expected CTR). Give concrete fix for each. "
            "Highlight keywords with spend that are underperforming on QS."
            + pmax_section
            + search_section
            + "\n\n## Device & Scheduling Insights\n"
            "Identify any device where performance is significantly better or worse (cost per conversion gap). "
            "Flag if mobile CPA is more than 50% higher than desktop. "
            "Identify strongest/weakest days and suggest ad scheduling adjustments if warranted.\n\n"
            "## Ad Group Structure\n"
            "Are campaigns well-segmented? Any ad groups with high spend but zero conversions? "
            "Any consolidation or split opportunities?\n\n"
            "## Priority Actions This Week\n"
            "This section is REQUIRED and must always appear, even if the account is performing well. "
            "If there are genuine priority actions: numbered list of up to 7, ordered by expected impact — "
            "name the campaign, ad group, or keyword, and state the expected outcome. "
            "If there are no urgent actions, write exactly: 'No priority actions this week — account is on track.' "
            "Do not skip or omit this section."
        )

        ai_client = anthropic.Anthropic(api_key=api_key)
        msg = ai_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return msg.content[0].text.strip()

    except Exception as e:
        return f"*(Analysis generation failed: {e})*"


# ── Note writer ───────────────────────────────────────────────────────────────

def write_deep_dive_note(base, account_name, analysis_md, period_str, date_slug):
    folder = os.path.join(base, account_name, "Performance Reports")
    os.makedirs(folder, exist_ok=True)
    filepath = os.path.join(folder, f"deep_analysis_{date_slug}.md")

    lines = [
        "---",
        f"tags: [google-ads, deep-analysis, {account_name.lower().replace(' ', '-')}]",
        f"date: {date.today().isoformat()}",
        f"account: {account_name}",
        f"period: \"{period_str}\"",
        "---",
        "",
        f"# {account_name} — Account Manager Deep Dive",
        f"**Period:** {period_str}  ",
        f"**Generated:** {date.today().strftime('%d %b %Y')} | *Powered by Claude Sonnet*",
        "",
        "> [!tip] How to use this",
        "> This brief is for internal use. Work through the Priority Actions list before preparing the client update.",
        "",
        "---",
        "",
        analysis_md,
        "",
    ]
    with open(filepath, "w") as f:
        f.write("\n".join(lines) + "\n")
    return filepath


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    client = GoogleAdsClient.load_from_storage("google-ads.yaml")

    current_end   = date.today() - timedelta(days=1)
    current_start = current_end - timedelta(days=6)
    date_from     = current_start.strftime("%Y-%m-%d")
    date_to       = current_end.strftime("%Y-%m-%d")
    date_slug     = current_end.strftime("%Y-%m-%d")
    period_str    = f"{current_start.strftime('%d %b')} – {current_end.strftime('%d %b %Y')}"

    print(f"\nDeep Analysis — {period_str}")
    print("=" * 70)

    accounts = get_client_accounts(client)
    if not accounts:
        print("No client accounts found.")
        return

    for account in accounts:
        name = account["name"]
        cid  = account["id"]
        print(f"\n  [{name}]  ID: {cid}")

        # Campaign overview + IS
        print("    Pulling campaign overview...")
        campaigns = get_campaign_overview(client, cid, date_from, date_to)
        has_pmax   = any(c["type"] == PMAX_CHANNEL_TYPE   for c in campaigns)
        has_search = any(c["type"] == SEARCH_CHANNEL_TYPE for c in campaigns)
        print(f"    Campaign types — PMax: {has_pmax}, Search: {has_search}")

        # Keyword QS + performance
        print("    Pulling keyword quality scores...")
        keywords = get_keyword_quality_scores(client, cid, date_from, date_to)
        print(f"    Keywords with QS data: {len(keywords)}")

        # Ad copy (Search)
        ad_copy = []
        if has_search:
            print("    Pulling ad copy...")
            ad_copy = get_ad_copy(client, cid, date_from, date_to)
            print(f"    RSAs found: {len(ad_copy)}")

        # PMax assets
        pmax_assets = {}
        if has_pmax:
            print("    Pulling PMax assets...")
            pmax_assets = get_pmax_assets(client, cid)
            print(f"    Asset groups: {len(pmax_assets)}")

        # Device + day breakdowns
        print("    Pulling device & day-of-week data...")
        devices = get_device_breakdown(client, cid, date_from, date_to)
        days    = get_day_of_week(client, cid, date_from, date_to)

        # Ad group performance
        print("    Pulling ad group performance...")
        ad_groups = get_ad_group_performance(client, cid, date_from, date_to)

        # Generate AI deep dive
        print("    Generating deep dive with Claude Sonnet...")
        data = {
            "campaigns":   campaigns,
            "keywords":    keywords,
            "ad_copy":     ad_copy,
            "pmax_assets": pmax_assets,
            "devices":     devices,
            "days":        days,
            "ad_groups":   ad_groups,
        }
        analysis = generate_deep_dive(name, has_pmax, has_search, data, period_str)

        # Write note
        note_path = write_deep_dive_note(OBSIDIAN_BASE, name, analysis, period_str, date_slug)
        print(f"    → {note_path}")

    print("\n" + "=" * 70)
    print("Deep analysis complete.")


if __name__ == "__main__":
    main()
