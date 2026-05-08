"""
Fix EC - Capture Form Email tag to attach listeners to ALL matching forms,
not just the first one (querySelectorAll instead of querySelector).
Then publish as a new version.
"""

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

NEW_HTML = """<script>
(function() {
  var forms = document.querySelectorAll('form[action*="/contact"]');
  if (!forms.length) return;
  forms.forEach(function(form) {
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
  });
})();
</script>"""


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
    print("GTM Capture Tag Fix — querySelectorAll")
    print("=" * 60)

    service = get_gtm_service()
    account_id, container_id = find_container(service)
    workspace_id = get_workspace(service, account_id, container_id)
    ws_path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
    print(f"  Container: {container_id}  Workspace: {workspace_id}")

    tags = service.accounts().containers().workspaces().tags().list(
        parent=ws_path
    ).execute().get("tag", [])

    capture = next((t for t in tags if t.get("name") == "EC - Capture Form Email"), None)
    if not capture:
        print("[ERROR] 'EC - Capture Form Email' tag not found.")
        return

    print(f"\n  Found tag: {capture['name']} (ID: {capture['tagId']})")

    # Update the html parameter
    updated = dict(capture)
    updated["parameter"] = [
        p if p.get("key") != "html" else {"type": "template", "key": "html", "value": NEW_HTML}
        for p in capture.get("parameter", [])
    ]

    try:
        result = service.accounts().containers().workspaces().tags().update(
            path=capture["path"],
            body=updated,
        ).execute()
        print(f"  ✅ Tag updated: {result['name']}")
    except HttpError as e:
        print(f"  [ERROR] {e}")
        return

    # Publish
    print("\n  Publishing as new version...")
    container_path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
    try:
        pub = service.accounts().containers().workspaces().create_version(
            path=container_path,
            body={"name": "EC capture fix — attach to all contact forms"},
        ).execute()
        version_id = pub.get("containerVersion", {}).get("containerVersionId", "?")
        version_path = f"accounts/{account_id}/containers/{container_id}/versions/{version_id}"
        service.accounts().containers().versions().publish(path=version_path).execute()
        print(f"  ✅ Published as Version {version_id}")
    except HttpError as e:
        print(f"  [ERROR publishing] {e}")
        print("  Tag updated — publish manually in GTM UI.")

    print("=" * 60)


if __name__ == "__main__":
    main()
