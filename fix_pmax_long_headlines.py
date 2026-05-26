"""
Fix PMax long headlines — Lee Renée Jewellery

Problem
-------
Every active PMax "Ad group" asset group has exactly 1 long headline
("Discover Nature-Inspired Jewellery By Lee Renée") and that same copy
is shared across all 5 campaigns. Google can't optimise creative
variation if there's nothing to rotate.

Fix
---
Add 4 new, distinct long headlines to every active asset group.
Each group goes from 1 → 5 long headlines with coverage across:
  1. Brand/discovery (existing — kept)
  2. Craft/provenance
  3. Brand ethos (derived from existing description copy)
  4. Gift/practical USP
  5. Values/sustainability

Run in dry-run mode first (default), then with --apply to push live.

Usage
-----
  python3 fix_pmax_long_headlines.py           # dry run
  python3 fix_pmax_long_headlines.py --apply   # push to Google Ads API
"""

import sys
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, "/Users/lewisphillips/Projects/google-ads-automation")
from google.ads.googleads.client import GoogleAdsClient

# ── Config ─────────────────────────────────────────────────────────────────────

CUSTOMER_ID = "9364748087"

# Active asset group IDs — one per active PMax campaign.
# PMax #6 (6462579053) is PAUSED — excluded.
ACTIVE_GROUP_IDS = [
    "6449944427",  # PMax: Shopping-Smart-Lewis #5
    "6450282233",  # PMax: Shopping-Smart-Lewis #3
    "6450282320",  # PMax: Shopping-Smart-Lewis
    "6450282287",  # PMax: Shopping-Smart-Lewis #4
    "6450296197",  # PMax: Shopping-Smart-Lewis #2
]

# ── New long headlines ─────────────────────────────────────────────────────────
# All ≤90 chars. Each covers a distinct brand angle.
# The existing headline ("Discover Nature-Inspired Jewellery By Lee Renée")
# is already linked — we only need to create and link these four.

NEW_LONG_HEADLINES = [
    # Craft + provenance — the Hatton Garden detail is genuinely differentiating
    "Fine Jewellery Handcrafted in London's Hatton Garden",

    # Ethos — elevates the existing description copy into a headline statement
    "Jewellery as Precious as the Planet That Inspires It",

    # Gift + practical USP — combines two real benefits in one scannable line
    "Beautifully Packaged Fine Jewellery with Free UK Delivery",

    # Values/sustainability — distinct from ethos, focuses on the making process
    "Ethically Crafted Pieces Inspired by the Natural World",
]

# ── Validation ─────────────────────────────────────────────────────────────────

_MAX_LONG_HEADLINE_CHARS = 90

def _validate():
    errors = []
    for h in NEW_LONG_HEADLINES:
        if len(h) > _MAX_LONG_HEADLINE_CHARS:
            errors.append(f"  TOO LONG ({len(h)} chars): {h}")
    if errors:
        print("VALIDATION FAILED:")
        for e in errors:
            print(e)
        sys.exit(1)


# ── Main ───────────────────────────────────────────────────────────────────────

def main(apply: bool = False):
    _validate()

    print("\nPMax Long Headlines Fix — Lee Renée Jewellery")
    print("=" * 60)
    print(f"\nMode: {'⚡ APPLY (live changes)' if apply else '🔍 DRY RUN (no changes)'}")
    print(f"\nTarget asset groups: {len(ACTIVE_GROUP_IDS)}")
    for gid in ACTIVE_GROUP_IDS:
        print(f"  {gid}")

    print(f"\nNew long headlines to add ({len(NEW_LONG_HEADLINES)}):")
    for i, h in enumerate(NEW_LONG_HEADLINES, 1):
        print(f"  {i}. [{len(h)} chars] {h}")

    print(f"\nResult: each asset group goes from 1 → {1 + len(NEW_LONG_HEADLINES)} long headlines")

    if not apply:
        print("\n──────────────────────────────────────────────────────────")
        print("Dry run complete. Run with --apply to push changes live.")
        return

    # ── Live execution ─────────────────────────────────────────────────────────

    client = GoogleAdsClient.load_from_storage(
        "/Users/lewisphillips/Projects/google-ads-automation/google-ads.yaml",
        version="v24",
    )

    # Step 1 — Create text assets
    print("\n[1/2] Creating text assets...")
    asset_service = client.get_service("AssetService")
    asset_ops = []
    for text in NEW_LONG_HEADLINES:
        op = client.get_type("AssetOperation")
        op.create.text_asset.text = text
        asset_ops.append(op)

    asset_response = asset_service.mutate_assets(
        customer_id=CUSTOMER_ID,
        operations=asset_ops,
    )
    asset_resources = [r.resource_name for r in asset_response.results]
    for res, text in zip(asset_resources, NEW_LONG_HEADLINES):
        print(f"  ✅ Created: {res}")
        print(f"     Text: {text}")

    # Step 2 — Link to all active asset groups
    print(f"\n[2/2] Linking {len(asset_resources)} assets to {len(ACTIVE_GROUP_IDS)} asset groups "
          f"({len(asset_resources) * len(ACTIVE_GROUP_IDS)} link operations)...")

    aga_service = client.get_service("AssetGroupAssetService")
    aga_ops = []
    for asset_resource in asset_resources:
        for group_id in ACTIVE_GROUP_IDS:
            op = client.get_type("AssetGroupAssetOperation")
            aga = op.create
            aga.asset_group = f"customers/{CUSTOMER_ID}/assetGroups/{group_id}"
            aga.asset = asset_resource
            aga.field_type = client.enums.AssetFieldTypeEnum.LONG_HEADLINE
            aga_ops.append(op)

    aga_response = aga_service.mutate_asset_group_assets(
        customer_id=CUSTOMER_ID,
        operations=aga_ops,
    )

    print(f"  ✅ {len(aga_response.results)} asset-group links created.")
    print("\n" + "=" * 60)
    print("Done. Each active asset group now has 5 long headlines.")
    print("Google will begin serving the new variants immediately.")
    print("Allow 24–48 h for performance data to populate.")


if __name__ == "__main__":
    apply_mode = "--apply" in sys.argv
    main(apply=apply_mode)
