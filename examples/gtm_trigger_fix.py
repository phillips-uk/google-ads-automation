"""
GTM Trigger Fix — EC - Capture Form Email

The 'Pageview - Contact Page' trigger filters for URLs containing '/contact',
but the landing page with the embedded Shopify contact form is:
  https://yourclient.co.uk/pages/private-office-space

The form itself has action="/contact#contact_form" so the JS selector is fine.
The GTM trigger just needs to match the landing page URL too.

Fix: update the trigger filter from 'contains /contact' to a regex that
matches either /contact (standalone contact page) or the landing page.

Then publish the workspace as a new version.
"""

import json
import os

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import load_env

load_env()

GTM_SCOPES = [
    "https://www.googleapis.com/auth/tagmanager.readonly",
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
    "https://www.googleapis.com/auth/tagmanager.publish",
]
TOKEN_FILE      = os.path.join(os.path.dirname(__file__), "gtm_token.json")
GTM_CLIENT_FILE = os.path.join(os.path.dirname(__file__), "gtm_client.json")
CONTAINER_PUBLIC_ID = "GTM-XXXXXXX"

TRIGGER_NAME    = "Pageview - Contact Page"
# Match either the standalone /contact page or the landing page with the embedded form
URL_REGEX       = r"yourclient\.co\.uk/(contact|pages/private-office-space)"


def get_gtm_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GTM_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GTM_CLIENT_FILE, GTM_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("tagmanager", "v2", credentials=creds)


def find_container(service):
    accounts = service.accounts().list().execute().get("account", [])
    for account in accounts:
        aid = account["accountId"]
        containers = service.accounts().containers().list(
            parent=f"accounts/{aid}"
        ).execute().get("container", [])
        for c in containers:
            if c.get("publicId") == CONTAINER_PUBLIC_ID:
                return aid, c["containerId"]
    raise RuntimeError(f"Container {CONTAINER_PUBLIC_ID} not found")


def get_workspace(service, account_id, container_id):
    parent = f"accounts/{account_id}/containers/{container_id}"
    workspaces = service.accounts().containers().workspaces().list(
        parent=parent
    ).execute().get("workspace", [])
    for ws in workspaces:
        if "Default" in ws.get("name", ""):
            return ws["workspaceId"]
    return workspaces[0]["workspaceId"] if workspaces else "1"


def main():
    print("GTM Trigger Fix — EC Capture Form Email")
    print("=" * 60)

    service = get_gtm_service()
    account_id, container_id = find_container(service)
    workspace_id = get_workspace(service, account_id, container_id)
    ws_path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
    print(f"  Container: {container_id}  Workspace: {workspace_id}")

    # ── Find the trigger ──────────────────────────────────────────────────────
    triggers = service.accounts().containers().workspaces().triggers().list(
        parent=ws_path
    ).execute().get("trigger", [])

    target = next((t for t in triggers if t.get("name") == TRIGGER_NAME), None)
    if not target:
        print(f"\n[ERROR] Trigger '{TRIGGER_NAME}' not found.")
        print("Existing triggers:")
        for t in triggers:
            print(f"  - {t['name']} (type: {t['type']})")
        return

    print(f"\nFound trigger: '{target['name']}' (ID: {target['triggerId']})")
    print(f"  Current filter: {json.dumps(target.get('filter', []), indent=4)}")

    # ── Build updated trigger ─────────────────────────────────────────────────
    updated = dict(target)
    updated["filter"] = [{
        "type": "matchRegex",
        "parameter": [
            {"type": "template", "key": "arg0",        "value": "{{Page URL}}"},
            {"type": "template", "key": "arg1",        "value": URL_REGEX},
            {"type": "boolean",  "key": "ignore_case", "value": "true"},
        ]
    }]

    print(f"\n  New filter (matchRegex): {URL_REGEX}")

    print("\n  Applying update...")


    # ── Apply update ──────────────────────────────────────────────────────────
    try:
        result = service.accounts().containers().workspaces().triggers().update(
            path=target["path"],
            body=updated,
        ).execute()
        print(f"\n  ✅ Trigger updated: {result['name']}")
    except HttpError as e:
        print(f"\n  [ERROR] {e}")
        return

    # ── Verify capture tag is using this trigger ───────────────────────────────
    tags = service.accounts().containers().workspaces().tags().list(
        parent=ws_path
    ).execute().get("tag", [])

    capture = next((t for t in tags if t.get("name") == "EC - Capture Form Email"), None)
    if capture:
        firing = capture.get("firingTriggerId", [])
        trigger_id = str(target["triggerId"])
        if trigger_id in [str(x) for x in firing]:
            print(f"  ✓ 'EC - Capture Form Email' already uses trigger ID {trigger_id}")
        else:
            print(f"\n  ⚠️  'EC - Capture Form Email' firing triggers: {firing}")
            print(f"     Expected trigger ID: {trigger_id}")
            print("     Updating tag to use correct trigger...")
            updated_tag = dict(capture)
            updated_tag["firingTriggerId"] = [trigger_id]
            service.accounts().containers().workspaces().tags().update(
                path=capture["path"],
                body=updated_tag,
            ).execute()
            print("  ✅ Tag trigger updated.")
    else:
        print("  ⚠️  'EC - Capture Form Email' tag not found — check GTM manually.")

    # ── Publish ───────────────────────────────────────────────────────────────
    print("\n  Publishing workspace as new version...")


    container_path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
    try:
        pub = service.accounts().containers().workspaces().create_version(
            path=container_path,
            body={"name": "EC trigger fix — match landing page URL"},
        ).execute()
        version_id = pub.get("containerVersion", {}).get("containerVersionId", "?")
        print(f"  ✅ Version created: {version_id}")

        # Publish the version
        version_path = f"accounts/{account_id}/containers/{container_id}/versions/{version_id}"
        service.accounts().containers().versions().publish(
            path=version_path
        ).execute()
        print(f"  ✅ Published as Version {version_id}")
        print(f"\n  Live. Test: submit the form at the target landing page")
        print(f"  GTM Preview: https://tagmanager.google.com/#/container/accounts/{account_id}/containers/{container_id}")
    except HttpError as e:
        print(f"\n  [ERROR publishing] {e}")
        print("  Version created — publish manually in GTM UI.")

    print("\n" + "=" * 60)
    print("Done.")


if __name__ == "__main__":
    main()
