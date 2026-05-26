"""
Shared service account authentication helper.

Replaces all OAuth token files (ga4_token.json, gtm_token.json, sc_token.json)
with a single non-expiring service account key.

Key file: ga4_data_sa.json
Service account: ga4-data-api@groovy-sentry-494808-n2.iam.gserviceaccount.com
GCP project:     groovy-sentry-494808-n2

── Why service account instead of OAuth ──────────────────────────────────────
OAuth installed-app tokens (what gtm_token.json, ga4_token.json, sc_token.json
were) expire after 6 months of inactivity, or after 7 days if the GCP app is
in Testing status. Service account keys do not expire — they are valid until
you manually revoke them in GCP Console.

── Access grants required (one-time setup per service) ───────────────────────
After adding a new client property, grant the service account access in:

GA4 Admin API:
  GA4 → Admin → Property Access Management → Add user
  Email: ga4-data-api@groovy-sentry-494808-n2.iam.gserviceaccount.com
  Role: Editor (needed for audience creation, property updates)

GTM API:
  GTM → Admin → User Management (account level) → Add user
  Email: ga4-data-api@groovy-sentry-494808-n2.iam.gserviceaccount.com
  Account: Read — Container: Publish (needed for tag reads and publishes)

Search Console:
  GSC → Settings → Users and permissions → Add user
  Email: ga4-data-api@groovy-sentry-494808-n2.iam.gserviceaccount.com
  Permission: Full

── Scopes ────────────────────────────────────────────────────────────────────
GA4 Admin:  analytics.readonly + analytics.edit + analytics.manage.users
GTM:        tagmanager.readonly + tagmanager.edit.containers + tagmanager.publish
SC:         webmasters.readonly

All are requested from the same key file. Google only grants the scopes the
service account has been authorised for at the property/container level.

── Key rotation ──────────────────────────────────────────────────────────────
If the key is ever compromised or you need to rotate:
  GCP Console → IAM → Service accounts → ga4-data-api → Keys → Add key → JSON
  Save as ga4_data_sa.json, delete the old one.
No code changes needed.
"""

import os
import sys

from google.oauth2 import service_account

_DIR    = os.path.dirname(__file__)
SA_FILE = os.path.join(_DIR, "ga4_data_sa.json")

SA_EMAIL = "ga4-data-api@groovy-sentry-494808-n2.iam.gserviceaccount.com"

# Scope constants — import from here to keep them consistent everywhere
SCOPES_GA4_ADMIN = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/analytics.edit",
    "https://www.googleapis.com/auth/analytics.manage.users",
]
SCOPES_GTM = [
    "https://www.googleapis.com/auth/tagmanager.readonly",
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
    "https://www.googleapis.com/auth/tagmanager.publish",
]
SCOPES_SC = [
    "https://www.googleapis.com/auth/webmasters.readonly",
]


def _check_key_file():
    if not os.path.exists(SA_FILE):
        print(f"""
[service_account] ERROR: Key file not found: {SA_FILE}

To fix:
  GCP Console → IAM & Admin → Service accounts
  → ga4-data-api@groovy-sentry-494808-n2.iam.gserviceaccount.com
  → Keys → Add key → Create new key → JSON
  Save as: {SA_FILE}
""")
        sys.exit(1)


def get_credentials(scopes: list) -> service_account.Credentials:
    """
    Return service account credentials for the given scopes.
    No token expiry — credentials are valid until the key is revoked.
    """
    _check_key_file()
    return service_account.Credentials.from_service_account_file(
        SA_FILE, scopes=scopes
    )


def get_ga4_admin_credentials() -> service_account.Credentials:
    """Credentials for GA4 Admin API (property config, audiences, custom dims, etc.)"""
    return get_credentials(SCOPES_GA4_ADMIN)


def get_gtm_credentials() -> service_account.Credentials:
    """Credentials for GTM API (tags, triggers, variables, publish)."""
    return get_credentials(SCOPES_GTM)


def get_sc_credentials() -> service_account.Credentials:
    """Credentials for Search Console API (query performance, site list)."""
    return get_credentials(SCOPES_SC)
