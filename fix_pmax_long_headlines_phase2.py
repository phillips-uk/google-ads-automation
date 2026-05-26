"""
Phase 2 — Link newly created long headline assets to asset groups.

The 4 long headline assets were already created in phase 1:
  362228698819 — Fine Jewellery Handcrafted in London's Hatton Garden
  362228698822 — Jewellery as Precious as the Planet That Inspires It
  362228698825 — Beautifully Packaged Fine Jewellery with Free UK Delivery
  362228698828 — Ethically Crafted Pieces Inspired by the Natural World

Phase 1 linking failed because 4 sparse groups (campaigns #, #2, #3, #4)
each have only 1 short headline. PMax requires ≥3 per asset group for
mutations to succeed. This script handles two cases:

  Group 6449944427 (PMax #5) — fully configured (5 headlines). Link only.
  Groups #, #2, #3, #4 (sparse) — add 4 short headlines + 4 long headlines
                                    in one batch per group.

Usage:
  python3 fix_pmax_long_headlines_phase2.py          # dry run
  python3 fix_pmax_long_headlines_phase2.py --apply  # push live
"""

import sys
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, "/Users/lewisphillips/Projects/google-ads-automation")
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

CUSTOMER_ID = "9364748087"

# ── Asset resource names created in phase 1 ────────────────────────────────────

LONG_HEADLINE_ASSETS = {
    "customers/9364748087/assets/362228698819": "Fine Jewellery Handcrafted in London's Hatton Garden",
    "customers/9364748087/assets/362228698822": "Jewellery as Precious as the Planet That Inspires It",
    "customers/9364748087/assets/362228698825": "Beautifully Packaged Fine Jewellery with Free UK Delivery",
    "customers/9364748087/assets/362228698828": "Ethically Crafted Pieces Inspired by the Natural World",
}

# Short headlines to fill out sparse groups (these also exist as assets in the
# main group, but creating new text assets for the same text is fine — Google
# deduplicates by text content at the library level).
SHORT_HEADLINES_FOR_SPARSE = [
    "Lee Renée Jewellery",
    "Free Delivery On All Orders",
    "Join Mailing List For 10% Off",
    "Buy Now Pay Later Available",
]

# Main group — fully configured, just needs the long headline links
MAIN_GROUP_ID = "6449944427"  # PMax: Shopping-Smart-Lewis #5

# Sparse groups — need short headlines AND long headline links
SPARSE_GROUP_IDS = [
    "6450282233",  # PMax: Shopping-Smart-Lewis #3
    "6450282320",  # PMax: Shopping-Smart-Lewis
    "6450282287",  # PMax: Shopping-Smart-Lewis #4
    "6450296197",  # PMax: Shopping-Smart-Lewis #2
]


def _build_long_headline_link_ops(client, group_id, asset_resources):
    """Build AssetGroupAssetOperation objects to link long headline assets."""
    ops = []
    for asset_resource in asset_resources:
        op = client.get_type("AssetGroupAssetOperation")
        aga = op.create
        aga.asset_group = f"customers/{CUSTOMER_ID}/assetGroups/{group_id}"
        aga.asset = asset_resource
        aga.field_type = client.enums.AssetFieldTypeEnum.LONG_HEADLINE
        ops.append(op)
    return ops


def main(apply: bool = False):
    print("\nPMax Long Headlines — Phase 2: Linking")
    print("=" * 60)
    print(f"\nMode: {'⚡ APPLY (live changes)' if apply else '🔍 DRY RUN (no changes)'}")

    print("\n[A] Main group (6449944427) — link 4 long headlines only")
    for res, text in LONG_HEADLINE_ASSETS.items():
        asset_id = res.split("/")[-1]
        print(f"    + LONG_HEADLINE asset {asset_id}: {text}")

    print(f"\n[B] Sparse groups ({len(SPARSE_GROUP_IDS)}) — add short headlines + long headlines")
    for gid in SPARSE_GROUP_IDS:
        print(f"\n  Group {gid}:")
        for h in SHORT_HEADLINES_FOR_SPARSE:
            print(f"    + HEADLINE: {h}")
        for text in LONG_HEADLINE_ASSETS.values():
            print(f"    + LONG_HEADLINE: {text}")

    if not apply:
        print("\n──────────────────────────────────────────────────────────")
        print("Dry run complete. Run with --apply to push changes live.")
        return

    client = GoogleAdsClient.load_from_storage(
        "/Users/lewisphillips/Projects/google-ads-automation/google-ads.yaml",
        version="v24",
    )
    aga_service   = client.get_service("AssetGroupAssetService")
    asset_service = client.get_service("AssetService")

    # ── A: Main group — link long headlines only ───────────────────────────────
    print("\n[A] Linking long headlines to main group 6449944427...")
    ops = _build_long_headline_link_ops(
        client, MAIN_GROUP_ID, list(LONG_HEADLINE_ASSETS.keys())
    )
    try:
        resp = aga_service.mutate_asset_group_assets(
            customer_id=CUSTOMER_ID, operations=ops
        )
        print(f"  ✅ {len(resp.results)} long headline links created for main group.")
    except GoogleAdsException as ex:
        msg = ex.failure.errors[0].message if ex.failure.errors else str(ex)
        print(f"  ❌ Failed: {msg}")

    # ── B: Sparse groups — create short headline assets, then link all ─────────
    print(f"\n[B] Processing {len(SPARSE_GROUP_IDS)} sparse groups...")

    for group_id in SPARSE_GROUP_IDS:
        print(f"\n  Group {group_id}:")

        # Step 1 — Create short headline text assets for this group
        sh_ops = []
        for text in SHORT_HEADLINES_FOR_SPARSE:
            op = client.get_type("AssetOperation")
            op.create.text_asset.text = text
            sh_ops.append(op)

        try:
            sh_resp = asset_service.mutate_assets(
                customer_id=CUSTOMER_ID, operations=sh_ops
            )
            sh_resources = [r.resource_name for r in sh_resp.results]
            print(f"    ✅ Created {len(sh_resources)} short headline assets")
        except GoogleAdsException as ex:
            msg = ex.failure.errors[0].message if ex.failure.errors else str(ex)
            print(f"    ❌ Asset creation failed: {msg}")
            continue

        # Step 2 — Build link operations: short headlines + long headlines
        link_ops = []

        # Short headline links
        for asset_res in sh_resources:
            op = client.get_type("AssetGroupAssetOperation")
            aga = op.create
            aga.asset_group = f"customers/{CUSTOMER_ID}/assetGroups/{group_id}"
            aga.asset = asset_res
            aga.field_type = client.enums.AssetFieldTypeEnum.HEADLINE
            link_ops.append(op)

        # Long headline links
        link_ops += _build_long_headline_link_ops(
            client, group_id, list(LONG_HEADLINE_ASSETS.keys())
        )

        # Step 3 — Submit all links in one batch
        try:
            link_resp = aga_service.mutate_asset_group_assets(
                customer_id=CUSTOMER_ID, operations=link_ops
            )
            sh_count = len(sh_resources)
            lh_count = len(LONG_HEADLINE_ASSETS)
            print(f"    ✅ {len(link_resp.results)} links created "
                  f"({sh_count} headlines + {lh_count} long headlines)")
        except GoogleAdsException as ex:
            msg = ex.failure.errors[0].message if ex.failure.errors else str(ex)
            print(f"    ❌ Linking failed: {msg}")

    print("\n" + "=" * 60)
    print("Phase 2 complete. Full summary:")
    print("  Main group 6449944427: 1 → 5 long headlines")
    print("  Sparse groups: 1 → 5 short headlines, 1 → 5 long headlines")
    print("Allow 24–48 h for performance data to populate.")


if __name__ == "__main__":
    apply_mode = "--apply" in sys.argv
    main(apply=apply_mode)
