"""
Create PMax Asset Groups — Earrings (filters only) & Bracelets (full)
Lee Renée Jewellery | Shopping-Smart-Lewis #6

State after previous runs:
  - Earrings (ID: 6714504847): fully populated with assets, missing listing group filters
  - Bracelets: not yet created

This script:
  Step 1 — Add listing group filters to the existing Earrings group
  Step 2 — Create Bracelets asset group (Phase 1: upload assets, Phase 2: create group + link)
  Step 3 — Add listing group filters to Bracelets
"""

import base64
import io
import warnings
import requests
from PIL import Image

warnings.filterwarnings("ignore")

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

CUSTOMER_ID = "9364748087"
CAMPAIGN_ID = "19771041830"
CAMPAIGN_RESOURCE = f"customers/{CUSTOMER_ID}/campaigns/{CAMPAIGN_ID}"
EARRINGS_AG_ID = "6714504847"
EARRINGS_AG_RESOURCE = f"customers/{CUSTOMER_ID}/assetGroups/{EARRINGS_AG_ID}"

BRACELETS_CONFIG = {
    "name": "Bracelets",
    "final_url": "https://www.leerenee.com/collections/bracelets",
    "product_type": "bracelets",
    "headlines": [
        "Handmade Silver Bracelets",
        "Designer Gold Bracelets",
        "Sterling Silver & Gold",
        "Bangles, Cuffs & Charms",
        "Bracelets by Lee Renée",
    ],
    "long_headlines": [
        "Handmade sterling silver & gold bracelets, designed in the UK. Free delivery over £50.",
        "Bangles, charm bracelets & cuffs in sterling silver, gold plated and 9ct solid gold.",
        "Designer bracelets handcrafted in the UK. Nature-inspired pieces in silver & gold.",
    ],
    "descriptions": [
        "Handmade designer bracelets in sterling silver, gold plated & 9ct gold. UK delivery.",
        "From delicate charm bracelets to bold bangles — handcrafted in sterling silver & gold.",
    ],
    "images": [
        {
            "url": "https://cdn.shopify.com/s/files/1/0558/7245/4823/products/gold_bracelet_model_shot_1500px.jpg",
            "name": "halo-bracelet-gold-model",
            "field_type": "MARKETING_IMAGE",
        },
        {
            "url": "https://cdn.shopify.com/s/files/1/0558/7245/4823/products/silver_bracelet_model_1500px.jpg",
            "name": "halo-bracelet-silver-model",
            "field_type": "SQUARE_MARKETING_IMAGE",
        },
        {
            "url": "https://cdn.shopify.com/s/files/1/0558/7245/4823/products/modle_shot_2_1500px_square.jpg",
            "name": "ladybird-bracelet-gold-model",
            "field_type": "SQUARE_MARKETING_IMAGE",
        },
        {
            "url": "https://cdn.shopify.com/s/files/1/0558/7245/4823/products/modle_shot_1_1500px_a8827048-70ee-4fd1-bd80-cd4f86af3d77.jpg",
            "name": "snake-bangle-emerald-model",
            "field_type": "SQUARE_MARKETING_IMAGE",
        },
        {
            "url": "https://cdn.shopify.com/s/files/1/0558/7245/4823/products/model_shot_1_785e2afb-21da-4890-acf4-3a89999a238e.jpg",
            "name": "swallow-bracelet-model",
            "field_type": "SQUARE_MARKETING_IMAGE",
        },
        {
            "url": "https://cdn.shopify.com/s/files/1/0558/7245/4823/files/Lucky-Bracelets.jpg",
            "name": "lucky-bracelets-group",
            "field_type": "SQUARE_MARKETING_IMAGE",
        },
    ],
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def download_and_prepare_image(url: str, field_type: str) -> bytes:
    print(f"  Downloading: {url.split('/')[-1]}")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    w, h = img.size

    if field_type == "MARKETING_IMAGE":
        target_ratio = 1.91
        if abs(w / h - target_ratio) > 0.05:
            new_w, new_h = w, int(w / target_ratio)
            if new_h > h:
                new_h, new_w = h, int(h * target_ratio)
            left, top = (w - new_w) // 2, (h - new_h) // 2
            img = img.crop((left, top, left + new_w, top + new_h))
            print(f"    Cropped {w}x{h} → {img.size[0]}x{img.size[1]} (1.91:1)")
        if img.size[0] < 600 or img.size[1] < 314:
            img = img.resize((1200, 628), Image.LANCZOS)
            print(f"    Upscaled to 1200x628")

    elif field_type == "SQUARE_MARKETING_IMAGE":
        if abs(w / h - 1.0) > 0.05:
            side = min(w, h)
            left, top = (w - side) // 2, (h - side) // 2
            img = img.crop((left, top, left + side, top + side))
            print(f"    Cropped {w}x{h} → {img.size[0]}x{img.size[1]} (1:1)")
        if img.size[0] < 300:
            img = img.resize((300, 300), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def upload_image_asset(client, image_bytes: bytes, name: str) -> str:
    asset_service = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    asset = op.create
    asset.name = name
    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = base64.b64encode(image_bytes).decode("utf-8")
    resp = asset_service.mutate_assets(customer_id=CUSTOMER_ID, operations=[op])
    rn = resp.results[0].resource_name
    print(f"    → {rn}")
    return rn


def create_text_asset(client, text: str) -> str:
    asset_service = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    op.create.text_asset.text = text
    resp = asset_service.mutate_assets(customer_id=CUSTOMER_ID, operations=[op])
    return resp.results[0].resource_name


def add_listing_group_filters(client, ag_resource: str, product_type: str, label: str) -> None:
    """Create SUBDIVISION root + UNIT_INCLUDED + UNIT_EXCLUDED for the given asset group."""
    print(f"\n--- Adding listing group filters to {label} (product_type={product_type}) ---")
    ga_service = client.get_service("GoogleAdsService")
    ag_id = ag_resource.split("/")[-1]
    operations = []

    root_tmp = f"customers/{CUSTOMER_ID}/assetGroupListingGroupFilters/{ag_id}~-1"

    SHOPPING = client.enums.ListingGroupFilterListingSourceEnum.SHOPPING

    root_op = client.get_type("MutateOperation")
    root = root_op.asset_group_listing_group_filter_operation.create
    root.resource_name = root_tmp
    root.asset_group = ag_resource
    root.type_ = client.enums.ListingGroupFilterTypeEnum.SUBDIVISION
    root.listing_source = SHOPPING
    operations.append(root_op)

    incl_op = client.get_type("MutateOperation")
    incl = incl_op.asset_group_listing_group_filter_operation.create
    incl.asset_group = ag_resource
    incl.type_ = client.enums.ListingGroupFilterTypeEnum.UNIT_INCLUDED
    incl.listing_source = SHOPPING
    incl.parent_listing_group_filter = root_tmp
    incl.case_value.product_type.level = (
        client.enums.ListingGroupFilterProductTypeLevelEnum.LEVEL1
    )
    incl.case_value.product_type.value = product_type
    operations.append(incl_op)

    excl_op = client.get_type("MutateOperation")
    excl = excl_op.asset_group_listing_group_filter_operation.create
    excl.asset_group = ag_resource
    excl.type_ = client.enums.ListingGroupFilterTypeEnum.UNIT_EXCLUDED
    excl.listing_source = SHOPPING
    excl.parent_listing_group_filter = root_tmp
    excl.case_value.product_type.level = (
        client.enums.ListingGroupFilterProductTypeLevelEnum.LEVEL1
    )
    excl.case_value.product_type.value = ""
    operations.append(excl_op)

    try:
        response = ga_service.mutate(customer_id=CUSTOMER_ID, mutate_operations=operations)
        for r in response.mutate_operation_responses:
            if r.HasField("asset_group_listing_group_filter_result"):
                print(f"  Filter: {r.asset_group_listing_group_filter_result.resource_name}")
        print(f"  ✅ Listing group filters added to {label}")
    except GoogleAdsException as ex:
        print(f"  ❌ ERROR adding filters to {label}:")
        for error in ex.failure.errors:
            print(f"    [{error.error_code}] {error.message}")
        raise


def create_bracelets_group(client) -> str:
    """Upload all Bracelets assets then create the group in one batch. Returns resource name."""
    cfg = BRACELETS_CONFIG
    print(f"\n{'='*60}")
    print(f"BUILDING: Bracelets")
    print(f"{'='*60}")

    # Phase 1 — upload assets
    print("\n--- Phase 1: uploading assets ---")
    print("  Creating text assets...")
    headline_rns = [create_text_asset(client, t) for t in cfg["headlines"]]
    lh_rns = [create_text_asset(client, t) for t in cfg["long_headlines"]]
    desc_rns = [create_text_asset(client, t) for t in cfg["descriptions"]]
    print(f"  Created {len(headline_rns)} headlines, {len(lh_rns)} long headlines, {len(desc_rns)} descriptions")

    print("  Uploading images...")
    image_rns = []
    for img_cfg in cfg["images"]:
        image_bytes = download_and_prepare_image(img_cfg["url"], img_cfg["field_type"])
        rn = upload_image_asset(client, image_bytes, img_cfg["name"])
        image_rns.append((rn, img_cfg["field_type"]))
    print(f"  Uploaded {len(image_rns)} images")

    # Phase 2 — create group + link all assets
    print("\n--- Phase 2: creating asset group ---")
    ga_service = client.get_service("GoogleAdsService")
    tmp_ag = f"customers/{CUSTOMER_ID}/assetGroups/-1"
    operations = []

    ag_op = client.get_type("MutateOperation")
    ag = ag_op.asset_group_operation.create
    ag.resource_name = tmp_ag
    ag.name = cfg["name"]
    ag.campaign = CAMPAIGN_RESOURCE
    ag.final_urls.append(cfg["final_url"])
    ag.status = client.enums.AssetGroupStatusEnum.ENABLED
    operations.append(ag_op)

    def link(asset_rn, field_type_str):
        op = client.get_type("MutateOperation")
        aga = op.asset_group_asset_operation.create
        aga.asset_group = tmp_ag
        aga.asset = asset_rn
        aga.field_type = getattr(client.enums.AssetFieldTypeEnum, field_type_str)
        return op

    for rn in headline_rns:
        operations.append(link(rn, "HEADLINE"))
    for rn in lh_rns:
        operations.append(link(rn, "LONG_HEADLINE"))
    for rn in desc_rns:
        operations.append(link(rn, "DESCRIPTION"))
    for img_rn, img_ft in image_rns:
        operations.append(link(img_rn, img_ft))

    print(f"  Submitting {len(operations)} operations...")
    try:
        response = ga_service.mutate(customer_id=CUSTOMER_ID, mutate_operations=operations)
        ag_resource = None
        for r in response.mutate_operation_responses:
            if r.HasField("asset_group_result"):
                ag_resource = r.asset_group_result.resource_name
                print(f"  ✅ Asset group created: {ag_resource}")
                break
        return ag_resource
    except GoogleAdsException as ex:
        print(f"  ❌ ERROR creating Bracelets group:")
        for error in ex.failure.errors:
            print(f"    [{error.error_code}] {error.message}")
            for el in error.location.field_path_elements:
                print(f"      {el.field_name}[{el.index}]")
        raise


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Initialising Google Ads client...")
    client = GoogleAdsClient.load_from_storage("google-ads.yaml")

    # Earrings is already complete (group + assets + listing group filters)
    print("\n✅ Earrings — already complete (ID: 6714504847, filters created)")

    # Create Bracelets group (full)
    bracelets_resource = create_bracelets_group(client)

    # Add listing group filters to Bracelets
    add_listing_group_filters(
        client,
        ag_resource=bracelets_resource,
        product_type="bracelets",
        label="Bracelets",
    )

    print("\n" + "="*60)
    print("ALL DONE")
    print("  Earrings  → https://www.leerenee.com/collections/earrings (ID: 6714504847)")
    bracelets_id = bracelets_resource.split("/")[-1] if bracelets_resource else "?"
    print(f"  Bracelets → https://www.leerenee.com/collections/bracelets (ID: {bracelets_id})")
    print("  Campaign: Shopping-Smart-Lewis #6")
    print("  Google begins serving within ~24h after asset review.")
    print("="*60)
