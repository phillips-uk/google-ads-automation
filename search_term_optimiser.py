"""
Weekly search term optimiser — intent-based irrelevance detection.

For each client account, pulls the last 30 days of search terms and identifies
those that are irrelevant to the account's core intent (derived from its ad group
keywords and names).

Detection strategy:
  1. Claude AI (preferred): passes all keywords + search terms to Claude in a
     single batch call per account. Activated when ANTHROPIC_API_KEY is set.
  2. Keyword overlap fallback: flags terms with no word overlap with any keyword
     in the account after removing PPC stop-words.

Outputs:
  - Obsidian markdown per account → My Brain/Google Ads Clients/<name>/Search Term Reports/
  - reports/search_terms/  (CSV backups)

Thresholds (override via env vars):
  MIN_SPEND_TO_REVIEW  — minimum spend (£) for a term to be worth analysing (default: 1)
  MIN_CLICKS_TO_REVIEW — minimum clicks for a term to be worth analysing (default: 3)
  LOOKBACK_DAYS        — days to look back (default: 30)
"""

import csv
import json
import os
from datetime import date, timedelta

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from config import MCC_ID, OBSIDIAN_BASE, LOOKBACK_DAYS, load_env
from obsidian_writer import write_search_term_report

load_env()

CSV_DIR = "reports/search_terms"
MIN_SPEND_TO_REVIEW = float(os.getenv("MIN_SPEND_TO_REVIEW", 1))
MIN_CLICKS_TO_REVIEW = int(os.getenv("MIN_CLICKS_TO_REVIEW", 3))

# Words that carry no semantic meaning for relevance decisions
PPC_STOP_WORDS = {
    "a", "an", "the", "and", "or", "for", "in", "on", "at", "to", "of",
    "with", "by", "near", "me", "my", "i", "is", "are", "was", "how",
    "what", "where", "when", "why", "can", "do", "get", "uk", "london",
    "best", "top", "cheap", "free", "good", "new", "buy", "price",
}


# ---------------------------------------------------------------------------
# Google Ads data fetching
# ---------------------------------------------------------------------------

def get_client_accounts(client):
    ga_service = client.get_service("GoogleAdsService")
    query = """
        SELECT customer_client.id, customer_client.descriptive_name,
               customer_client.manager, customer_client.status
        FROM customer_client
        WHERE customer_client.level <= 1 AND customer_client.status = 'ENABLED'
    """
    response = ga_service.search(customer_id=MCC_ID, query=query)
    return [
        {
            "id": str(row.customer_client.id),
            "name": row.customer_client.descriptive_name or str(row.customer_client.id),
        }
        for row in response
        if not row.customer_client.manager and row.customer_client.id != int(MCC_ID)
    ]


def get_keywords_by_adgroup(client, customer_id):
    """Return {ad_group_name: [keyword_text, ...], ...} for enabled keywords."""
    ga_service = client.get_service("GoogleAdsService")
    query = """
        SELECT
            ad_group.name,
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type
        FROM ad_group_criterion
        WHERE
            ad_group_criterion.type = 'KEYWORD'
            AND ad_group_criterion.status != 'REMOVED'
            AND campaign.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
    """
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
    except GoogleAdsException:
        return {}

    groups = {}
    for row in response:
        ag = row.ad_group.name
        kw = row.ad_group_criterion.keyword.text
        groups.setdefault(ag, []).append(kw)
    return groups


def get_search_terms(client, customer_id, date_from, date_to):
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            search_term_view.search_term,
            campaign.name,
            ad_group.name,
            metrics.impressions, metrics.clicks,
            metrics.cost_micros, metrics.conversions,
            metrics.conversions_value, metrics.ctr
        FROM search_term_view
        WHERE
            segments.date BETWEEN '{date_from}' AND '{date_to}'
            AND campaign.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
        ORDER BY metrics.cost_micros DESC
    """
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
    except GoogleAdsException as ex:
        print(f"  [SKIP] {customer_id}: {ex.failure.errors[0].message}")
        return []

    rows = []
    for row in response:
        cost = row.metrics.cost_micros / 1_000_000
        rows.append({
            "search_term": row.search_term_view.search_term,
            "campaign_name": row.campaign.name,
            "ad_group_name": row.ad_group.name,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": round(cost, 2),
            "ctr_pct": round(row.metrics.ctr * 100, 2),
            "conversions": round(row.metrics.conversions, 2),
            "conv_value": round(row.metrics.conversions_value, 2),
        })
    return rows


# ---------------------------------------------------------------------------
# Intent analysis — Claude AI
# ---------------------------------------------------------------------------

def _claude_classify(account_name, keywords_by_adgroup, search_terms):
    """
    Ask Claude to classify each search term as RELEVANT or IRRELEVANT.
    Returns a dict: {search_term_lower: {"relevant": bool, "reason": str}}
    """
    try:
        import anthropic
    except ImportError:
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    # Build the ad group summary
    ag_summary_lines = []
    for ag_name, kws in keywords_by_adgroup.items():
        kw_sample = ", ".join(kws[:15])
        ag_summary_lines.append(f'  - Ad group "{ag_name}": {kw_sample}')
    ag_summary = "\n".join(ag_summary_lines) if ag_summary_lines else "  (no keyword data)"

    # Only send terms that haven't been trivially decided
    terms_list = "\n".join(
        f"{i+1}. {r['search_term']}"
        for i, r in enumerate(search_terms)
    )

    prompt = f"""You are a PPC account manager auditing search terms for {account_name}.

The account's ad groups and their keywords are:
{ag_summary}

Based on the ad group themes above, classify each search term below as either:
- RELEVANT: someone searching this could plausibly be interested in what the account offers
- IRRELEVANT: clearly off-intent (different service, informational with no commercial value, competitor, unrelated topic)

Search terms to classify:
{terms_list}

Respond ONLY with a JSON array. Each element: {{"term": "<exact term>", "relevant": true/false, "reason": "<one short phrase>"}}
Do not include any text outside the JSON array."""

    try:
        ai_client = anthropic.Anthropic(api_key=api_key)
        message = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        print(f"  [WARN] Claude API error: {e}")
        return None

    try:
        text = message.content[0].text.strip()
        # Strip markdown code block if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        results = json.loads(text)
        return {item["term"].lower(): {"relevant": item["relevant"], "reason": item.get("reason", "")} for item in results}
    except Exception as e:
        print(f"  [WARN] Claude response parse error: {e}")
        return None


# ---------------------------------------------------------------------------
# Intent analysis — keyword overlap fallback
# ---------------------------------------------------------------------------

def _keyword_overlap_classify(keywords_by_adgroup, search_term):
    """
    Returns (is_irrelevant: bool, reason: str).
    A term is flagged if it shares no meaningful words with any keyword in the account.
    """
    all_keywords = [kw for kws in keywords_by_adgroup.values() for kw in kws]
    if not all_keywords:
        return False, ""

    term_words = {w for w in search_term.lower().split() if w not in PPC_STOP_WORDS and len(w) > 2}
    if not term_words:
        return False, ""

    keyword_words = set()
    for kw in all_keywords:
        for w in kw.lower().split():
            if w not in PPC_STOP_WORDS and len(w) > 2:
                keyword_words.add(w)

    overlap = term_words & keyword_words
    if not overlap:
        return True, "No word overlap with any account keyword"
    return False, ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    client = GoogleAdsClient.load_from_storage("google-ads.yaml")

    date_to = date.today() - timedelta(days=1)
    date_from = date_to - timedelta(days=LOOKBACK_DAYS - 1)
    date_from_str = date_from.strftime("%Y-%m-%d")
    date_to_str = date_to.strftime("%Y-%m-%d")
    run_date = date.today().strftime("%Y%m%d")

    using_ai = bool(os.environ.get("ANTHROPIC_API_KEY", ""))
    mode = "Claude AI" if using_ai else "keyword overlap (set ANTHROPIC_API_KEY for AI mode)"

    print(f"\nSearch Term Optimiser — {date_from_str} → {date_to_str}")
    print(f"Mode: {mode}")
    print(f"Min spend to review: £{MIN_SPEND_TO_REVIEW} | Min clicks: {MIN_CLICKS_TO_REVIEW}")
    print("=" * 70)

    accounts = get_client_accounts(client)
    if not accounts:
        print("No client accounts found.")
        return

    os.makedirs(CSV_DIR, exist_ok=True)
    total_irrelevant = 0
    total_wasted_spend = 0

    for account in accounts:
        print(f"\n  [{account['name']}]  ID: {account['id']}")

        keywords_by_adgroup = get_keywords_by_adgroup(client, account["id"])
        search_terms = get_search_terms(client, account["id"], date_from_str, date_to_str)

        if not search_terms:
            print("    No search term data.")
            continue

        # Filter to terms worth reviewing
        reviewable = [
            r for r in search_terms
            if r["cost"] >= MIN_SPEND_TO_REVIEW or r["clicks"] >= MIN_CLICKS_TO_REVIEW
        ]
        print(f"    Search terms: {len(search_terms)} total, {len(reviewable)} above threshold")

        if not reviewable:
            print("    Nothing above threshold — skipping.")
            continue

        # Classify
        irrelevant_terms = []

        if using_ai:
            print(f"    Classifying {len(reviewable)} terms with Claude...")
            classifications = _claude_classify(account["name"], keywords_by_adgroup, reviewable)

            if classifications is None:
                print("    [WARN] AI classification failed — falling back to keyword overlap")
                using_ai_this_account = False
            else:
                using_ai_this_account = True

            if using_ai_this_account:
                for row in reviewable:
                    result = classifications.get(row["search_term"].lower())
                    if result and not result["relevant"]:
                        irrelevant_terms.append({**row, "reason": result["reason"]})
        else:
            classifications = None

        if not using_ai or classifications is None:
            for row in reviewable:
                is_irrel, reason = _keyword_overlap_classify(keywords_by_adgroup, row["search_term"])
                if is_irrel:
                    irrelevant_terms.append({**row, "reason": reason})

        wasted_spend = sum(r["cost"] for r in irrelevant_terms)
        total_irrelevant += len(irrelevant_terms)
        total_wasted_spend += wasted_spend

        print(f"    Irrelevant terms: {len(irrelevant_terms)}  (£{wasted_spend:,.2f} wasted spend)")

        if irrelevant_terms:
            # CSV backup
            acct_slug = account["name"].replace(" ", "_").lower()
            csv_path = os.path.join(CSV_DIR, f"irrelevant_{acct_slug}_{run_date}.csv")
            csv_fields = [
                "search_term", "campaign_name", "ad_group_name",
                "impressions", "clicks", "cost", "ctr_pct",
                "conversions", "conv_value", "reason",
            ]
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(irrelevant_terms)

            # Obsidian report
            md_path = write_search_term_report(
                OBSIDIAN_BASE, account["name"], date_from_str, date_to_str, irrelevant_terms
            )
            print(f"    → Obsidian: {md_path}")
            print(f"    → CSV:      {csv_path}")

    print("\n" + "=" * 70)
    print(f"TOTAL IRRELEVANT TERMS:  {total_irrelevant}")
    print(f"TOTAL WASTED SPEND:      £{total_wasted_spend:,.2f}")


if __name__ == "__main__":
    main()
