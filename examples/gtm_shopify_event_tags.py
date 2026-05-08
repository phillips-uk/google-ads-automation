"""Add GA4 event tags to the existing Example Store GTM workspace."""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from tracking_audit import build_gtm_service

CONTAINER_PATH = "accounts/0000000000/containers/00000000"
GA4_MEASUREMENT_ID = "G-XXXXXXXXXX"


def _event_params(pairs):
    return {
        "type": "list",
        "key":  "eventParameters",
        "list": [
            {"type": "map", "map": [
                {"type": "template", "key": "name",  "value": n},
                {"type": "template", "key": "value", "value": v},
            ]}
            for n, v in pairs
        ],
    }


def var(n):
    return "{{" + n + "}}"


def main():
    svc = build_gtm_service()

    # Find the workspace
    workspaces = svc.accounts().containers().workspaces().list(
        parent=CONTAINER_PATH
    ).execute().get("workspace", [])
    ws = workspaces[0]
    ws_path = ws["path"]
    print(f"Workspace: {ws['name']}  {ws_path}")

    # Find existing triggers by name
    triggers = {
        t["name"]: t["triggerId"]
        for t in svc.accounts().containers().workspaces().triggers().list(
            parent=ws_path
        ).execute().get("trigger", [])
    }
    print(f"Found triggers: {list(triggers.keys())}")

    # GA4 event tags — gaawe requires measurementIdOverride
    ga4_id_param = {"type": "template", "key": "measurementIdOverride",
                    "value": var("CONST - GA4 Measurement ID")}

    ecommerce_params = [
        ("currency", var("DLV - ecommerce.currency")),
        ("value",    var("DLV - ecommerce.value")),
        ("items",    var("DLV - ecommerce.items")),
    ]

    event_tags = [
        {
            "name":      "GA4 - view_item",
            "trigger":   "CE - view_item",
            "event":     "view_item",
            "params":    ecommerce_params,
        },
        {
            "name":      "GA4 - add_to_cart",
            "trigger":   "CE - add_to_cart",
            "event":     "add_to_cart",
            "params":    ecommerce_params,
        },
        {
            "name":      "GA4 - begin_checkout",
            "trigger":   "CE - begin_checkout",
            "event":     "begin_checkout",
            "params":    ecommerce_params,
        },
        {
            "name":      "GA4 - sign_up",
            "trigger":   "CE - sign_up",
            "event":     "sign_up",
            "params":    [("method", "newsletter")],
        },
    ]

    for defn in event_tags:
        trigger_id = triggers.get(defn["trigger"])
        if not trigger_id:
            print(f"  ⚠️  Trigger not found: {defn['trigger']}")
            continue

        body = {
            "name":            defn["name"],
            "type":            "gaawe",
            "firingTriggerId": [trigger_id],
            "parameter": [
                ga4_id_param,
                {"type": "template", "key": "eventName", "value": defn["event"]},
                _event_params(defn["params"]),
            ],
        }
        tag = svc.accounts().containers().workspaces().tags().create(
            parent=ws_path, body=body
        ).execute()
        print(f"  ✅  Tag created: {defn['name']}  (id={tag['tagId']})")

    print("\nAll event tags created. Review and publish from GTM UI.")


if __name__ == "__main__":
    main()
