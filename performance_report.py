"""
Weekly performance report — last 7 days vs previous 7 days.

Pulls daily segmented data using conversions_by_conversion_date so conversion
timing reflects when conversions actually happened, not the click date.

Outputs per account:
  - Obsidian markdown  → My Brain/Google Ads Clients/<name>/Performance Reports/
  - Branded xlsx       → same folder (Results tab + Data tab with SUMIFS)
"""

from datetime import date, timedelta

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from config import MCC_ID, OBSIDIAN_BASE, load_env
from obsidian_writer import write_performance_report

load_env()


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
        {"id": str(row.customer_client.id), "name": row.customer_client.descriptive_name or str(row.customer_client.id)}
        for row in response
        if not row.customer_client.manager and row.customer_client.id != int(MCC_ID)
    ]


def get_daily_performance(client, customer_id, date_from, date_to):
    """
    Returns one row per (date, campaign) for the full 14-day window.
    Uses conversions_by_conversion_date so numbers reflect when conversions happened.
    """
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            segments.date,
            campaign.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions_by_conversion_date,
            metrics.conversions_value_by_conversion_date
        FROM campaign
        WHERE
            segments.date BETWEEN '{date_from}' AND '{date_to}'
            AND campaign.status = 'ENABLED'
        ORDER BY segments.date ASC, metrics.cost_micros DESC
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
            "date":          row.segments.date,          # ISO string "YYYY-MM-DD"
            "campaign_name": row.campaign.name,
            "impressions":   row.metrics.impressions,
            "clicks":        row.metrics.clicks,
            "cost":          round(cost, 4),
            "conversions":   round(row.metrics.conversions_by_conversion_date, 4),
            "conv_value":    round(row.metrics.conversions_value_by_conversion_date, 4),
        })
    return rows


def main():
    client = GoogleAdsClient.load_from_storage("google-ads.yaml")

    # Date windows
    current_end   = date.today() - timedelta(days=1)       # yesterday
    current_start = current_end - timedelta(days=6)        # 7 days back
    prev_end      = current_start - timedelta(days=1)
    prev_start    = prev_end - timedelta(days=6)

    date_from_str = prev_start.strftime("%Y-%m-%d")
    date_to_str   = current_end.strftime("%Y-%m-%d")

    print(f"\nWeekly Performance Report")
    print(f"  Current:  {current_start} → {current_end}")
    print(f"  Previous: {prev_start} → {prev_end}")
    print("=" * 70)

    accounts = get_client_accounts(client)
    if not accounts:
        print("No client accounts found.")
        return

    for account in accounts:
        print(f"\n  Account: {account['name']} ({account['id']})")
        daily_rows = get_daily_performance(client, account["id"], date_from_str, date_to_str)

        if not daily_rows:
            print("    No data.")
            continue

        curr_rows = [r for r in daily_rows if current_start.isoformat() <= r["date"] <= current_end.isoformat()]
        curr_spend = sum(r["cost"] for r in curr_rows)
        curr_conv  = sum(r["conversions"] for r in curr_rows)
        print(f"    Days with data: {len(set(r['date'] for r in daily_rows))} | "
              f"Current spend: £{curr_spend:,.2f} | Conversions: {curr_conv:,.1f}")

        md_path = write_performance_report(
            base          = OBSIDIAN_BASE,
            account_name  = account["name"],
            daily_rows    = daily_rows,
            current_start = current_start,
            current_end   = current_end,
            prev_start    = prev_start,
            prev_end      = prev_end,
        )
        print(f"    → {md_path}")

    print("\n" + "=" * 70)
    print("Done.")


if __name__ == "__main__":
    main()
