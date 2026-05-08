"""GA4 Admin API OAuth helper — reuses gtm_client.json Desktop credentials."""

import json
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.analytics.admin import AnalyticsAdminServiceClient
from google.analytics.admin_v1alpha import AnalyticsAdminServiceClient as AnalyticsAdminServiceClientAlpha

GA4_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/analytics.edit",
]

_DIR        = os.path.dirname(__file__)
TOKEN_FILE  = os.path.join(_DIR, "ga4_token.json")
CLIENT_FILE = os.path.join(_DIR, "gtm_client.json")


def _save_token(creds):
    with open(TOKEN_FILE, "w") as f:
        json.dump({
            "token":         creds.token,
            "refresh_token": creds.refresh_token,
            "client_id":     creds.client_id,
            "client_secret": creds.client_secret,
        }, f)


def get_credentials():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            td = json.load(f)
        creds = Credentials(
            token=td.get("token"),
            refresh_token=td.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=td.get("client_id"),
            client_secret=td.get("client_secret"),
            scopes=GA4_SCOPES,
        )
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds)
            return creds

    with open(CLIENT_FILE) as f:
        gc = json.load(f)
    client_config = {"installed": gc["installed"]}
    flow = InstalledAppFlow.from_client_config(client_config, scopes=GA4_SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_token(creds)
    return creds


def get_admin_client():
    """Return an AnalyticsAdminServiceClient authenticated via OAuth."""
    from google.auth.credentials import Credentials as BaseCredentials
    creds = get_credentials()
    return AnalyticsAdminServiceClient(credentials=creds)
