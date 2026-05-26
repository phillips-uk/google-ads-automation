"""
GA4 Data API — Service Account Auth
====================================
Standalone module for querying GA4 reporting and Realtime data.
Uses a dedicated service account (ga4_data_sa.json) — non-expiring, no browser.

Setup (one-time):
  1. GCP Console → groovy-sentry-494808-n2 → APIs & Services → Enable:
       "Google Analytics Data API"  (analyticsdata.googleapis.com)
  2. GCP Console → IAM & admin → Service accounts → Create:
       Name: ga4-data-api
       Email will be: ga4-data-api@groovy-sentry-494808-n2.iam.gserviceaccount.com
       → Create and continue → Done
       → Click the account → Keys → Add key → JSON → download
       → Save as ga4_data_sa.json in this project root
  3. GA4 Admin → (Lee Renée property) → Property access management → + Add users:
       Email: ga4-data-api@groovy-sentry-494808-n2.iam.gserviceaccount.com
       Role: Viewer → Add
  4. python3 ga4_data.py   ← runs a self-test

Usage:
    from ga4_data import GA4DataClient
    client = GA4DataClient(property_id="393708418")

    # Channel attribution breakdown (last 30 days)
    rows = client.channel_attribution(days=30)

    # Realtime events
    rows = client.realtime_events()

    # Conversion funnel
    rows = client.conversion_funnel(days=7)
"""

import os
from datetime import date, timedelta

from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest,
    RunRealtimeReportRequest,
    Dimension,
    Metric,
    DateRange,
    OrderBy,
    FilterExpression,
    Filter,
)

SA_KEY_FILE  = os.path.join(os.path.dirname(__file__), "ga4_data_sa.json")
GA4_TOKEN    = os.path.join(os.path.dirname(__file__), "ga4_token.json")
GTM_CLIENT   = os.path.join(os.path.dirname(__file__), "gtm_client.json")
SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]


def _sa_client() -> BetaAnalyticsDataClient:
    """Service account client (primary)."""
    creds = service_account.Credentials.from_service_account_file(
        SA_KEY_FILE, scopes=SCOPES
    )
    return BetaAnalyticsDataClient(credentials=creds)


def _oauth_client() -> BetaAnalyticsDataClient:
    """Lewis's OAuth client (fallback for properties not yet granted to SA)."""
    import json
    from google.oauth2.credentials import Credentials as OAuthCredentials
    from google.auth.transport.requests import Request

    with open(GA4_TOKEN) as f:
        td = json.load(f)
    with open(GTM_CLIENT) as f:
        cr = json.load(f)
    c = cr.get("installed") or cr.get("web") or cr
    creds = OAuthCredentials(
        token=td.get("token"),
        refresh_token=td["refresh_token"],
        client_id=c["client_id"],
        client_secret=c["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    if not creds.valid:
        creds.refresh(Request())
        td["token"] = creds.token
        with open(GA4_TOKEN, "w") as f:
            json.dump(td, f, indent=2)
    return BetaAnalyticsDataClient(credentials=creds)


def _get_client() -> BetaAnalyticsDataClient:
    """Return a GA4 Data API client (service account, no expiry)."""
    if not os.path.exists(SA_KEY_FILE):
        raise FileNotFoundError(
            f"[ga4_data] Service account key not found: {SA_KEY_FILE}\n"
            "  Follow the setup steps at the top of ga4_data.py"
        )
    return _sa_client()


class GA4DataClient:
    """Thin wrapper around the GA4 Data API for common reporting tasks.

    Auth order:
    1. Service account (ga4_data_sa.json) — preferred, non-expiring.
    2. Lewis's OAuth token (ga4_token.json) — fallback for properties where
       the SA has not yet been granted access (Lee Renée, Engine House).
    """

    def __init__(self, property_id: str):
        self.property = f"properties/{property_id}"
        self._client = _get_client()  # SA by default; _run/_run_realtime fall back to OAuth on 403

    def _run(self, request):
        """Execute a report request, falling back to OAuth if SA gets 403."""
        from google.api_core.exceptions import PermissionDenied
        try:
            return self._client.run_report(request)
        except PermissionDenied:
            if os.path.exists(GA4_TOKEN):
                self._client = _oauth_client()
                return self._client.run_report(request)
            raise

    def _run_realtime(self, request):
        """Execute a realtime report request with OAuth fallback."""
        from google.api_core.exceptions import PermissionDenied
        try:
            return self._client.run_realtime_report(request)
        except PermissionDenied:
            if os.path.exists(GA4_TOKEN):
                self._client = _oauth_client()
                return self._client.run_realtime_report(request)
            raise

    # ── Realtime ─────────────────────────────────────────────────────────────

    def realtime_events(self) -> list[dict]:
        """Events fired in the last 30 minutes with counts."""
        resp = self._run_realtime(RunRealtimeReportRequest(
            property=self.property,
            dimensions=[Dimension(name="eventName")],
            metrics=[Metric(name="eventCount")],
        ))
        return [
            {
                "event": r.dimension_values[0].value,
                "count": int(r.metric_values[0].value),
            }
            for r in resp.rows
        ]

    def realtime_active_users(self) -> int:
        """Active users in the last 5 minutes."""
        resp = self._run_realtime(RunRealtimeReportRequest(
            property=self.property,
            metrics=[Metric(name="activeUsers")],
        ))
        if resp.rows:
            return int(resp.rows[0].metric_values[0].value)
        return 0

    # ── Attribution ───────────────────────────────────────────────────────────

    def channel_attribution(self, days: int = 30) -> list[dict]:
        """
        Session channel breakdown for the last N days.
        Returns rows: channel, sessions, key_events, revenue.
        """
        end = date.today()
        start = end - timedelta(days=days)
        resp = self._run(RunReportRequest(
            property=self.property,
            date_ranges=[DateRange(
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )],
            dimensions=[Dimension(name="sessionDefaultChannelGroup")],
            metrics=[
                Metric(name="sessions"),
                Metric(name="keyEvents"),
                Metric(name="totalRevenue"),
            ],
            order_bys=[OrderBy(
                metric=OrderBy.MetricOrderBy(metric_name="sessions"),
                desc=True,
            )],
        ))
        return [
            {
                "channel":    r.dimension_values[0].value,
                "sessions":   int(r.metric_values[0].value),
                "key_events": int(r.metric_values[1].value),
                "revenue":    float(r.metric_values[2].value),
            }
            for r in resp.rows
        ]

    def purchase_attribution(self, days: int = 30) -> list[dict]:
        """
        Purchase conversions by session channel — the key attribution check.
        Use this to verify the Direct % is dropping after the MP pixel fix.
        """
        end = date.today()
        start = end - timedelta(days=days)
        resp = self._run(RunReportRequest(
            property=self.property,
            date_ranges=[DateRange(
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )],
            dimensions=[Dimension(name="sessionDefaultChannelGroup")],
            metrics=[
                Metric(name="ecommercePurchases"),
                Metric(name="purchaseRevenue"),
                Metric(name="sessions"),
            ],
            order_bys=[OrderBy(
                metric=OrderBy.MetricOrderBy(metric_name="ecommercePurchases"),
                desc=True,
            )],
        ))
        rows = [
            {
                "channel":   r.dimension_values[0].value,
                "purchases": int(r.metric_values[0].value),
                "revenue":   float(r.metric_values[1].value),
                "sessions":  int(r.metric_values[2].value),
            }
            for r in resp.rows
        ]
        total = sum(r["purchases"] for r in rows) or 1
        for r in rows:
            r["pct"] = round(r["purchases"] / total * 100, 1)
        return rows

    # ── Conversion funnel ─────────────────────────────────────────────────────

    def conversion_funnel(self, days: int = 7) -> list[dict]:
        """view_item → add_to_cart → begin_checkout → purchase event counts."""
        end = date.today()
        start = end - timedelta(days=days)
        events = ["view_item", "add_to_cart", "begin_checkout", "purchase"]
        resp = self._run(RunReportRequest(
            property=self.property,
            date_ranges=[DateRange(
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )],
            dimensions=[Dimension(name="eventName")],
            metrics=[Metric(name="eventCount")],
            dimension_filter=FilterExpression(
                filter=Filter(
                    field_name="eventName",
                    in_list_filter=Filter.InListFilter(values=events),
                )
            ),
        ))
        result = {e: 0 for e in events}
        for r in resp.rows:
            name = r.dimension_values[0].value
            if name in result:
                result[name] = int(r.metric_values[0].value)
        return [{"event": e, "count": result[e]} for e in events]

    # ── Weekly performance ─────────────────────────────────────────────────────

    def weekly_performance(self, days: int = 7) -> dict:
        """
        Top-line weekly metrics: sessions, users, key events, revenue, ROAS proxy.
        """
        end = date.today()
        start = end - timedelta(days=days)
        resp = self._run(RunReportRequest(
            property=self.property,
            date_ranges=[DateRange(
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )],
            metrics=[
                Metric(name="sessions"),
                Metric(name="activeUsers"),
                Metric(name="keyEvents"),
                Metric(name="ecommercePurchases"),
                Metric(name="purchaseRevenue"),
                Metric(name="bounceRate"),
            ],
        ))
        if not resp.rows:
            return {}
        v = resp.rows[0].metric_values
        return {
            "sessions":   int(v[0].value),
            "users":      int(v[1].value),
            "key_events": int(v[2].value),
            "purchases":  int(v[3].value),
            "revenue":    float(v[4].value),
            "bounce_rate": round(float(v[5].value) * 100, 1),
        }


# ── Self-test ──────────────────────────────────────────────────────────────────

def _self_test():
    """Quick connectivity test. Run: python3 ga4_data.py"""
    from config import load_env
    load_env()

    # Load property ID from clients.py
    try:
        from clients import GA4_PROPERTY_IDS
        property_id = GA4_PROPERTY_IDS.get("Lee Renee Jewellery", "393708418")
    except ImportError:
        property_id = "393708418"

    print(f"\nGA4 Data API — self-test (property {property_id})")
    print("=" * 55)

    client = GA4DataClient(property_id=property_id)

    print("\nRealtime active users:", client.realtime_active_users())

    print("\nRealtime events (last 30 min):")
    for row in client.realtime_events():
        print(f"  {row['event']}: {row['count']}")

    print("\nPurchase attribution (last 30 days):")
    for row in client.purchase_attribution(days=30):
        print(f"  {row['channel']}: {row['purchases']} purchases ({row['pct']}%) — £{row['revenue']:.2f}")

    print("\nConversion funnel (last 7 days):")
    for row in client.conversion_funnel(days=7):
        print(f"  {row['event']}: {row['count']}")

    print("\n✅ GA4 Data API OK\n")


if __name__ == "__main__":
    _self_test()
