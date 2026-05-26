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
          AND campaign.status = 'ENABLED'
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


def get_pmax_asset_performance(client, customer_id):
    """Returns PMax asset text grouped by asset group and field type.
    Note: asset_group_asset.performance_label was removed in Google Ads API v17.
    Labels are returned as 'N/A' — use asset group level metrics for performance signals."""

    rows = _search(client, customer_id, """
        SELECT
          asset_group.name,
          asset_group_asset.field_type,
          asset.text_asset.text,
          asset.name,
          asset.type
        FROM asset_group_asset
        WHERE asset_group.status = 'ENABLED'
          AND campaign.status = 'ENABLED'
    """, "pmax_asset_performance")

    groups = defaultdict(lambda: defaultdict(list))
    for r in rows:
        field_int = r.asset_group_asset.field_type
        field_name = ASSET_FIELD_NAMES.get(field_int, f"field_{field_int}")
        label = "N/A"
        text = r.asset.text_asset.text or r.asset.name or ""
        groups[r.asset_group.name][field_name].append({"text": text, "label": label})

    return {grp: dict(fields) for grp, fields in groups.items()}


def get_pmax_asset_group_metrics(client, customer_id, date_from, date_to, prev_from, prev_to):
    """Returns asset group performance for current and previous period with WoW spend change."""

    def _fetch_period(from_date, to_date):
        rows = _search(client, customer_id, f"""
            SELECT
              asset_group.id, asset_group.name, asset_group.status,
              metrics.impressions, metrics.clicks, metrics.cost_micros,
              metrics.conversions, metrics.conversions_value, metrics.ctr
            FROM asset_group
            WHERE asset_group.status = 'ENABLED'
              AND campaign.status = 'ENABLED'
              AND segments.date BETWEEN '{from_date}' AND '{to_date}'
        """, f"pmax_ag_metrics_{from_date}")
        agg = {}
        for r in rows:
            ag_id = r.asset_group.id
            if ag_id not in agg:
                agg[ag_id] = {
                    "id": ag_id,
                    "name": r.asset_group.name,
                    "impressions": 0,
                    "clicks": 0,
                    "cost": 0.0,
                    "conversions": 0.0,
                    "conversions_value": 0.0,
                    "ctr": 0.0,
                }
            agg[ag_id]["impressions"] += r.metrics.impressions
            agg[ag_id]["clicks"] += r.metrics.clicks
            agg[ag_id]["cost"] += r.metrics.cost_micros / 1_000_000
            agg[ag_id]["conversions"] += r.metrics.conversions
            agg[ag_id]["conversions_value"] += r.metrics.conversions_value
        for ag in agg.values():
            ag["cost"] = round(ag["cost"], 2)
            ag["conversions"] = round(ag["conversions"], 2)
            ag["conversions_value"] = round(ag["conversions_value"], 2)
            ag["roas"] = round(ag["conversions_value"] / ag["cost"], 2) if ag["cost"] > 0 else None
        return agg

    current = _fetch_period(date_from, date_to)
    previous = _fetch_period(prev_from, prev_to)

    out = []
    for ag_id, curr in current.items():
        prev = previous.get(ag_id, {})
        prev_cost = prev.get("cost", 0.0)
        if prev_cost > 0:
            wow_pct = round((curr["cost"] - prev_cost) / prev_cost * 100, 1)
        else:
            wow_pct = None
        out.append({**curr, "wow_spend_pct": wow_pct})

    return sorted(out, key=lambda x: -x["cost"])


def get_pmax_listing_group_perf(client, customer_id, date_from, date_to):
    """Returns performance by listing group filter (product_type level)."""
    try:
        rows = _search(client, customer_id, f"""
            SELECT
              asset_group.name,
              asset_group_listing_group_filter.case_value.product_type.value,
              asset_group_listing_group_filter.type,
              metrics.impressions, metrics.clicks, metrics.cost_micros,
              metrics.conversions, metrics.conversions_value
            FROM asset_group_product_group_view
            WHERE asset_group.status = 'ENABLED'
              AND campaign.status = 'ENABLED'
              AND asset_group_listing_group_filter.type = 'UNIT_INCLUDED'
              AND segments.date BETWEEN '{date_from}' AND '{date_to}'
        """, "pmax_listing_groups")
    except Exception:
        return []

    out = []
    for r in rows:
        cost = r.metrics.cost_micros / 1_000_000
        conv_val = r.metrics.conversions_value
        out.append({
            "asset_group": r.asset_group.name,
            "product_type": r.asset_group_listing_group_filter.case_value.product_type.value or "(All products)",
            "impressions": r.metrics.impressions,
            "clicks": r.metrics.clicks,
            "cost": round(cost, 2),
            "conversions": round(r.metrics.conversions, 2),
            "roas": round(conv_val / cost, 2) if cost > 0 else None,
        })
    return sorted(out, key=lambda x: -x["cost"])


def get_pmax_audience_signals(client, customer_id):
    """Returns audience signals per asset group, resolved to audience names where possible."""
    try:
        signal_rows = _search(client, customer_id, """
            SELECT
              asset_group.name,
              asset_group_signal.audience.audience
            FROM asset_group_signal
            WHERE asset_group.status = 'ENABLED'
              AND campaign.status = 'ENABLED'
        """, "pmax_audience_signals")
    except Exception:
        return {}

    # Collect all audience resource names
    audience_resource_names = set()
    raw_signals = defaultdict(list)
    for r in signal_rows:
        aud_resource = r.asset_group_signal.audience.audience
        raw_signals[r.asset_group.name].append(aud_resource)
        if aud_resource:
            audience_resource_names.add(aud_resource)

    # Extract numeric IDs from resource names (format: customers/{cid}/audiences/{id})
    audience_ids = []
    for res in audience_resource_names:
        parts = res.split("/")
        if len(parts) >= 4:
            try:
                audience_ids.append(int(parts[-1]))
            except ValueError:
                pass

    # Fetch audience names
    name_map = {}
    if audience_ids:
        id_list = ", ".join(str(i) for i in audience_ids)
        try:
            aud_rows = _search(client, customer_id, f"""
                SELECT audience.id, audience.name, audience.description
                FROM audience
                WHERE audience.status = 'ENABLED'
                  AND audience.id IN ({id_list})
            """, "pmax_audience_names")
            for r in aud_rows:
                name_map[r.audience.id] = r.audience.name or str(r.audience.id)
        except Exception:
            pass

    # Build output: resolve resource names to audience names
    out = {}
    for ag_name, resources in raw_signals.items():
        resolved = []
        for res in resources:
            if not res:
                continue
            parts = res.split("/")
            if len(parts) >= 4:
                try:
                    aud_id = int(parts[-1])
                    resolved.append(name_map.get(aud_id, str(aud_id)))
                except ValueError:
                    resolved.append(res)
            else:
                resolved.append(res)
        out[ag_name] = resolved

    return out


# ── PMax Intelligence Claude analysis ────────────────────────────────────────

def generate_pmax_intelligence(account_name, asset_performance, ag_metrics, listing_groups, audience_signals):
    """Call Claude Sonnet to generate a focused PMax intelligence block."""
    try:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return "*(Claude API key not configured — skipping PMax intelligence)*"

        sections = []

        # Asset group performance table
        if ag_metrics:
            lines = ["ASSET GROUP PERFORMANCE (current period vs prior week):"]
            for ag in ag_metrics:
                wow = f"{ag['wow_spend_pct']:+.1f}%" if ag["wow_spend_pct"] is not None else "N/A"
                roas = f"{ag['roas']:.2f}" if ag["roas"] is not None else "—"
                lines.append(
                    f"  {ag['name']} | Spend: £{ag['cost']} | Clicks: {ag['clicks']} | "
                    f"Conv: {ag['conversions']} | ROAS: {roas} | WoW Spend: {wow}"
                )
            sections.append("\n".join(lines))

        # Performance label breakdown
        if asset_performance:
            lines = ["ASSET PERFORMANCE LABELS (by asset group and type):"]
            label_types = {"Headline", "Description", "Long Headline", "Marketing Image", "Square Image"}
            for grp_name, fields in asset_performance.items():
                lines.append(f"\n  Asset Group: {grp_name}")
                for field_name, assets in fields.items():
                    if field_name not in label_types:
                        continue
                    counts = defaultdict(int)
                    low_assets = []
                    for a in assets:
                        counts[a["label"]] += 1
                        if a["label"] == "LOW" and a["text"]:
                            low_assets.append(a["text"])
                    count_str = " | ".join(f"{lbl}: {cnt}" for lbl, cnt in sorted(counts.items()))
                    lines.append(f"    {field_name}: {count_str}")
                    if low_assets:
                        lines.append(f"      LOW assets: {' | '.join(low_assets[:5])}")
            sections.append("\n".join(lines))

        # Listing groups
        if listing_groups:
            lines = ["LISTING GROUP PERFORMANCE (product type level):"]
            for lg in listing_groups[:15]:
                roas = f"{lg['roas']:.2f}" if lg["roas"] is not None else "—"
                lines.append(
                    f"  [{lg['asset_group']}] {lg['product_type']} | "
                    f"Spend: £{lg['cost']} | Conv: {lg['conversions']} | ROAS: {roas}"
                )
            sections.append("\n".join(lines))

        # Audience signals
        if audience_signals:
            lines = ["AUDIENCE SIGNALS (per asset group):"]
            for ag_name, signals in audience_signals.items():
                if signals:
                    lines.append(f"  {ag_name}: {', '.join(signals)}")
                else:
                    lines.append(f"  {ag_name}: (no signals configured)")
            # Flag groups with no signals
            all_groups = set(ag["name"] for ag in ag_metrics) if ag_metrics else set()
            groups_with_signals = set(audience_signals.keys())
            missing = all_groups - groups_with_signals
            if missing:
                lines.append(f"  Groups with NO audience signals: {', '.join(missing)}")
            sections.append("\n".join(lines))

        data_block = "\n\n".join(sections)

        has_listing = bool(listing_groups)
        has_signals = bool(audience_signals)

        listing_instruction = (
            "\n### Listing Group Performance\n"
            "Which product types are generating the best returns? "
            "Flag any product type with high spend and low/zero ROAS. "
            "Note if any asset group has no listing group data."
        ) if has_listing else ""

        signals_instruction = (
            "\n### Audience Signals Review\n"
            "Note which asset groups have signals and which don't. "
            "Flag if signals look thin (e.g., only one signal per group, no customer match, no custom intent). "
            "Suggest specific audience signals to add where missing."
        ) if has_signals else (
            "\n### Audience Signals Review\n"
            "No audience signals data available. Note this and recommend adding signals."
        )

        system_prompt = (
            "You are a senior PPC analyst and PMax specialist. "
            "You write rigorous, specific analysis — no filler, no generic advice. "
            "Every recommendation must reference actual asset names, group names, or metrics from the data provided. "
            "Use markdown with the exact sub-headers specified."
        )

        user_prompt = (
            f"Analyse the PMax data below for **{account_name}** and write a focused PMax intelligence section.\n\n"
            f"---\n\n{data_block}\n\n---\n\n"
            "Write a PMax analysis using exactly these sub-headers:\n\n"
            "### Asset Group Scorecard\n"
            "Rank asset groups by performance. Note any with strong WoW improvement or decline. "
            "Flag any group with ROAS below 1.0 or zero conversions. "
            "Identify the best-performing group and explain what's driving it.\n\n"
            "### Asset Performance Labels — What to Swap\n"
            "Flag any text asset rated LOW by name and suggest a specific replacement. "
            "Flag if any asset type has 0 BEST assets. "
            "Flag if PENDING count is high (>3 per field type — not enough data yet). "
            "Be specific: quote the LOW asset text and provide a concrete replacement.\n"
            + listing_instruction
            + signals_instruction
            + "\n\n### PMax Optimisation Plan\n"
            "Numbered list, maximum 6 items, ordered by expected impact. "
            "Each item must reference a specific asset group, metric, or audience by name. "
            "Focus on the highest-leverage actions: asset swaps, bid signals, listing group splits, audience additions."
        )

        ai_client = anthropic.Anthropic(api_key=api_key)
        msg = ai_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return msg.content[0].text.strip()

    except Exception as e:
        return f"*(PMax intelligence generation failed: {e})*"


def get_pmax_intelligence_block(client, customer_id, account_name, date_from, date_to, prev_from, prev_to):
    """Orchestrates all PMax data fetches and generates a complete markdown block."""

    # 1. Fetch all four data sources
    asset_performance = get_pmax_asset_performance(client, customer_id)
    ag_metrics        = get_pmax_asset_group_metrics(client, customer_id, date_from, date_to, prev_from, prev_to)
    listing_groups    = get_pmax_listing_group_perf(client, customer_id, date_from, date_to)
    audience_signals  = get_pmax_audience_signals(client, customer_id)

    # 2. Build raw data tables
    block_lines = [f"## PMax Intelligence", ""]

    # Asset group performance table
    block_lines += [
        "### Raw Data — Asset Group Performance",
        "",
        "| Asset Group | Spend | Clicks | Conv | ROAS | WoW Spend |",
        "|---|---|---|---|---|---|",
    ]
    for ag in ag_metrics:
        roas = f"{ag['roas']:.2f}" if ag["roas"] is not None else "—"
        wow  = f"{ag['wow_spend_pct']:+.1f}%" if ag["wow_spend_pct"] is not None else "—"
        block_lines.append(
            f"| {ag['name']} | £{ag['cost']} | {ag['clicks']} | {ag['conversions']} | {roas} | {wow} |"
        )

    # Asset label summary table
    block_lines += ["", "### Raw Data — Asset Performance Labels", ""]
    label_types = {"Headline", "Description", "Long Headline", "Marketing Image", "Square Image"}
    label_rows = []
    for grp_name, fields in asset_performance.items():
        for field_name, assets in fields.items():
            if field_name not in label_types:
                continue
            counts = defaultdict(int)
            for a in assets:
                counts[a["label"]] += 1
            label_rows.append((
                grp_name, field_name,
                counts.get("BEST", 0), counts.get("GOOD", 0),
                counts.get("LOW", 0), counts.get("PENDING", 0),
            ))

    if label_rows:
        block_lines += [
            "| Asset Group | Type | BEST | GOOD | LOW | PENDING |",
            "|---|---|---|---|---|---|",
        ]
        for row in label_rows:
            block_lines.append(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} | {row[5]} |")
    else:
        block_lines.append("*No asset label data available.*")

    # Listing group table
    block_lines += ["", "### Raw Data — Listing Group Performance", ""]
    if listing_groups:
        block_lines += [
            "| Asset Group | Product Type | Spend | Conv | ROAS |",
            "|---|---|---|---|---|",
        ]
        for lg in listing_groups:
            roas = f"{lg['roas']:.2f}" if lg["roas"] is not None else "—"
            block_lines.append(
                f"| {lg['asset_group']} | {lg['product_type']} | £{lg['cost']} | {lg['conversions']} | {roas} |"
            )
    else:
        block_lines.append("*No listing group data available.*")

    # Audience signals table
    block_lines += ["", "### Raw Data — Audience Signals", ""]
    if audience_signals:
        block_lines += [
            "| Asset Group | Audience Signals |",
            "|---|---|",
        ]
        for ag_name, signals in audience_signals.items():
            sig_str = ", ".join(signals) if signals else "*(none)*"
            block_lines.append(f"| {ag_name} | {sig_str} |")
    else:
        block_lines.append("*No audience signal data available.*")

    block_lines += ["", "---", ""]

    # 3. Generate Claude analysis
    claude_analysis = generate_pmax_intelligence(
        account_name, asset_performance, ag_metrics, listing_groups, audience_signals
    )
    block_lines.append(claude_analysis)

    return "\n".join(block_lines)


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
