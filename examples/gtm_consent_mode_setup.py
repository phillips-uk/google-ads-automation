"""
GTM Consent Mode v2 Setup — Example Client (GTM-XXXXXXX)

Sets up the consent mode infrastructure so that the Consentik GTM template
can slot in as the CMP with zero additional API work.

What this script does (via GTM API):
  1. Creates a 'Consent Initialization - All Pages' trigger (if missing)
  2. Creates 'Consent Mode v2 - Default State' Custom HTML tag
       - Fires on Consent Initialization trigger, priority 10
       - Sets all consent states to denied by default (500ms wait)
       - Enables url_passthrough + ads_data_redaction
  3. Adds ad_storage + ad_user_data consent requirements to:
       - 'Google AW Tag global'
       - 'Thank-you-page conversion'
  4. Publishes the workspace

After running this script, complete these 2 manual steps in the GTM UI:
  A. Admin → Container Settings → check 'Enable consent overview' → Save
  B. Templates → Search Gallery → 'Consentik GDPR CMP' → Add to workspace
     Then create a new Tag:
       - Type: Consentik GDPR CMP
       - Region: (leave blank — all countries)
       - Analytics and Statistics: Denied
       - Marketing and Retargeting: Denied
       - Functional Cookies: Denied
       - Update delay: 500
       - Consent Settings: No additional consent required
       - Trigger: Consent Initialization - All Pages
       - Save → Submit
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
TOKEN_FILE          = os.path.join(os.path.dirname(__file__), "gtm_token.json")
GTM_CLIENT_FILE     = os.path.join(os.path.dirname(__file__), "gtm_client.json")
CONTAINER_PUBLIC_ID = "GTM-XXXXXXX"

CONSENT_INIT_TRIGGER_NAME = "Consent Initialization - All Pages"
CONSENT_DEFAULT_TAG_NAME  = "Consent Mode v2 - Default State"

CONSENT_DEFAULT_HTML = """<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}

  gtag('consent', 'default', {
    'ad_storage':              'denied',
    'ad_user_data':            'denied',
    'ad_personalization':      'denied',
    'analytics_storage':       'denied',
    'functionality_storage':   'denied',
    'personalization_storage': 'denied',
    'security_storage':        'granted',
    'wait_for_update':         500
  });

  gtag('set', 'url_passthrough', true);
  gtag('set', 'ads_data_redaction', true);
</script>"""

# Google Ads tags to add consent requirements to
ADS_TAGS_TO_UPDATE = {
    "Google AW Tag global",
    "Thank-you-page conversion",
}


# ── Auth / Service ─────────────────────────────────────────────────────────────

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


def get_workspace_id(service, account_id, container_id):
    parent = f"accounts/{account_id}/containers/{container_id}"
    workspaces = service.accounts().containers().workspaces().list(
        parent=parent
    ).execute().get("workspace", [])
    for ws in workspaces:
        if "Default" in ws.get("name", ""):
            return ws["workspaceId"]
    return workspaces[0]["workspaceId"] if workspaces else "1"


# ── Step 1: Consent Initialization trigger ────────────────────────────────────

def ensure_consent_init_trigger(service, ws_path):
    """
    Return the trigger ID for 'Consent Initialization - All Pages'.
    Creates it if not present.
    """
    triggers = service.accounts().containers().workspaces().triggers().list(
        parent=ws_path
    ).execute().get("trigger", [])

    for t in triggers:
        if t.get("name") == CONSENT_INIT_TRIGGER_NAME:
            print(f"  ✅ Trigger already exists: '{CONSENT_INIT_TRIGGER_NAME}' (id={t['triggerId']})")
            return t["triggerId"]

    print(f"  Creating trigger: '{CONSENT_INIT_TRIGGER_NAME}' ...")
    body = {
        "name": CONSENT_INIT_TRIGGER_NAME,
        "type": "consentInit",
    }
    try:
        result = service.accounts().containers().workspaces().triggers().create(
            parent=ws_path, body=body
        ).execute()
        tid = result["triggerId"]
        print(f"  ✅ Created trigger id={tid}")
        return tid
    except HttpError as e:
        print(f"  [ERROR] Could not create trigger: {e}")
        return None


# ── Step 2: Consent default state tag ─────────────────────────────────────────

def ensure_consent_default_tag(service, ws_path, trigger_id):
    """Create the Consent Mode v2 default state Custom HTML tag (skip if exists)."""
    tags = service.accounts().containers().workspaces().tags().list(
        parent=ws_path
    ).execute().get("tag", [])

    for t in tags:
        if t.get("name") == CONSENT_DEFAULT_TAG_NAME:
            print(f"  ✅ Tag already exists: '{CONSENT_DEFAULT_TAG_NAME}'")
            return

    print(f"  Creating tag: '{CONSENT_DEFAULT_TAG_NAME}' ...")
    body = {
        "name":     CONSENT_DEFAULT_TAG_NAME,
        "type":     "html",
        "priority": {"type": "integer", "value": "10"},
        "parameter": [
            {"type": "template", "key": "html",          "value": CONSENT_DEFAULT_HTML},
            {"type": "boolean",  "key": "supportDocumentWrite", "value": "false"},
        ],
        "firingTriggerId": [str(trigger_id)],
        "consentSettings": {
            "consentStatus": "notNeeded",
        },
    }
    try:
        result = service.accounts().containers().workspaces().tags().create(
            parent=ws_path, body=body
        ).execute()
        print(f"  ✅ Created tag '{CONSENT_DEFAULT_TAG_NAME}' (id={result['tagId']})")
    except HttpError as e:
        print(f"  [ERROR] Could not create tag: {e}")


# ── Step 3: Add consent requirements to existing Google Ads tags ───────────────

def update_ads_tags_consent(service, ws_path):
    """Add ad_storage + ad_user_data consent requirements to Google Ads tags."""
    tags = service.accounts().containers().workspaces().tags().list(
        parent=ws_path
    ).execute().get("tag", [])

    for tag in tags:
        name = tag.get("name", "")
        if name not in ADS_TAGS_TO_UPDATE:
            continue

        existing = tag.get("consentSettings", {})
        if existing.get("consentStatus") == "needed":
            print(f"  ✅ Consent already set on '{name}' — skipping.")
            continue

        print(f"  Updating consent settings on '{name}' ...")
        tag["consentSettings"] = {
            "consentStatus": "needed",
            "consentType": {
                "type": "list",
                "list": [
                    {"type": "template", "value": "ad_storage"},
                    {"type": "template", "value": "ad_user_data"},
                ],
            },
        }
        try:
            service.accounts().containers().workspaces().tags().update(
                path=tag["path"], body=tag
            ).execute()
            print(f"  ✅ Consent settings applied to '{name}'")
        except HttpError as e:
            print(f"  [ERROR] Could not update '{name}': {e}")


# ── Step 4: Publish ────────────────────────────────────────────────────────────

def publish_workspace(service, ws_path, account_id, container_id, workspace_id):
    print("\n  Publishing workspace ...")
    try:
        service.accounts().containers().workspaces().create_version(
            path=ws_path,
            body={"name": "Consent Mode v2 setup", "notes": "Default state tag + consent requirements on GA tags. Consentik template tag to follow."},
        ).execute()
        print("  ✅ Version created and published.")
    except HttpError as e:
        print(f"  [ERROR] Could not publish: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 65)
    print("  GTM Consent Mode v2 Setup — Example Client")
    print("=" * 65)

    service      = get_gtm_service()
    account_id, container_id = find_container(service)
    workspace_id = get_workspace_id(service, account_id, container_id)
    ws_path      = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
    print(f"\n  Container: {container_id}  Workspace: {workspace_id}\n")

    print("── Step 1: Consent Initialization trigger ────────────────────")
    trigger_id = ensure_consent_init_trigger(service, ws_path)
    if not trigger_id:
        print("  ❌ Cannot continue without trigger.")
        return

    print("\n── Step 2: Consent default state tag ─────────────────────────")
    ensure_consent_default_tag(service, ws_path, trigger_id)

    print("\n── Step 3: Consent requirements on Google Ads tags ───────────")
    update_ads_tags_consent(service, ws_path)

    print("\n── Step 4: Publish ───────────────────────────────────────────")
    publish_workspace(service, ws_path, account_id, container_id, workspace_id)

    print("\n" + "=" * 65)
    print("  API setup complete.")
    print("\n  ⚠️  2 manual steps remaining in GTM UI:")
    print()
    print("  A. Admin → Container Settings")
    print("     → check 'Enable consent overview' → Save")
    print()
    print("  B. Templates → Search Gallery → 'Consentik GDPR CMP' → Add")
    print("     → Tags → New → Consentik GDPR CMP")
    print("       Region:                    (blank — all countries)")
    print("       Analytics and Statistics:  Denied")
    print("       Marketing and Retargeting: Denied")
    print("       Functional Cookies:        Denied")
    print("       Update delay:              500")
    print("       Consent Settings:          No additional consent required")
    print("       Trigger:                   Consent Initialization - All Pages")
    print("     → Save → Submit")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
