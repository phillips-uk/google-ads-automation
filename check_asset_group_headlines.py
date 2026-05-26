"""Check headline counts per asset group — active campaigns only."""
import sys
import warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, '/Users/lewisphillips/Projects/google-ads-automation')
from google.ads.googleads.client import GoogleAdsClient
from collections import defaultdict

CUSTOMER_ID = "9364748087"

client = GoogleAdsClient.load_from_storage(
    "/Users/lewisphillips/Projects/google-ads-automation/google-ads.yaml",
    version="v24"
)
ga_service = client.get_service("GoogleAdsService")

# Step 1 — get asset group IDs from ENABLED campaigns only
campaign_query = """
SELECT campaign.id, campaign.name, asset_group.id, asset_group.name
FROM asset_group
WHERE campaign.status = 'ENABLED'
  AND asset_group.status = 'ENABLED'
ORDER BY campaign.id, asset_group.id
"""
asset_group_ids = []
ag_names = {}
campaign_names = {}
for row in ga_service.search(customer_id=CUSTOMER_ID, query=campaign_query):
    gid = str(row.asset_group.id)
    asset_group_ids.append(gid)
    ag_names[gid] = row.asset_group.name
    campaign_names[gid] = row.campaign.name

if not asset_group_ids:
    print("No enabled asset groups found.")
    sys.exit(0)

print(f"Active campaigns with enabled asset groups: {len(asset_group_ids)}")

# Step 2 — pull asset text for those groups
asset_query = """
SELECT
  asset_group.id,
  asset_group_asset.field_type,
  asset.text_asset.text
FROM asset_group_asset
WHERE asset_group.id IN ({})
  AND asset_group_asset.field_type IN ('HEADLINE', 'LONG_HEADLINE', 'DESCRIPTION', 'BUSINESS_NAME')
  AND asset_group_asset.status != 'REMOVED'
ORDER BY asset_group.id, asset_group_asset.field_type, asset.text_asset.text
""".format(",".join(asset_group_ids))

groups = defaultdict(lambda: defaultdict(list))
for row in ga_service.search(customer_id=CUSTOMER_ID, query=asset_query):
    gid = str(row.asset_group.id)
    ft = row.asset_group_asset.field_type.name
    text = row.asset.text_asset.text
    if text:
        groups[gid][ft].append(text)

for gid in asset_group_ids:
    g = groups[gid]
    print(f"\nGroup {gid} — '{ag_names.get(gid,'?')}' (Campaign: {campaign_names.get(gid,'?')}):")
    for ft in ['HEADLINE', 'LONG_HEADLINE', 'DESCRIPTION', 'BUSINESS_NAME']:
        items = g.get(ft, [])
        print(f"  {ft}: {len(items)} — {items}")
