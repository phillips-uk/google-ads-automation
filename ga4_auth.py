"""
GA4 Admin API auth helper — service account.

Replaces the previous OAuth flow (ga4_token.json + gtm_client.json).
No token expiry. No browser prompts. Key file: ga4_data_sa.json.

Access grant (one-time per property):
  GA4 → Admin → Property Access Management → Add user
  Email: ga4-data-api@groovy-sentry-494808-n2.iam.gserviceaccount.com
  Role: Editor
"""

import os
from google.analytics.admin import AnalyticsAdminServiceClient
from service_account import get_ga4_admin_credentials


def get_credentials():
    """Return service account credentials for GA4 Admin API."""
    return get_ga4_admin_credentials()


def get_admin_client() -> AnalyticsAdminServiceClient:
    """Return an AnalyticsAdminServiceClient authenticated via service account."""
    return AnalyticsAdminServiceClient(credentials=get_ga4_admin_credentials())
