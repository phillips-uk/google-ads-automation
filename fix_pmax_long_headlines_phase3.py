"""
Phase 3 — Complete sparse group asset coverage (comprehensive).

The 4 sparse groups (campaigns #, #2, #3, #4) were stripped to near-empty
by a previous duplicate-removal run. They now have only:
  1 short headline + 1 long headline + 1 description + 0 images

Google validates ALL minimum requirements when mutating any asset group:
  ≥3 short headlines, ≥1 long headline, ≥2 descriptions,
  ≥1 square marketing image, ≥1 business name, ≥1 logo.

This script links ALL assets from the main group (6449944427) to each
sparse group in a single batch, satisfying all minimums at once. This
includes the 4 new long headlines already created in phase 1.

Main group (6449944427) is already complete — 5 long headlines live.
This script only touches the 4 sparse groups.

Usage:
  python3 fix_pmax_long_headlines_phase3.py          # dry run
  python3 fix_pmax_long_headlines_phase3.py --apply  # push live
"""

import sys
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, "/Users/lewisphillips/Projects/google-ads-automation")
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

CUSTOMER_ID = "9364748087"

SPARSE_GROUP_IDS = [
    "6450282233",  # PMax: Shopping-Smart-Lewis #3
    "6450282320",  # PMax: Shopping-Smart-Lewis
    "6450282287",  # PMax: Shopping-Smart-Lewis #4
    "6450296197",  # PMax: Shopping-Smart-Lewis #2
]

# All assets from the main group (6449944427) to replicate.
# Format: (resource_name, field_type, description)
# Excludes: REMOVED items that served as canonical for dedup.
# Includes: 4 new long headlines created in phase 1.
ASSETS_TO_LINK = [
    # ── Short headlines (4 new — existing groups already have 'Nature-Inspired') ──
    ("customers/9364748087/assets/18400898082", "HEADLINE",                "Lee Renée Jewellery"),
    ("customers/9364748087/assets/18400898088", "HEADLINE",                "Free Delivery On All Orders"),
    ("customers/9364748087/assets/18400898091", "HEADLINE",                "Join Mailing List For 10% Off"),
    ("customers/9364748087/assets/18436040486", "HEADLINE",                "Buy Now Pay Later Available"),
    # ── Long headlines (4 new) ─────────────────────────────────────────────────
    ("customers/9364748087/assets/362228698819", "LONG_HEADLINE",          "Fine Jewellery Handcrafted in London's Hatton Garden"),
    ("customers/9364748087/assets/362228698822", "LONG_HEADLINE",          "Jewellery as Precious as the Planet That Inspires It"),
    ("customers/9364748087/assets/362228698825", "LONG_HEADLINE",          "Beautifully Packaged Fine Jewellery with Free UK Delivery"),
    ("customers/9364748087/assets/362228698828", "LONG_HEADLINE",          "Ethically Crafted Pieces Inspired by the Natural World"),
    # ── Descriptions (3 new) ───────────────────────────────────────────────────
    ("customers/9364748087/assets/21174657491",  "DESCRIPTION",            "Timeless Additions To Any Jewellery Collection"),
    ("customers/9364748087/assets/50297768486",  "DESCRIPTION",            "Lee Renée Jewellery - Handmade In Hatton Garden"),
    ("customers/9364748087/assets/50297768489",  "DESCRIPTION",            "Made from Responsibly Sourced FSC-Certified Paper"),
    # ── Images ─────────────────────────────────────────────────────────────────
    ("customers/9364748087/assets/21211437924",  "MARKETING_IMAGE",        "Marketing image 1 (1500×785)"),
    ("customers/9364748087/assets/50279831021",  "MARKETING_IMAGE",        "Marketing image 2 (1500×785)"),
    ("customers/9364748087/assets/50329283556",  "MARKETING_IMAGE",        "Marketing image 3 (1500×785)"),
    ("customers/9364748087/assets/21211437921",  "SQUARE_MARKETING_IMAGE", "Square image 1 (1500×1500)"),
    ("customers/9364748087/assets/50288749738",  "SQUARE_MARKETING_IMAGE", "Square image 2 (1500×1500)"),
    ("customers/9364748087/assets/50288759632",  "PORTRAIT_MARKETING_IMAGE", "Portrait image 1 (1200×1500)"),
    ("customers/9364748087/assets/50329063128",  "PORTRAIT_MARKETING_IMAGE", "Portrait image 2 (1200×1500)"),
    # ── Business name, Logo, YouTube, CTA ──────────────────────────────────────
    ("customers/9364748087/assets/18400898082",  "BUSINESS_NAME",          "Lee Renée Jewellery (business name)"),
    ("customers/9364748087/assets/50289707074",  "LOGO",                   "Logo image"),
    ("customers/9364748087/assets/20012362760",  "YOUTUBE_VIDEO",          "YouTube video"),
    ("customers/9364748087/assets/50297768492",  "CALL_TO_ACTION_SELECTION", "Call to Action"),
]


def main(apply: bool = False):
    print("\nPMax Asset Groups — Phase 3: Complete Sparse Groups")
    print("=" * 60)
    print(f"\nMode: {'⚡ APPLY (live changes)' if apply else '🔍 DRY RUN (no changes)'}")
    print(f"\nTargeting {len(SPARSE_GROUP_IDS)} sparse groups:")
    for gid in SPARSE_GROUP_IDS:
        print(f"  {gid}")

    # Count by type
    by_type = {}
    for _, ft, _ in ASSETS_TO_LINK:
        by_type[ft] = by_type.get(ft, 0) + 1
    print(f"\nAssets per group ({len(ASSETS_TO_LINK)} total):")
    for ft, count in by_type.items():
        print(f"  {ft}: {count}")

    print("\nResulting state per sparse group (existing + new):")
    print("  Short headlines:        1 + 4 = 5")
    print("  Long headlines:         1 + 4 = 5")
    print("  Descriptions:           1 + 3 = 4")
    print("  Marketing images:           3")
    print("  Square marketing images:    2")
    print("  Portrait images:            2")
    print("  Business name:              1")
    print("  Logo:                       1")

    if not apply:
        print("\n──────────────────────────────────────────────────────────")
        print("Dry run complete. Run with --apply to push changes live.")
        return

    client = GoogleAdsClient.load_from_storage(
        "/Users/lewisphillips/Projects/google-ads-automation/google-ads.yaml",
        version="v24",
    )
    aga_service = client.get_service("AssetGroupAssetService")

    success_total = 0
    fail_total    = 0

    for group_id in SPARSE_GROUP_IDS:
        print(f"\n  Group {group_id}...")

        ops = []
        for asset_resource, field_type_str, desc in ASSETS_TO_LINK:
            op  = client.get_type("AssetGroupAssetOperation")
            aga = op.create
            aga.asset_group = f"customers/{CUSTOMER_ID}/assetGroups/{group_id}"
            aga.asset       = asset_resource
            aga.field_type  = getattr(client.enums.AssetFieldTypeEnum, field_type_str)
            ops.append(op)

        try:
            resp = aga_service.mutate_asset_group_assets(
                customer_id=CUSTOMER_ID, operations=ops
            )
            print(f"    ✅ {len(resp.results)}/{len(ops)} links created")
            success_total += len(resp.results)
        except GoogleAdsException as ex:
            errs = list({e.message for e in ex.failure.errors})  # deduplicate
            for e in errs:
                print(f"    ❌ {e}")
            fail_total += len(ops)

    print("\n" + "=" * 60)
    if fail_total == 0:
        print(f"✅ All done. {success_total} links created across {len(SPARSE_GROUP_IDS)} groups.")
        print("\nAll 5 PMax asset groups now have full asset coverage:")
        print("  5 short headlines · 5 long headlines · 4 descriptions")
        print("  + marketing images, square images, logo, business name")
        print("\nAllow 24–48 h for performance data to populate.")
    else:
        print(f"Partial: {success_total} succeeded, {fail_total} failed.")


if __name__ == "__main__":
    apply_mode = "--apply" in sys.argv
    main(apply=apply_mode)
