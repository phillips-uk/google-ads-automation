"""
Conversion Action Cleanup — Example Client

Goal: archive EVERYTHING except 'Thank You Page Conversion' to give a clean
      slate. We'll rebuild the conversion action set from scratch later.

The Google Ads API restricts what can be mutated:
  - System-generated actions (HIDDEN status, UA imports, call extensions, Smart
    Campaign auto-created types) → READ ONLY via API. Must be archived in the UI.
  - User-created website conversion actions (ENABLED, Type 8) → fully mutable.

This script:
  1. Archives all user-created ENABLED actions except KEEP_ACTION via API
  2. Prints a manual checklist for HIDDEN/system actions that need UI archiving
"""

import os
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf import field_mask_pb2
from config import load_env

load_env()

CUSTOMER_ID = "0000000000"
YAML_FILE   = os.path.join(os.path.dirname(__file__), "google-ads.yaml")

# The one conversion action we are keeping — everything else gets archived.
KEEP_ACTION = "Thank You Page Conversion"

# ── Manual UI checklist ───────────────────────────────────────────────────────
# HIDDEN / system-generated actions the API cannot touch.
# Path: Google Ads → Tools → Measurement → Conversions → select each → Edit → Archive

MANUAL_ARCHIVE = [
    # UA Analytics imports (Type 38/39) — HIDDEN, read-only
    "converstions (All Web Site Data)",
    "Transactions (All Web Site Data)",
    "Smart Goals (All Web Site Data)",
    "Purchase of Product (All Web Site Data)",
    "Business Dev Page Views (All Web Site Data)",
    "3D Printing Page Views (All Web Site Data)",
    "Added To Cart (All Web Site Data)",
    "Laser Cutting Page View (All Web Site Data)",
    "View Co Working Fixed Desk (All Web Site Data)",
    "Application Downloaded (All Web Site Data)",
    "Form completed made contact (All Web Site Data)",
    "Co Working Trial View (All Web Site Data)",
    "Additive manufacturing Views (All Web Site Data)",
    "3D Print Quote Page Load (All Web Site Data)",
    "View Booking Page Office Space (All Web Site Data)",
    "Contact Us (Default Google Ads Profile)",
    "Example Client 2022/23 (web) purchase",
    # System call / Smart Campaign types
    "Calls from Smart Campaign Ads",
    "Calls from ads (1)",
    "awx_maps_directions",
    "Clicks to call",
    "Calls from ads",
]


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_actions(client):
    ga_service = client.get_service("GoogleAdsService")
    query = """
        SELECT
            conversion_action.id,
            conversion_action.name,
            conversion_action.status,
            conversion_action.type,
            conversion_action.include_in_conversions_metric,
            conversion_action.value_settings.default_value
        FROM conversion_action
        WHERE conversion_action.status != 'REMOVED'
    """
    try:
        rows = ga_service.search(customer_id=CUSTOMER_ID, query=query)
    except GoogleAdsException as ex:
        print(f"[ERROR] {ex.failure.errors[0].message}")
        return {}
    return {
        row.conversion_action.name: {
            "id":       row.conversion_action.id,
            "name":     row.conversion_action.name,
            "status":   row.conversion_action.status.name,
            "type":     int(row.conversion_action.type_),
            "in_conv":  row.conversion_action.include_in_conversions_metric,
            "value":    round(row.conversion_action.value_settings.default_value, 2),
            "resource": f"customers/{CUSTOMER_ID}/conversionActions/{row.conversion_action.id}",
        }
        for row in rows
    }


# ── Plan + print ──────────────────────────────────────────────────────────────

def build_and_print_plan(actions):
    """
    Archive everything except KEEP_ACTION.
    ENABLED Type 8 actions → API.  HIDDEN / system types → manual UI checklist.
    """
    api_ops   = []
    api_skip  = []   # non-archivable via API (system types, call types)
    manual    = list(MANUAL_ARCHIVE)   # start with known HIDDEN list

    # Types that are mutable via API (website conversion actions only)
    API_MUTABLE_TYPES = {8}

    # Classify every action
    for name, a in sorted(actions.items()):
        if name == KEEP_ACTION:
            continue
        if a["status"] == "HIDDEN":
            if name not in manual:
                manual.append(name)
            continue
        # ENABLED — only Type 8 (website) can be archived via API
        if a["type"] in API_MUTABLE_TYPES:
            api_ops.append(("archive", a))
        else:
            # Call types, system types, etc. — manual only
            if name not in manual:
                manual.append(name)

    print("\n" + "=" * 70)
    print("  CONVERSION ACTION CLEANUP — Example Client  (clean slate)")
    print(f"  Keeping:  '{KEEP_ACTION}'")
    print(f"  Removing: everything else")
    print("=" * 70)

    print(f"\n  ── VIA API ({len(api_ops)} actions → status: Removed) ──────────────")
    for _, a in api_ops:
        print(f"    - {a['name']}  (Type {a['type']})")

    print(f"\n  ── MANUAL UI — {len(manual)} actions ────────────────────────────────")
    print("  Path: Tools → Measurement → Conversions → select each → Edit → Archive")
    for name in manual:
        status = f"  ({actions[name]['status']})" if name in actions else "  (not in account — skip)"
        print(f"    - [ ] {name}{status}")

    print("\n" + "=" * 70)
    return api_ops


# ── Pre-cleanup: custom goals + campaign configs ──────────────────────────────

def clear_custom_goals_and_configs(client):
    """
    Unlink campaigns from CustomConversionGoals, delete the goals, then reset
    any remaining CAMPAIGN-level goal configs back to CUSTOMER level.
    This is required before ConversionAction removal — Google Ads refuses to
    remove a conversion action referenced by a CustomConversionGoal.
    """
    ga_service   = client.get_service("GoogleAdsService")
    ccg_service  = client.get_service("CustomConversionGoalService")
    cfg_service  = client.get_service("ConversionGoalCampaignConfigService")
    GoalConfigLevel = client.enums.GoalConfigLevelEnum

    # Step 1 — reset all CAMPAIGN-level configs to CUSTOMER (unlinks custom goals)
    cfg_rows = list(ga_service.search(customer_id=CUSTOMER_ID, query="""
        SELECT conversion_goal_campaign_config.resource_name,
               conversion_goal_campaign_config.goal_config_level
        FROM conversion_goal_campaign_config
        WHERE conversion_goal_campaign_config.goal_config_level = 'CAMPAIGN'
    """))
    if cfg_rows:
        ops = []
        for r in cfg_rows:
            resource = r.conversion_goal_campaign_config.resource_name
            op = client.get_type("ConversionGoalCampaignConfigOperation")
            op.update.resource_name     = resource
            op.update.goal_config_level = GoalConfigLevel.CUSTOMER
            op.update_mask.paths[:] = ["goal_config_level", "custom_conversion_goal"]
            ops.append(op)
        # Send individually — atomic batches fail entirely if any item errors
        success = 0
        for op in ops:
            try:
                cfg_service.mutate_conversion_goal_campaign_configs(
                    customer_id=CUSTOMER_ID, operations=[op]
                )
                success += 1
            except GoogleAdsException as ex:
                for err in ex.failure.errors:
                    print(f"    [WARN] goal config reset skipped: {err.message}")
        if success:
            print(f"    ✓ Reset {success} campaign config(s) to CUSTOMER level")

    # Step 2 — delete all custom conversion goals (now unlinked)
    ccg_rows = list(ga_service.search(customer_id=CUSTOMER_ID, query="""
        SELECT custom_conversion_goal.resource_name, custom_conversion_goal.name
        FROM custom_conversion_goal
    """))
    if ccg_rows:
        ops = []
        for r in ccg_rows:
            op = client.get_type("CustomConversionGoalOperation")
            op.remove = r.custom_conversion_goal.resource_name
            ops.append(op)
        try:
            ccg_service.mutate_custom_conversion_goals(customer_id=CUSTOMER_ID, operations=ops)
            print(f"    ✓ Deleted {len(ops)} custom conversion goal(s)")
        except GoogleAdsException as ex:
            for err in ex.failure.errors:
                print(f"    [ERROR] custom goal delete: {err.message}")


# ── Execute ───────────────────────────────────────────────────────────────────

def execute(client, api_ops):
    ca_service = client.get_service("ConversionActionService")
    results    = {"archived": 0, "activated": 0, "valued": 0, "errors": []}

    for item in api_ops:
        kind = item[0]
        a    = item[1]
        op   = client.get_type("ConversionActionOperation")
        ca   = op.update
        ca.resource_name = a["resource"]
        paths = []

        if kind == "archive":
            # Fresh operation — only set remove, never touch update
            op2 = client.get_type("ConversionActionOperation")
            op2.remove = a["resource"]
            try:
                ca_service.mutate_conversion_actions(
                    customer_id=CUSTOMER_ID,
                    operations=[op2],
                )
                results["archived"] += 1
                print(f"    🗑  Removed: {a['name']}")
            except GoogleAdsException as ex:
                for err in ex.failure.errors:
                    msg = f"{a['name']}: {err.message}"
                    results["errors"].append(msg)
                    print(f"    [ERROR] {msg}")
            continue

        elif kind == "activate":
            new_val = item[2]
            ca.include_in_conversions_metric = True
            paths = ["include_in_conversions_metric"]
            if new_val is not None:
                ca.value_settings.default_value           = new_val
                ca.value_settings.always_use_default_value = False
                paths += ["value_settings.default_value",
                          "value_settings.always_use_default_value"]

        elif kind == "value":
            new_val = item[2]
            ca.value_settings.default_value           = new_val
            ca.value_settings.always_use_default_value = False
            paths = ["value_settings.default_value",
                     "value_settings.always_use_default_value"]

        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))

        try:
            ca_service.mutate_conversion_actions(
                customer_id=CUSTOMER_ID,
                operations=[op],
            )
            if kind == "archive":
                results["archived"] += 1
                print(f"    🗑  Archived: {a['name']}")
            elif kind == "activate":
                results["activated"] += 1
                print(f"    ✅ Activated: {a['name']}")
            else:
                results["valued"] += 1
                print(f"    💰 Value updated: {a['name']}")
        except GoogleAdsException as ex:
            for err in ex.failure.errors:
                msg = f"{a['name']}: {err.message}"
                results["errors"].append(msg)
                print(f"    [ERROR] {msg}")

    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    client  = GoogleAdsClient.load_from_storage(YAML_FILE)

    print("\nFetching conversion actions...")
    actions = fetch_actions(client)
    print(f"Found {len(actions)} active/enabled actions.")

    api_ops = build_and_print_plan(actions)

    api_count = len(api_ops)
    print(f"\n  API changes ready to apply: {api_count}")
    print(f"  Manual UI steps: {len(MANUAL_ARCHIVE)} actions (see list above)")
    confirm = input("\n  Apply the API changes now? (yes / no): ").strip().lower()

    if confirm != "yes":
        print("\n  Aborted — no changes made.")
        print("  The manual UI checklist above is still valid — work through that in Google Ads.")
        return

    print("\n  Pre-cleanup: removing custom conversion goals and resetting campaign configs...")
    clear_custom_goals_and_configs(client)

    print("\n  Applying API changes...\n")
    results = execute(client, api_ops)

    print(f"\n{'=' * 70}")
    print(f"  API changes done.")
    print(f"  Archived:        {results['archived']}")
    print(f"  Activated:       {results['activated']}")
    print(f"  Values updated:  {results['valued']}")
    if results["errors"]:
        print(f"  Errors:          {len(results['errors'])}")
        for e in results["errors"]:
            print(f"    - {e}")
    print(f"\n  ⚠️  Complete the manual UI checklist above to finish the clean slate.")
    print(f"  '{KEEP_ACTION}' was untouched.")
    print("=" * 70)


if __name__ == "__main__":
    main()
