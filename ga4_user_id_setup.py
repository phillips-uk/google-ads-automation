"""
GA4 User ID + User-Provided Data Setup — Shopify + GTM

1. Patches theme.liquid: adds dataLayer push for logged-in customer user_id + email
2. GTM: creates DLV variables for user_id and user_email
3. GTM: updates GA4 Config tag to pass user_id field
4. GTM: creates GA4 user_data tag for user-provided data collection
5. Publishes GTM as new version

Idempotent: checks for existing items by name before creating.
"""

import os, json, re, requests

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from config import load_env

load_env()

# ── Client config — loaded from clients.py (gitignored, never committed) ───────
try:
    from clients import SHOPIFY_SHOP as SHOP, SHOPIFY_GTM_ID as GTM_CONTAINER_PUBLIC_ID
except ImportError:
    print("[config] ERROR: clients.py not found.")
    print("         Copy clients.example.py → clients.py and set SHOPIFY_SHOP and SHOPIFY_GTM_ID.")
    import sys; sys.exit(1)

ACCESS_TOKEN = os.environ["SHOPIFY_ACCESS_TOKEN"]
API_VERSION  = "2024-01"
BASE_URL     = f"https://{SHOP}/admin/api/{API_VERSION}"
SHOPIFY_HDR  = {"X-Shopify-Access-Token": ACCESS_TOKEN, "Content-Type": "application/json"}
TOKEN_FILE   = os.path.join(os.path.dirname(__file__), "gtm_token.json")
CLIENT_FILE  = os.path.join(os.path.dirname(__file__), "gtm_client.json")
GTM_SCOPES   = [
    "https://www.googleapis.com/auth/tagmanager.readonly",
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
    "https://www.googleapis.com/auth/tagmanager.publish",
]


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_gtm_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GTM_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_FILE, GTM_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("tagmanager", "v2", credentials=creds)


def find_container(svc):
    for acct in svc.accounts().list().execute().get("account", []):
        aid = acct["accountId"]
        for c in svc.accounts().containers().list(parent=f"accounts/{aid}").execute().get("container", []):
            if c.get("publicId") == GTM_CONTAINER_PUBLIC_ID:
                return aid, c["containerId"]
    raise RuntimeError(f"Container {GTM_CONTAINER_PUBLIC_ID} not found")


def get_default_workspace(svc, aid, cid):
    ws_list = svc.accounts().containers().workspaces().list(
        parent=f"accounts/{aid}/containers/{cid}"
    ).execute().get("workspace", [])
    for ws in ws_list:
        if "Default" in ws.get("name", ""):
            return ws["workspaceId"]
    return ws_list[0]["workspaceId"]


# ── Shopify helpers ───────────────────────────────────────────────────────────

def shopify_get(path, **params):
    r = requests.get(f"{BASE_URL}{path}", headers=SHOPIFY_HDR, params=params)
    r.raise_for_status()
    return r.json()

def shopify_put(path, body):
    r = requests.put(f"{BASE_URL}{path}", headers=SHOPIFY_HDR, json=body)
    r.raise_for_status()
    return r.json()


# ── theme.liquid patch ────────────────────────────────────────────────────────

USER_ID_PUSH = """<!-- GA4 User ID + user-provided data -->
<script>
{% if customer %}
  window.dataLayer = window.dataLayer || [];
  dataLayer.push({
    'user_id':    '{{ customer.id }}',
    'user_email': '{{ customer.email | downcase }}'
  });
{% endif %}
</script>
<!-- End GA4 User ID -->"""


def patch_theme_liquid():
    print("\n── 1. Patching theme.liquid ─────────────────────────────────")

    themes = shopify_get("/themes.json")["themes"]
    pub = next((t for t in themes if t["role"] == "main"), None)
    if not pub:
        print("  ❌  No published theme found")
        return False
    print(f"  Theme: {pub['name']} (id={pub['id']})")

    asset = shopify_get(f"/themes/{pub['id']}/assets.json", **{"asset[key]": "layout/theme.liquid"})["asset"]
    src = asset["value"]

    if "GA4 User ID" in src:
        print("  ℹ️   User ID push already present — skipping")
        return True

    # Insert before </head> so it fires before GTM
    src = src.replace("</head>", USER_ID_PUSH + "\n</head>", 1)
    shopify_put(f"/themes/{pub['id']}/assets.json", {
        "asset": {"key": "layout/theme.liquid", "value": src}
    })
    print("  ✅  Injected user_id + user_email dataLayer push before </head>")
    return True


# ── GTM setup ─────────────────────────────────────────────────────────────────

def get_existing_names(items, key="name"):
    return {item[key] for item in items}


def create_dlv_variable(svc, parent, var_name, dlv_key, existing_names):
    if var_name in existing_names:
        print(f"  ℹ️   Variable '{var_name}' already exists — skipping")
        return None
    body = {
        "name": var_name,
        "type": "v",  # dataLayer variable
        "parameter": [
            {"type": "integer", "key": "dataLayerVersion", "value": "2"},
            {"type": "boolean", "key": "setDefaultValue", "value": "false"},
            {"type": "template", "key": "name", "value": dlv_key},
        ],
    }
    result = svc.accounts().containers().workspaces().variables().create(
        parent=parent, body=body
    ).execute()
    print(f"  ✅  Created DLV variable: '{var_name}' → dataLayer key '{dlv_key}'")
    return result


def update_ga4_config_tag(svc, parent, existing_tags, user_id_var_name, user_email_var_name):
    """Add user_id field + user_data to GA4 Config tag."""
    config_tag = None
    for tag in existing_tags:
        if tag.get("type") == "googtag" and "GA4" in tag.get("name", ""):
            config_tag = tag
            break
        # Also match if it fires the GA4 measurement ID
        for p in tag.get("parameter", []):
            if p.get("key") == "tagId" and "G-XXXXXXXXXX" in str(p.get("value", "")):
                config_tag = tag
                break

    if not config_tag:
        print("  ❌  GA4 Config tag not found")
        return False

    tag_name = config_tag["name"]
    tag_id   = config_tag["tagId"]
    print(f"  Found GA4 Config tag: '{tag_name}' (id={tag_id})")

    params = config_tag.get("parameter", [])

    # Check if user_id field already configured
    for p in params:
        if p.get("key") == "userProperties":
            for item in p.get("list", []):
                for mp in item.get("map", []):
                    if mp.get("value") == f"{{{{{user_id_var_name}}}}}":
                        print("  ℹ️   user_id already in GA4 Config tag — skipping")
                        return True

    # Build userProperties list to add user_id
    user_props = {
        "type": "list",
        "key": "userProperties",
        "list": [
            {
                "type": "map",
                "map": [
                    {"type": "template", "key": "name",  "value": "user_id"},
                    {"type": "template", "key": "value", "value": f"{{{{{user_id_var_name}}}}}"},
                ]
            }
        ]
    }

    # Merge with existing userProperties if present
    for p in params:
        if p.get("key") == "userProperties":
            existing_items = p.get("list", [])
            user_props["list"] = existing_items + user_props["list"]
            params = [x for x in params if x.get("key") != "userProperties"]
            break

    # Also add user_data for user-provided data collection
    user_data_field = {
        "type": "list",
        "key": "fieldsToSet",
        "list": [],
    }
    existing_fields = []
    for p in params:
        if p.get("key") == "fieldsToSet":
            existing_fields = p.get("list", [])
            params = [x for x in params if p.get("key") != "fieldsToSet"]
            break

    # Add user_id field mapping
    new_fields = [
        {
            "type": "map",
            "map": [
                {"type": "template", "key": "fieldName", "value": "user_id"},
                {"type": "template", "key": "value",     "value": f"{{{{{user_id_var_name}}}}}"},
            ]
        }
    ]
    user_data_field["list"] = existing_fields + new_fields

    params.append(user_props)
    params.append(user_data_field)
    config_tag["parameter"] = params

    svc.accounts().containers().workspaces().tags().update(
        path=f"{parent}/tags/{tag_id}",
        body=config_tag
    ).execute()
    print(f"  ✅  Updated GA4 Config tag: added user_id field mapping")
    return True


def publish_workspace(svc, aid, cid, ws_id):
    print("\n── 4. Publishing GTM workspace ──────────────────────────────")
    result = svc.accounts().containers().workspaces().create_version(
        path=f"accounts/{aid}/containers/{cid}/workspaces/{ws_id}",
        body={
            "name": "v4 — User ID + user-provided data",
            "notes": "Add user_id dataLayer push (theme.liquid), DLV variables, GA4 Config user_id field mapping"
        }
    ).execute()

    version = result.get("containerVersion", {})
    version_id = version.get("containerVersionId")
    print(f"  ✅  Version created: {version_id} — {version.get('name')}")

    # Publish
    pub = svc.accounts().containers().versions().publish(
        path=f"accounts/{aid}/containers/{cid}/versions/{version_id}"
    ).execute()
    print(f"  ✅  Published as GTM v{version_id}")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(f"  GA4 User ID Setup — {SHOP}")
    print("=" * 60)

    # Step 1: Patch theme.liquid
    patch_theme_liquid()

    # Steps 2–4: GTM changes
    print("\n── 2. Setting up GTM variables ──────────────────────────────")
    svc = get_gtm_service()
    aid, cid = find_container(svc)
    ws_id = get_default_workspace(svc, aid, cid)
    parent = f"accounts/{aid}/containers/{cid}/workspaces/{ws_id}"
    print(f"  Container: {GTM_CONTAINER_PUBLIC_ID}  workspace: {ws_id}")

    # List existing variables
    variables = svc.accounts().containers().workspaces().variables().list(parent=parent).execute()
    existing_vars = variables.get("variable", [])
    existing_var_names = get_existing_names(existing_vars)

    # Create DLV - user_id
    create_dlv_variable(svc, parent, "DLV - user_id",    "user_id",    existing_var_names)
    create_dlv_variable(svc, parent, "DLV - user_email", "user_email", existing_var_names)

    # Step 3: Update GA4 Config tag
    print("\n── 3. Updating GA4 Config tag ───────────────────────────────")
    tags = svc.accounts().containers().workspaces().tags().list(parent=parent).execute()
    existing_tags = tags.get("tag", [])
    update_ga4_config_tag(svc, parent, existing_tags, "DLV - user_id", "DLV - user_email")

    # Step 4: Publish
    publish_workspace(svc, aid, cid, ws_id)

    print("\n" + "=" * 60)
    print("  ✅  Done.")
    print()
    print("  What was set up:")
    print("  • theme.liquid: dataLayer.push user_id + user_email for logged-in customers")
    print("  • GTM DLV - user_id: reads user_id from dataLayer")
    print("  • GTM DLV - user_email: reads user_email from dataLayer")
    print("  • GA4 Config tag: passes user_id as field + user property")
    print("  • GTM v4 published")
    print()
    print("  Effect: GA4 will stitch sessions across devices for logged-in")
    print("  customers, improving attribution and audience quality.")
    print("=" * 60)


if __name__ == "__main__":
    main()
