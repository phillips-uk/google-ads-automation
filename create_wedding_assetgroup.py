"""
Create Wedding & Occasions asset group in Lee Renee PMax campaign.
Filter: product_type = rings (LEVEL1)
Final URL: https://www.leerenee.com/collections/engagement-rings-wedding-rings
Images: reuse existing from campaign
"""
import warnings
warnings.filterwarnings("ignore")
import sys
from config import load_env
load_env()

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

CUSTOMER_ID  = "9364748087"
CAMPAIGN_ID  = "19771041830"
CAMPAIGN_RES = f"customers/{CUSTOMER_ID}/campaigns/{CAMPAIGN_ID}"

client      = GoogleAdsClient.load_from_storage("google-ads.yaml")
ga_service  = client.get_service("GoogleAdsService")
FT          = client.enums.AssetFieldTypeEnum
SHOPPING    = client.enums.ListingGroupFilterListingSourceEnum.SHOPPING

# ── 1. Confirm campaign exists ────────────────────────────────────────────────
resp = ga_service.search(customer_id=CUSTOMER_ID, query=f"""
    SELECT campaign.id, campaign.name FROM campaign
    WHERE campaign.id = {CAMPAIGN_ID}
""")
for row in resp:
    print(f"Campaign: {row.campaign.name} (ID: {row.campaign.id})")

# ── 2. Collect existing image assets to reuse ─────────────────────────────────
resp = ga_service.search(customer_id=CUSTOMER_ID, query=f"""
    SELECT asset_group_asset.asset, asset_group_asset.field_type
    FROM asset_group_asset
    WHERE campaign.id = {CAMPAIGN_ID}
      AND asset.type = 'IMAGE'
""")
marketing_imgs = []
square_imgs    = []
seen = set()
for row in resp:
    rn = row.asset_group_asset.asset
    ft = row.asset_group_asset.field_type
    if rn in seen:
        continue
    seen.add(rn)
    if ft == FT.MARKETING_IMAGE:
        marketing_imgs.append(rn)
    elif ft == FT.SQUARE_MARKETING_IMAGE:
        square_imgs.append(rn)
marketing_imgs = list(dict.fromkeys(marketing_imgs))[:6]
square_imgs    = list(dict.fromkeys(square_imgs))[:6]
print(f"Reusing {len(marketing_imgs)} marketing + {len(square_imgs)} square images")
if not marketing_imgs or not square_imgs:
    sys.exit("No image assets found — aborting")

# ── 3. Pre-upload text assets ─────────────────────────────────────────────────
asset_svc = client.get_service("AssetService")

def upload_text(text: str) -> str:
    op = client.get_type("AssetOperation")
    op.create.text_asset.text = text
    r = asset_svc.mutate_assets(customer_id=CUSTOMER_ID, operations=[op])
    return r.results[0].resource_name

print("\nUploading text assets...")
headline_rns = [upload_text(t) for t in [
    "Wedding Jewellery",
    "Bridal & Wedding Rings",
    "Engagement Ring Gift",
    "Handmade Wedding Rings",
    "Occasion Jewellery Gifts",
]]
lh_rns = [upload_text(t) for t in [
    "Handcrafted Sterling Silver & Gold Wedding Jewellery",
    "Unique Engagement Rings & Bridal Gifts",
    "Celebrate Every Occasion With Fine Jewellery",
]]
desc_rns = [upload_text(t) for t in [
    "Handmade engagement rings & wedding jewellery in sterling silver & gold. Free UK delivery.",
    "Bridal gifts & occasion jewellery handcrafted in the UK. Sterling silver and 9ct gold.",
]]
# Business name is a campaign-level asset (Brand Guidelines enabled) — do not add at asset group level
print(f"  Uploaded: {len(headline_rns)} headlines, {len(lh_rns)} long headlines, {len(desc_rns)} descriptions")

# ── 4. Create asset group + link assets ───────────────────────────────────────
print("\nCreating asset group...")
tmp_ag = f"customers/{CUSTOMER_ID}/assetGroups/-1"
ops = []

ag_op = client.get_type("MutateOperation")
ag = ag_op.asset_group_operation.create
ag.resource_name = tmp_ag
ag.name         = "Wedding & Occasions"
ag.campaign     = CAMPAIGN_RES
ag.status       = client.enums.AssetGroupStatusEnum.ENABLED
ag.final_urls.append("https://www.leerenee.com/collections/engagement-rings-wedding-rings")
ag.final_mobile_urls.append("https://www.leerenee.com/collections/engagement-rings-wedding-rings")
ops.append(ag_op)

def link(asset_rn, field_type):
    op = client.get_type("MutateOperation")
    aga = op.asset_group_asset_operation.create
    aga.asset_group = tmp_ag
    aga.asset       = asset_rn
    aga.field_type  = field_type
    return op

for rn in headline_rns:
    ops.append(link(rn, FT.HEADLINE))
for rn in lh_rns:
    ops.append(link(rn, FT.LONG_HEADLINE))
for rn in desc_rns:
    ops.append(link(rn, FT.DESCRIPTION))
# Business name is a CampaignAsset (Brand Guidelines) — omit from asset group
for rn in marketing_imgs:
    ops.append(link(rn, FT.MARKETING_IMAGE))
for rn in square_imgs:
    ops.append(link(rn, FT.SQUARE_MARKETING_IMAGE))

try:
    response = ga_service.mutate(customer_id=CUSTOMER_ID, mutate_operations=ops)
    ag_rn = None
    for r in response.mutate_operation_responses:
        if r.HasField("asset_group_result"):
            ag_rn = r.asset_group_result.resource_name
            ag_id = ag_rn.split("/")[-1]
            print(f"  Asset group created — ID: {ag_id}, RN: {ag_rn}")
    if not ag_rn:
        sys.exit("No asset group result returned")
except GoogleAdsException as ex:
    print("Error creating asset group:")
    for e in ex.failure.errors:
        print(f"  [{e.error_code}] {e.message}")
        if e.location:
            for fp in e.location.field_path_elements:
                print(f"    field: {fp.field_name} idx: {fp.index}")
    sys.exit(1)

# ── 5. Add listing group filters ──────────────────────────────────────────────
print("\nAdding listing group filters (product_type=rings)...")
root_tmp = f"customers/{CUSTOMER_ID}/assetGroupListingGroupFilters/{ag_id}~-1"
lgf_ops  = []

root_op = client.get_type("MutateOperation")
root    = root_op.asset_group_listing_group_filter_operation.create
root.resource_name  = root_tmp
root.asset_group    = ag_rn
root.type_          = client.enums.ListingGroupFilterTypeEnum.SUBDIVISION
root.listing_source = SHOPPING
lgf_ops.append(root_op)

incl_op = client.get_type("MutateOperation")
incl    = incl_op.asset_group_listing_group_filter_operation.create
incl.asset_group            = ag_rn
incl.type_                  = client.enums.ListingGroupFilterTypeEnum.UNIT_INCLUDED
incl.listing_source         = SHOPPING
incl.parent_listing_group_filter = root_tmp
incl.case_value.product_type.level = client.enums.ListingGroupFilterProductTypeLevelEnum.LEVEL1
incl.case_value.product_type.value = "rings"
lgf_ops.append(incl_op)

excl_op = client.get_type("MutateOperation")
excl    = excl_op.asset_group_listing_group_filter_operation.create
excl.asset_group            = ag_rn
excl.type_                  = client.enums.ListingGroupFilterTypeEnum.UNIT_EXCLUDED
excl.listing_source         = SHOPPING
excl.parent_listing_group_filter = root_tmp
excl.case_value.product_type.level = client.enums.ListingGroupFilterProductTypeLevelEnum.LEVEL1
excl.case_value.product_type.value = ""
lgf_ops.append(excl_op)

try:
    response = ga_service.mutate(customer_id=CUSTOMER_ID, mutate_operations=lgf_ops)
    for r in response.mutate_operation_responses:
        if r.HasField("asset_group_listing_group_filter_result"):
            print(f"  Filter: {r.asset_group_listing_group_filter_result.resource_name}")
    print("\nDone — Wedding & Occasions asset group created successfully")
    print(f"Asset group ID: {ag_id}")
except GoogleAdsException as ex:
    print("Error adding listing group filters:")
    for e in ex.failure.errors:
        print(f"  [{e.error_code}] {e.message}")
        if e.location:
            for fp in e.location.field_path_elements:
                print(f"    field: {fp.field_name} idx: {fp.index}")
    sys.exit(1)
