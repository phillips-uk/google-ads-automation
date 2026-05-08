"""
Enhanced Conversions Fix — Example Client (GTM-XXXXXXX)

Problem:
  The 'Thank-you-page conversion' tag uses {{Auto Collection}} for enhanced
  conversions. On Shopify, the contact form posts then redirects to /pages/thank-you,
  so the email field is gone from the DOM on the thank-you page. Auto Collection
  finds nothing → EC shows "Setup issues detected."

Fix (sessionStorage bridge):
  1. Create DLV variables for user_data.email and user_data.phone_number
  2. 'EC - Capture Form Email' Custom HTML tag fires on the Shopify contact form
     page submit trigger, grabs the email (and phone if present) from the form
     fields, writes them to sessionStorage.
  3. 'EC - Push User Data' Custom HTML tag fires on the existing thank-you page
     trigger (fires BEFORE the conversion tag via tag sequencing), reads
     sessionStorage and pushes user_data to dataLayer.
  4. Update 'Thank-you-page conversion' tag: replace Auto Collection with
     {{dlv - user_data.email}} and add phone field.

This script is idempotent: it checks for existing items by name before creating.
"""

import os
import json

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from config import load_env

load_env()

GTM_SCOPES = [
    "https://www.googleapis.com/auth/tagmanager.readonly",
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
]
TOKEN_FILE      = os.path.join(os.path.dirname(__file__), "gtm_token.json")
GTM_CLIENT_FILE = os.path.join(os.path.dirname(__file__), "gtm_client.json")
CONTAINER_PUBLIC_ID = "GTM-XXXXXXX"


# ── Auth ──────────────────────────────────────────────────────────────────────

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


# ── Find container / workspace ─────────────────────────────────────────────────

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
    # prefer Default Workspace
    for ws in workspaces:
        if "Default" in ws.get("name", ""):
            return ws["workspaceId"]
    return workspaces[0]["workspaceId"] if workspaces else "1"


# ── Helpers ───────────────────────────────────────────────────────────────────

def list_variables(service, path):
    return service.accounts().containers().workspaces().variables().list(
        parent=path
    ).execute().get("variable", [])


def list_tags(service, path):
    return service.accounts().containers().workspaces().tags().list(
        parent=path
    ).execute().get("tag", [])


def list_triggers(service, path):
    return service.accounts().containers().workspaces().triggers().list(
        parent=path
    ).execute().get("trigger", [])


def find_by_name(items, name):
    for item in items:
        if item.get("name") == name:
            return item
    return None


def var_ref(variable):
    """Return the {{Name}} reference string for a variable."""
    return "{{" + variable["name"] + "}}"


# ── Step 1: Create Data Layer Variables ───────────────────────────────────────

def ensure_dlv(service, ws_path, variables, var_name, dl_key):
    existing = find_by_name(variables, var_name)
    if existing:
        print(f"  ✓ Variable already exists: {var_name}")
        return existing
    body = {
        "name": var_name,
        "type": "v",  # Data Layer Variable
        "parameter": [
            {"type": "integer", "key": "dataLayerVersion", "value": "2"},
            {"type": "boolean", "key": "setDefaultValue",  "value": "false"},
            {"type": "template", "key": "name",            "value": dl_key},
        ],
    }
    result = service.accounts().containers().workspaces().variables().create(
        parent=ws_path, body=body
    ).execute()
    print(f"  ✅ Created variable: {var_name}")
    return result


# ── Step 2: Capture tag (contact form page) ────────────────────────────────────

CAPTURE_TAG_NAME = "EC - Capture Form Email"
CAPTURE_TAG_HTML = """<script>
(function() {
  var form = document.querySelector('form[action*="/contact"]');
  if (!form) return;
  form.addEventListener('submit', function() {
    var emailEl = form.querySelector('input[name="contact[email]"], input[type="email"]');
    var phoneEl = form.querySelector('input[name="contact[phone]"], input[type="tel"]');
    if (emailEl && emailEl.value) {
      sessionStorage.setItem('_ec_email', emailEl.value.trim().toLowerCase());
    }
    if (phoneEl && phoneEl.value) {
      sessionStorage.setItem('_ec_phone', phoneEl.value.trim());
    }
  }, true);
})();
</script>"""

def ensure_capture_tag(service, ws_path, tags, triggers):
    existing = find_by_name(tags, CAPTURE_TAG_NAME)
    if existing:
        print(f"  ✓ Tag already exists: {CAPTURE_TAG_NAME}")
        return existing

    # Fire on all pages that contain the contact form
    # Use a trigger that fires on pages where URL contains /contact
    # First check if a suitable trigger exists
    contact_trigger = None
    for t in triggers:
        name = t.get("name", "").lower()
        if "contact" in name and t.get("type") in ("pageview", "domReady", "windowLoaded"):
            contact_trigger = t
            break

    if not contact_trigger:
        # Create a pageview trigger that fires on /contact pages
        trigger_body = {
            "name": "Pageview - Contact Page",
            "type": "pageview",
            "filter": [{
                "type": "contains",
                "parameter": [
                    {"type": "template", "key": "arg0", "value": "{{Page URL}}"},
                    {"type": "template", "key": "arg1", "value": "/contact"},
                ]
            }]
        }
        contact_trigger = service.accounts().containers().workspaces().triggers().create(
            parent=ws_path, body=trigger_body
        ).execute()
        print(f"  ✅ Created trigger: Pageview - Contact Page")
    else:
        print(f"  ✓ Using existing trigger: {contact_trigger['name']}")

    body = {
        "name": CAPTURE_TAG_NAME,
        "type": "html",
        "parameter": [
            {"type": "template", "key": "html",            "value": CAPTURE_TAG_HTML},
            {"type": "boolean", "key": "supportDocumentWrite", "value": "false"},
        ],
        "firingTriggerId": [contact_trigger["triggerId"]],
    }
    result = service.accounts().containers().workspaces().tags().create(
        parent=ws_path, body=body
    ).execute()
    print(f"  ✅ Created tag: {CAPTURE_TAG_NAME}")
    return result


# ── Step 3: Push tag (thank-you page) ─────────────────────────────────────────

PUSH_TAG_NAME = "EC - Push User Data"
PUSH_TAG_HTML = """<script>
(function() {
  var email = sessionStorage.getItem('_ec_email');
  var phone = sessionStorage.getItem('_ec_phone');
  if (!email && !phone) return;
  var userData = {};
  if (email) userData.email = email;
  if (phone) userData.phone_number = phone;
  window.dataLayer = window.dataLayer || [];
  dataLayer.push({ user_data: userData });
  // Clean up so it doesn't persist across future page loads
  sessionStorage.removeItem('_ec_email');
  sessionStorage.removeItem('_ec_phone');
})();
</script>"""

def ensure_push_tag(service, ws_path, tags, triggers, conversion_tag):
    existing = find_by_name(tags, PUSH_TAG_NAME)
    if existing:
        print(f"  ✓ Tag already exists: {PUSH_TAG_NAME}")
        return existing

    # Find the thank-you page trigger (same one used by the conversion tag)
    thank_you_trigger_ids = conversion_tag.get("firingTriggerId", [])
    if not thank_you_trigger_ids:
        raise RuntimeError("Could not find firing trigger on Thank-you-page conversion tag")

    thank_you_trigger_id = thank_you_trigger_ids[0]
    print(f"  ✓ Reusing trigger ID {thank_you_trigger_id} from conversion tag")

    # Tag sequences: this tag must fire BEFORE the conversion tag
    body = {
        "name": PUSH_TAG_NAME,
        "type": "html",
        "parameter": [
            {"type": "template", "key": "html",            "value": PUSH_TAG_HTML},
            {"type": "boolean", "key": "supportDocumentWrite", "value": "false"},
        ],
        "firingTriggerId": [thank_you_trigger_id],
        # Tag sequencing: conversion tag fires after this tag
        "tagFiringOption": "oncePerEvent",
    }
    result = service.accounts().containers().workspaces().tags().create(
        parent=ws_path, body=body
    ).execute()
    print(f"  ✅ Created tag: {PUSH_TAG_NAME}")
    return result


# ── Step 4: Update conversion tag tag-sequencing ──────────────────────────────

def update_conversion_tag_sequencing(service, ws_path, conversion_tag, push_tag):
    """
    Set the conversion tag to fire AFTER the push tag using GTM tag sequencing.
    This replaces the need to manually set tag sequencing in the UI.
    """
    tag_id = conversion_tag["tagId"]
    resource_name = conversion_tag["path"]

    # Get the current tag (fresh copy)
    current = service.accounts().containers().workspaces().tags().get(
        path=resource_name
    ).execute()

    # Add setup tag (fires before)
    setup_tag = {
        "tagName": push_tag["name"],
        "stopOnSetupFailure": False,
    }

    # Only update if not already set
    existing_setup = current.get("setupTag", [])
    for s in existing_setup:
        if s.get("tagName") == push_tag["name"]:
            print(f"  ✓ Tag sequencing already set: {push_tag['name']} → {current['name']}")
            return current

    body = dict(current)
    body["setupTag"] = existing_setup + [setup_tag]

    result = service.accounts().containers().workspaces().tags().update(
        path=resource_name, body=body
    ).execute()
    print(f"  ✅ Tag sequencing set: '{push_tag['name']}' fires before '{current['name']}'")
    return result


# ── Step 5: Update EC field on conversion tag ─────────────────────────────────

CONVERSION_TAG_NAME = "Thank-you-page conversion"

def update_ec_on_conversion_tag(service, ws_path, conversion_tag, email_var):
    """
    Replace {{Auto Collection}} with {{dlv - user_data.email}} on the conversion tag
    and add the userProvidedData parameters for email and phone.
    """
    resource_name = conversion_tag["path"]
    current = service.accounts().containers().workspaces().tags().get(
        path=resource_name
    ).execute()

    params = current.get("parameter", [])

    # Check current EC setting
    ec_param = next((p for p in params if p.get("key") == "enableEnhancedConversion"), None)
    if ec_param:
        print(f"  ✓ enableEnhancedConversion is already set to: {ec_param.get('value')}")

    # Build updated parameters
    new_params = []
    changed = False
    for p in params:
        key = p.get("key", "")
        val = p.get("value", "")

        if key == "cssProvidedEnhancedConversionValue":
            # Replace Auto Collection with our DLV
            new_ref = var_ref(email_var)
            if val != new_ref:
                new_params.append({"type": "template", "key": key, "value": new_ref})
                print(f"  ✅ Replacing cssProvidedEnhancedConversionValue: {val!r} → {new_ref!r}")
                changed = True
            else:
                new_params.append(p)
                print(f"  ✓ cssProvidedEnhancedConversionValue already correct: {val!r}")
        elif key == "enableEnhancedConversion":
            # Ensure EC is enabled
            if val != "true":
                new_params.append({"type": "boolean", "key": key, "value": "true"})
                print(f"  ✅ Setting enableEnhancedConversion → true")
                changed = True
            else:
                new_params.append(p)
        else:
            new_params.append(p)

    # If cssProvidedEnhancedConversionValue wasn't there, add it
    keys_present = {p.get("key") for p in new_params}
    if "cssProvidedEnhancedConversionValue" not in keys_present:
        new_ref = var_ref(email_var)
        new_params.append({"type": "template", "key": "cssProvidedEnhancedConversionValue", "value": new_ref})
        print(f"  ✅ Added cssProvidedEnhancedConversionValue: {new_ref!r}")
        changed = True

    if "enableEnhancedConversion" not in keys_present:
        new_params.append({"type": "boolean", "key": "enableEnhancedConversion", "value": "true"})
        print(f"  ✅ Added enableEnhancedConversion: true")
        changed = True

    if not changed:
        print(f"  ✓ Conversion tag EC settings already correct — no update needed")
        return current

    body = dict(current)
    body["parameter"] = new_params
    result = service.accounts().containers().workspaces().tags().update(
        path=resource_name, body=body
    ).execute()
    print(f"  ✅ Updated conversion tag: {CONVERSION_TAG_NAME}")
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("GTM Enhanced Conversions Fix — Example Client")
    print("=" * 60)

    print("\nAuthenticating with GTM API...")
    service = get_gtm_service()

    print("\nLocating container...")
    account_id, container_id = find_container(service)
    print(f"  Account: {account_id}  Container: {container_id}")

    workspace_id = get_workspace(service, account_id, container_id)
    ws_path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
    print(f"  Workspace: {workspace_id}")

    print("\nFetching current container state...")
    variables = list_variables(service, ws_path)
    tags      = list_tags(service, ws_path)
    triggers  = list_triggers(service, ws_path)
    print(f"  Variables: {len(variables)}, Tags: {len(tags)}, Triggers: {len(triggers)}")

    # Find conversion tag
    conversion_tag = find_by_name(tags, CONVERSION_TAG_NAME)
    if not conversion_tag:
        # Try partial match
        for t in tags:
            if "thank" in t.get("name", "").lower() or "conversion" in t.get("name", "").lower():
                print(f"  Found similar tag: {t['name']}")
        raise RuntimeError(
            f"Tag '{CONVERSION_TAG_NAME}' not found. "
            "Check the name above and update CONVERSION_TAG_NAME."
        )
    print(f"\n  Found conversion tag: {conversion_tag['name']} (ID: {conversion_tag['tagId']})")

    print("\n── Step 1: Data Layer Variables ──────────────────────────────")
    # Re-fetch after any changes
    variables = list_variables(service, ws_path)
    email_var = ensure_dlv(service, ws_path, variables, "dlv - user_data.email",       "user_data.email")
    _         = ensure_dlv(service, ws_path, variables, "dlv - user_data.phone_number", "user_data.phone_number")

    print("\n── Step 2: Capture Tag (contact form page) ───────────────────")
    tags = list_tags(service, ws_path)
    ensure_capture_tag(service, ws_path, tags, triggers)

    print("\n── Step 3: Push Tag (thank-you page) ─────────────────────────")
    tags = list_tags(service, ws_path)
    push_tag = ensure_push_tag(service, ws_path, tags, triggers, conversion_tag)

    print("\n── Step 4: Tag Sequencing ────────────────────────────────────")
    # Re-fetch conversion tag (might have changed)
    tags = list_tags(service, ws_path)
    conversion_tag = find_by_name(tags, CONVERSION_TAG_NAME)
    update_conversion_tag_sequencing(service, ws_path, conversion_tag, push_tag)

    print("\n── Step 5: EC Field on Conversion Tag ────────────────────────")
    tags = list_tags(service, ws_path)
    conversion_tag = find_by_name(tags, CONVERSION_TAG_NAME)
    update_ec_on_conversion_tag(service, ws_path, conversion_tag, email_var)

    print("\n" + "=" * 60)
    print("Done. Next steps:")
    print("  1. Review and publish the workspace in GTM (Submit → Publish)")
    print("  2. Test: submit the contact form, check GTM preview on thank-you page")
    print("     → dataLayer should contain user_data.email after the push tag fires")
    print("  3. Allow 24–48h for Google Ads EC diagnostics to refresh")
    print("=" * 60)


if __name__ == "__main__":
    main()
