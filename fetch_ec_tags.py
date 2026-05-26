"""Fetch EC tags from Engine House GTM container (GTM-PBLR8JNZ)."""
import warnings; warnings.filterwarnings("ignore")
import json, requests, sys

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

with open("gtm_client.json") as f:
    client_config = json.load(f)["installed"]
with open("gtm_token.json") as f:
    token_data = json.load(f)

creds = Credentials(
    token=token_data.get("token"),
    refresh_token=token_data.get("refresh_token"),
    token_uri=client_config["token_uri"],
    client_id=client_config["client_id"],
    client_secret=client_config["client_secret"],
)
if creds.expired:
    creds.refresh(Request())
    print("[token refreshed]")

headers = {"Authorization": f"Bearer {creds.token}"}
BASE = "https://www.googleapis.com/tagmanager/v2"

accounts = requests.get(f"{BASE}/accounts", headers=headers).json()
print(f"Accounts found: {[a['accountId'] for a in accounts.get('account', [])]}")

for acct in accounts.get("account", []):
    containers = requests.get(
        f"{BASE}/accounts/{acct['accountId']}/containers", headers=headers
    ).json()
    for c in containers.get("container", []):
        if c.get("publicId") == "GTM-PBLR8JNZ":
            acct_id = acct["accountId"]
            cont_id = c["containerId"]
            print(f"\nFound GTM-PBLR8JNZ: account={acct_id}  container={cont_id}")

            ws_resp = requests.get(
                f"{BASE}/accounts/{acct_id}/containers/{cont_id}/workspaces",
                headers=headers,
            ).json()
            ws_id = ws_resp["workspace"][0]["workspaceId"]
            ws_name = ws_resp["workspace"][0].get("name", "?")
            print(f"Workspace: {ws_id} ({ws_name})\n")

            tags_resp = requests.get(
                f"{BASE}/accounts/{acct_id}/containers/{cont_id}/workspaces/{ws_id}/tags",
                headers=headers,
            ).json()

            print(f"Total tags: {len(tags_resp.get('tag', []))}\n")

            for tag in tags_resp.get("tag", []):
                name = tag["name"]
                # Show EC-related tags
                if any(k in name for k in ["EC", "Push", "Capture", "push", "capture", "enhanced", "user"]):
                    print(f"=== {name!r}  tagId={tag['tagId']}  type={tag['type']} ===")
                    for p in tag.get("parameter", []):
                        val = p.get("value", "")
                        print(f"  [{p['key']}]\n{val[:800]}\n")
                    print()
