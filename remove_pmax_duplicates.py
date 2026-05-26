"""Remove duplicate AssetGroupAsset entries from Lee Renée PMax 'Ad group' asset group."""

import sys
sys.path.insert(0, '/Users/lewisphillips/Projects/google-ads-automation')

from google.ads.googleads.client import GoogleAdsClient

CUSTOMER_ID = "9364748087"

# Resource names to REMOVE — keeping one instance of each unique asset per field type.
# Format: customers/{cid}/assetGroupAssets/{group_id}~{asset_id}~{field_type}
REMOVE_RESOURCES = [
    # 'Nature-Inspired Jewellery' HEADLINE ×6 — keep group 6449944427, remove the other 5
    "customers/9364748087/assetGroupAssets/6450282233~50272195366~HEADLINE",
    "customers/9364748087/assetGroupAssets/6450282320~50272195366~HEADLINE",
    "customers/9364748087/assetGroupAssets/6450282287~50272195366~HEADLINE",
    "customers/9364748087/assetGroupAssets/6450296197~50272195366~HEADLINE",
    "customers/9364748087/assetGroupAssets/6462579053~50272195366~HEADLINE",

    # 'Buy Now Pay Later Available' HEADLINE ×2 — keep 6449944427, remove 6462579053
    "customers/9364748087/assetGroupAssets/6462579053~18436040486~HEADLINE",

    # 'Free Delivery On All Orders' HEADLINE ×2 — keep 6449944427, remove 6462579053
    "customers/9364748087/assetGroupAssets/6462579053~18400898088~HEADLINE",

    # 'Join Mailing List For 10% Off' HEADLINE ×2 — keep 6449944427, remove 6462579053
    "customers/9364748087/assetGroupAssets/6462579053~18400898091~HEADLINE",

    # 'Lee Renée Jewellery' HEADLINE ×2 — keep 6449944427, remove 6462579053
    "customers/9364748087/assetGroupAssets/6462579053~18400898082~HEADLINE",

    # 'Discover Nature-Inspired Jewellery By Lee Renée' LONG_HEADLINE ×6 — keep 6449944427, remove 5
    "customers/9364748087/assetGroupAssets/6450282233~50279739803~LONG_HEADLINE",
    "customers/9364748087/assetGroupAssets/6450282320~50279739803~LONG_HEADLINE",
    "customers/9364748087/assetGroupAssets/6450282287~50279739803~LONG_HEADLINE",
    "customers/9364748087/assetGroupAssets/6450296197~50279739803~LONG_HEADLINE",
    "customers/9364748087/assetGroupAssets/6462579053~50279739803~LONG_HEADLINE",

    # 'Jewellery is as precious as the planet that inspires it.' DESCRIPTION ×2 — keep 6449944427, remove 6462579053
    "customers/9364748087/assetGroupAssets/6462579053~50297768483~DESCRIPTION",

    # 'Lee Renée Jewellery - Handmade In Hatton Garden' DESCRIPTION ×2 — keep 6449944427, remove 6462579053
    "customers/9364748087/assetGroupAssets/6462579053~50297768486~DESCRIPTION",

    # 'Made from Responsibly Sourced FSC-Certified Paper' DESCRIPTION ×2 — keep 6449944427, remove 6462579053
    "customers/9364748087/assetGroupAssets/6462579053~50297768489~DESCRIPTION",

    # 'Timeless Additions To Any Jewellery Collection' DESCRIPTION ×2 — keep 6449944427, remove 6462579053
    "customers/9364748087/assetGroupAssets/6462579053~21174657491~DESCRIPTION",

    # 'To us, jewellery is as precious as the planet that inspires it.' DESCRIPTION ×5 — keep 6449944427, remove 4
    "customers/9364748087/assetGroupAssets/6450282233~50272209877~DESCRIPTION",
    "customers/9364748087/assetGroupAssets/6450282320~50272209877~DESCRIPTION",
    "customers/9364748087/assetGroupAssets/6450282287~50272209877~DESCRIPTION",
    "customers/9364748087/assetGroupAssets/6450296197~50272209877~DESCRIPTION",

    # 'Lee Renée Jewellery' BUSINESS_NAME ×2 — keep 6449944427, remove 6462579053
    "customers/9364748087/assetGroupAssets/6462579053~18400898082~BUSINESS_NAME",
]

def main():
    client = GoogleAdsClient.load_from_storage(
        "/Users/lewisphillips/Projects/google-ads-automation/google-ads.yaml",
        version="v24"
    )

    service = client.get_service("AssetGroupAssetService")

    operations = []
    for resource_name in REMOVE_RESOURCES:
        op = client.get_type("AssetGroupAssetOperation")
        op.remove = resource_name
        operations.append(op)

    print(f"Sending {len(operations)} REMOVE operations...")

    response = service.mutate_asset_group_assets(
        customer_id=CUSTOMER_ID,
        operations=operations,
    )

    print(f"✅ Done. {len(response.results)} assets removed.")
    for result in response.results:
        print(f"  Removed: {result.resource_name}")

if __name__ == "__main__":
    main()
