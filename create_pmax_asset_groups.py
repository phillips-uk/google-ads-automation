"""
Create PMax Asset Groups — Earrings & Bracelets
Lee Renée Jewellery | Shopping-Smart-Lewis #6 (campaign ID 19771041830)
Created: 2026-05-19

Phase approach (required by Google Ads API validation):
  Phase 1 — Upload images + create text assets → collect real resource names
  Phase 2 — Create asset group + link ALL assets in one batch (group is complete from day 1)
  Phase 3 — Create listing group filters
"""

import base64
import io
import warnings
import requests
from PIL import Image

warnings.filterwarnings("ignore")

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# ── Config ────────────────────────────────────────────────────────────────────
CUSTOMER_ID = "9364748087"
CAMPAIGN_ID = "19771041830"
CAMPAIGN_RESOURCE = f"customers/{CUSTOMER_ID}/campaigns/{CAMPAIGN_ID}"
BUSINESS_NAME = "Lee Renée"

# ── Asset group definitions ────────────────────────────────────────────────────
ASSET_GROUPS = {
    "Earrings": {
        "name": "Earrings",
        "final_url": "https://www.leerenee.com/collections/earrings",
        "product_type": "earrings",
        "headlines": [
            "Handmade Silver Earrings",
            "Designer Drop Earrings",
            "Sterling Silver & Gold",
            "Nature-Inspired Designs",
            "Earrings by Lee Renée",
        ],
        "long_headlines": [
            "Handmade sterling silver & gold earrings, designed in the UK. Free delivery over £50.",
            "Stud earrings, hoops & drops in sterling silver, gold plated and 9ct gold.",
            "Designer earrings handcrafted in the UK. Nature-inspired studs, drops & hoop earrings.",
        ],
        "descriptions": [
            "Handmade designer earrings in sterling silver, gold plated & 9ct solid gold. UK delivery.",
            "From delicate studs to statement drops — each piece handcrafted in sterling silver & gold.",
        ],
        "images": [
            {
                "url": "https://cdn.shopify.com/s/files/1/0558/7245/4823/products/heart-earrings-gold-model-hires.jpg",
                "name": "heart-earrings-gold-model",
                "field_type": "MARKETING_IMAGE",
            },
            {
                "url": "https://cdn.shopify.com/s/files/1/0558/7245/4823/products/heart-earrings-silver-mdoel-hires.jpg",
                "name": "heart-earrings-silver-model",
                "field_type": "SQUARE_MARKETING_IMAGE",
            },
            {
                "url": "https://cdn.shopify.com/s/files/1/0558/7245/4823/products/earrings-scl-juliet-silver-modelshot-hires.jpg",
                "name": "juliet-earrings-silver-model",
                "field_type": "SQUARE_MARKETING_IMAGE",
            },
            {
                "url": "https://cdn.shopify.com/s/files/1/0558/7245/4823/products/earrings-scl-juliet-rg-modelshot-hires.jpg",
                "name": "juliet-earrings-rg-model",
                "field_type": "SQUARE_MARKETING_IMAGE",
            },
            {
                "url": "https://cdn.shopify.com/s/files/1/0558/7245/4823/products/Heloise_earrings_gold_model_shot_hi_res.jpg",
                "name": "heloise-earrings-gold-model",
                "field_type": "SQUARE_MARKETING_IMAGE",
            },
            {
                "url": "https://cdn.shopify.com/s/files/1/0558/7245/4823/products/model_shot_tourmaline_2.jpg",
                "name": "tourmaline-leaf-earrings-model",
                "field_type": "SQUARE_MARKETING_IMAGE",
            },
        ],
    },
    "Bracelets": {
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
            # Approved version (84 chars — "solid" dropped)
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
    },
}


# ── Image helpers ─────────────────────────────────────────────────────────────

def download_and_prepare_image(url: str, field_type: str) -> bytes:
    """
    Download image and prepare for Google Ads:
    - MARKETING_IMAGE:      centre-crop to 1.91:1, upscale to min 600x314
    - SQUARE_MARKETING_IMAGE: centre-crop to 1:1, upscale to min 300x300
    """
    print(f"  Downloading: {url.split('/')[-1]}")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    w, h = img.size

    if field_type == "MARKETING_IMAGE":
        target_ratio = 1.91
        current_ratio = w / h
        if abs(current_ratio - target_ratio) > 0.05:
            new_w = w
            new_h = int(w / target_ratio)
            if new_h > h:
                new_h = h
                new_w = int(h * target_ratio)
            left = (w - new_w) // 2
            top = (h - new_h) // 2
            img = img.crop((left, top, left + new_w, top + new_h))
            print(f"    Cropped {w}x{h} → {img.size[0]}x{img.size[1]} (1.91:1)")
        if img.size[0] < 600 or img.size[1] < 314:
            img = img.resize((1200, 628), Image.LANCZOS)
            print(f"    Upscaled to 1200x628")

    elif field_type == "SQUARE_MARKETING_IMAGE":
        current_ratio = w / h
        if abs(current_ratio - 1.0) > 0.05:
            # Centre-crop to square
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            img = img.crop((left, top, left + side, top + side))
            print(f"    Cropped {w}x{h} → {img.size[0]}x{img.size[1]} (1:1)")
        if img.size[0] < 300:
            img = img.resize((300, 300), Image.LANCZOS)
            print(f"    Upscaled to 300x300")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def upload_image_asset(client, image_bytes: bytes, name: str) -> str:
    """Upload image to Google Ads asset library. Returns asset resource name."""
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
    """Create a text asset in the library. Returns resource name."""
    asset_service = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    op.create.text_asset.text = text
    resp = asset_service.mutate_assets(customer_id=CUSTOMER_ID, operations=[op])
    return resp.results[0].resource_name


# ── Phase 1 — Pre-upload all assets ──────────────────────────────────────────

def phase1_upload_assets(client, group_config: dict) -> dict:
    """
    Upload all images and create all text assets.
    Returns dict with resource names keyed by field_type / label.
    """
    name = group_config["name"]
    print(f"\n--- Phase 1: uploading assets for {name} ---")
    assets = {
        "headlines": [],
        "long_headlines": [],
        "descriptions": [],
        "images": [],  # list of (resource_name, field_type)
    }

    # Text assets
    print("  Creating text assets...")
    for text in group_config["headlines"]:
        rn = create_text_asset(client, text)
        assets["headlines"].append(rn)

    for text in group_config["long_headlines"]:
        rn = create_text_asset(client, text)
        assets["long_headlines"].append(rn)

    for text in group_config["descriptions"]:
        rn = create_text_asset(client, text)
        assets["descriptions"].append(rn)

    # NOTE: Business name is NOT created here.
    # Brand Guidelines is enabled on this campaign — business name and logo
    # are managed at the CampaignAsset level, not the asset group level.

    print(f"  Created {len(assets['headlines'])} headlines, "
          f"{len(assets['long_headlines'])} long headlines, "
          f"{len(assets['descriptions'])} descriptions")

    # Image assets
    print("  Uploading images...")
    for img_cfg in group_config["images"]:
        image_bytes = download_and_prepare_image(img_cfg["url"], img_cfg["field_type"])
        rn = upload_image_asset(client, image_bytes, img_cfg["name"])
        assets["images"].append((rn, img_cfg["field_type"]))

    print(f"  Uploaded {len(assets['images'])} images")
    return assets


# ── Phase 2 — Create asset group and link all assets ─────────────────────────

def phase2_create_asset_group(client, group_config: dict, assets: dict) -> str:
    """
    Create the asset group and link ALL pre-created assets in a single mutate.
    Returns the new asset group resource name.
    """
    name = group_config["name"]
    print(f"\n--- Phase 2: creating asset group '{name}' ---")
    ga_service = client.get_service("GoogleAdsService")

    tmp_ag = f"customers/{CUSTOMER_ID}/assetGroups/-1"
    operations = []

    # Create asset group
    ag_op = client.get_type("MutateOperation")
    ag = ag_op.asset_group_operation.create
    ag.resource_name = tmp_ag
    ag.name = name
    ag.campaign = CAMPAIGN_RESOURCE
    ag.final_urls.append(group_config["final_url"])
    ag.status = client.enums.AssetGroupStatusEnum.ENABLED
    operations.append(ag_op)

    # Helper to make an AssetGroupAsset link operation
    def link_op(asset_rn: str, field_type_str: str):
        op = client.get_type("MutateOperation")
        aga = op.asset_group_asset_operation.create
        aga.asset_group = tmp_ag
        aga.asset = asset_rn
        aga.field_type = getattr(client.enums.AssetFieldTypeEnum, field_type_str)
        return op

    for rn in assets["headlines"]:
        operations.append(link_op(rn, "HEADLINE"))

    for rn in assets["long_headlines"]:
        operations.append(link_op(rn, "LONG_HEADLINE"))

    for rn in assets["descriptions"]:
        operations.append(link_op(rn, "DESCRIPTION"))

    for img_rn, img_field_type in assets["images"]:
        operations.append(link_op(img_rn, img_field_type))

    print(f"  Submitting {len(operations)} operations...")
    try:
        response = ga_service.mutate(
            customer_id=CUSTOMER_ID,
            mutate_operations=operations,
        )
        ag_resource = None
        for r in response.mutate_operation_responses:
            if r.HasField("asset_group_result"):
                ag_resource = r.asset_group_result.resource_name
                print(f"  ✅ Asset group created: {ag_resource}")
                break
        return ag_resource
    except GoogleAdsException as ex:
        print(f"  ❌ ERROR creating asset group '{name}':")
        for error in ex.failure.errors:
            print(f"    [{error.error_code}] {error.message}")
            for el in error.location.field_path_elements:
                print(f"      field: {el.field_name}, index: {el.index}")
        raise


# ── Phase 3 — Create listing group filters ────────────────────────────────────

def phase3_listing_group_filters(client, ag_resource: str, product_type: str) -> None:
    """
    Create SUBDIVISION root + UNIT_INCLUDED (product_type) + UNIT_EXCLUDED (catch-all).
    Mirrors the pattern used by the existing Necklaces and Rings asset groups.
    """
    print(f"\n--- Phase 3: listing group filters (product_type={product_type}) ---")
    ga_service = client.get_service("GoogleAdsService")
    ag_id = ag_resource.split("/")[-1]
    operations = []

    root_tmp = f"customers/{CUSTOMER_ID}/assetGroupListingGroupFilters/{ag_id}~-1"

    # Root SUBDIVISION
    root_op = client.get_type("MutateOperation")
    root = root_op.asset_group_listing_group_filter_operation.create
    root.resource_name = root_tmp
    root.asset_group = ag_resource
    root.type_ = client.enums.ListingGroupFilterTypeEnum.SUBDIVISION
    operations.append(root_op)

    # UNIT_INCLUDED — this product type
    incl_op = client.get_type("MutateOperation")
    incl = incl_op.asset_group_listing_group_filter_operation.create
    incl.asset_group = ag_resource
    incl.type_ = client.enums.ListingGroupFilterTypeEnum.UNIT_INCLUDED
    incl.parent_listing_group_filter = root_tmp
    incl.case_value.product_type.level = (
        client.enums.ListingGroupFilterProductTypeLevelEnum.LEVEL1
    )
    incl.case_value.product_type.value = product_type
    operations.append(incl_op)

    # UNIT_EXCLUDED — everything else
    excl_op = client.get_type("MutateOperation")
    excl = excl_op.asset_group_listing_group_filter_operation.create
    excl.asset_group = ag_resource
    excl.type_ = client.enums.ListingGroupFilterTypeEnum.UNIT_EXCLUDED
    excl.parent_listing_group_filter = root_tmp
    excl.case_value.product_type.level = (
        client.enums.ListingGroupFilterProductTypeLevelEnum.LEVEL1
    )
    excl.case_value.product_type.value = ""
    operations.append(excl_op)

    try:
        response = ga_service.mutate(
            customer_id=CUSTOMER_ID,
            mutate_operations=operations,
        )
        for r in response.mutate_operation_responses:
            if r.HasField("asset_group_listing_group_filter_result"):
                print(f"  Filter: {r.asset_group_listing_group_filter_result.resource_name}")
        print("  ✅ Listing group filters created")
    except GoogleAdsException as ex:
        print(f"  ❌ ERROR creating listing group filters:")
        for error in ex.failure.errors:
            print(f"    {error.message}")
        raise


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Initialising Google Ads client...")
    client = GoogleAdsClient.load_from_storage("google-ads.yaml")

    for group_name, group_config in ASSET_GROUPS.items():
        print(f"\n{'='*60}")
        print(f"BUILDING: {group_name}")
        print(f"{'='*60}")
        uploaded_assets = phase1_upload_assets(client, group_config)
        ag_resource = phase2_create_asset_group(client, group_config, uploaded_assets)
        phase3_listing_group_filters(client, ag_resource, group_config["product_type"])
        print(f"\n✅ {group_name} COMPLETE")

    print("\n" + "=" * 60)
    print("ALL DONE — Earrings and Bracelets asset groups created")
    print("Campaign: Shopping-Smart-Lewis #6")
    print("Google begins serving within ~24h after asset review.")
    print("=" * 60)
