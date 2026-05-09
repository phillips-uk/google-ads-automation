"""
GA4 Data API — traffic, channel, page, event, and device reporting.

This module provides data-fetching functions for the GA4 Reporting API
(BetaAnalyticsDataClient). It is NOT the Admin API — it pulls actual
visitor data, not property configuration.

Used by analytics_report.py. Can also be imported standalone.

Auth: reuses ga4_auth.get_credentials() — same token, same scopes.
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ga4_auth import get_credentials
from google.auth.transport.requests import Request

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
    OrderBy,
)


# ── Auth ───────────────────────────────────────────────────────────────────────

def get_data_client():
    """Return a BetaAnalyticsDataClient with a fresh token."""
    creds = get_credentials()
    creds.refresh(Request())  # always refresh — token file doesn't store expiry
    return BetaAnalyticsDataClient(credentials=creds)


# ── Date helpers ───────────────────────────────────────────────────────────────

def _iso(d):
    return d.isoformat()


def get_date_ranges(days=28):
    """Returns (current_start, current_end, prev_start, prev_end) as ISO strings."""
    today   = date.today()
    cur_end   = today - timedelta(days=1)
    cur_start = cur_end - timedelta(days=days - 1)
    prev_end  = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    return _iso(cur_start), _iso(cur_end), _iso(prev_start), _iso(prev_end)


# ── Core runner ────────────────────────────────────────────────────────────────

def _run(client, property_id, dimensions, metrics, date_range, order_by=None, limit=100):
    """Generic report runner. Returns list of dicts {dim: val, metric: val}."""
    dim_names    = [d if isinstance(d, str) else d for d in dimensions]
    metric_names = [m if isinstance(m, str) else m for m in metrics]

    request = RunReportRequest(
        property=property_id,
        dimensions=[Dimension(name=d) for d in dim_names],
        metrics=[Metric(name=m) for m in metric_names],
        date_ranges=[DateRange(start_date=date_range[0], end_date=date_range[1])],
        order_bys=order_by or [],
        limit=limit,
    )
    response = client.run_report(request)

    rows = []
    for row in response.rows:
        r = {}
        for i, dv in enumerate(row.dimension_values):
            r[dim_names[i]] = dv.value
        for i, mv in enumerate(row.metric_values):
            r[metric_names[i]] = mv.value
        rows.append(r)
    return rows


# ── Reports ────────────────────────────────────────────────────────────────────

def get_overview(client, property_id, cur_start, cur_end, prev_start, prev_end):
    """
    High-level traffic totals for two periods.
    Returns {"current": {...}, "previous": {...}}
    """
    metrics = [
        "sessions", "activeUsers", "newUsers",
        "engagementRate", "averageSessionDuration",
        "screenPageViews", "conversions",
    ]

    _empty = {
        "sessions": 0, "users": 0, "new_users": 0,
        "eng_rate": 0.0, "avg_session": 0.0,
        "pageviews": 0, "conversions": 0,
    }

    def fetch_totals(start, end):
        rows = _run(client, property_id, [], metrics, (start, end), limit=1)
        if not rows:
            return dict(_empty)
        row = rows[0]
        return {
            "sessions":    int(float(row.get("sessions", 0))),
            "users":       int(float(row.get("activeUsers", 0))),
            "new_users":   int(float(row.get("newUsers", 0))),
            "eng_rate":    round(float(row.get("engagementRate", 0)) * 100, 1),
            "avg_session": round(float(row.get("averageSessionDuration", 0)), 0),
            "pageviews":   int(float(row.get("screenPageViews", 0))),
            "conversions": int(float(row.get("conversions", 0))),
        }

    return {
        "current":  fetch_totals(cur_start, cur_end),
        "previous": fetch_totals(prev_start, prev_end),
    }


def get_channel_breakdown(client, property_id, start, end, limit=15):
    """
    Sessions and conversions by channel group.
    Returns list of dicts sorted by sessions desc.
    """
    rows = _run(
        client, property_id,
        ["sessionDefaultChannelGroup"],
        ["sessions", "activeUsers", "conversions", "engagementRate"],
        (start, end),
        order_by=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        limit=limit,
    )
    result = []
    for row in rows:
        result.append({
            "channel":     row.get("sessionDefaultChannelGroup", "Unknown"),
            "sessions":    int(float(row.get("sessions", 0))),
            "users":       int(float(row.get("activeUsers", 0))),
            "conversions": int(float(row.get("conversions", 0))),
            "eng_rate":    round(float(row.get("engagementRate", 0)) * 100, 1),
        })
    return result


def get_top_pages(client, property_id, start, end, limit=15):
    """
    Top landing pages by sessions.
    Returns list of dicts sorted by sessions desc.
    """
    rows = _run(
        client, property_id,
        ["pagePath"],
        ["sessions", "screenPageViews", "activeUsers", "engagementRate"],
        (start, end),
        order_by=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        limit=limit,
    )
    result = []
    for row in rows:
        result.append({
            "page":      row.get("pagePath", "/"),
            "sessions":  int(float(row.get("sessions", 0))),
            "pageviews": int(float(row.get("screenPageViews", 0))),
            "users":     int(float(row.get("activeUsers", 0))),
            "eng_rate":  round(float(row.get("engagementRate", 0)) * 100, 1),
        })
    return result


def get_key_events(client, property_id, start, end, limit=20):
    """
    Event counts for all events. Caller filters to key events if needed.
    Returns list of {event_name, count} sorted by count desc.
    """
    rows = _run(
        client, property_id,
        ["eventName"],
        ["eventCount", "conversions"],
        (start, end),
        order_by=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="eventCount"), desc=True)],
        limit=limit,
    )
    result = []
    for row in rows:
        result.append({
            "event":       row.get("eventName", ""),
            "count":       int(float(row.get("eventCount", 0))),
            "conversions": int(float(row.get("conversions", 0))),
        })
    return result


def get_device_breakdown(client, property_id, start, end):
    """Sessions by device category."""
    rows = _run(
        client, property_id,
        ["deviceCategory"],
        ["sessions", "activeUsers"],
        (start, end),
        order_by=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        limit=10,
    )
    result = []
    for row in rows:
        result.append({
            "device":   row.get("deviceCategory", ""),
            "sessions": int(float(row.get("sessions", 0))),
            "users":    int(float(row.get("activeUsers", 0))),
        })
    return result


def get_source_medium(client, property_id, start, end, limit=15):
    """Sessions by source/medium — more granular than channel group."""
    rows = _run(
        client, property_id,
        ["sessionSourceMedium"],
        ["sessions", "activeUsers", "conversions"],
        (start, end),
        order_by=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        limit=limit,
    )
    result = []
    for row in rows:
        result.append({
            "source_medium": row.get("sessionSourceMedium", ""),
            "sessions":      int(float(row.get("sessions", 0))),
            "users":         int(float(row.get("activeUsers", 0))),
            "conversions":   int(float(row.get("conversions", 0))),
        })
    return result


def get_daily_sessions(client, property_id, start, end):
    """Daily session count for sparkline / trend data."""
    rows = _run(
        client, property_id,
        ["date"],
        ["sessions"],
        (start, end),
        order_by=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))],
        limit=90,
    )
    return [
        {"date": row.get("date", ""), "sessions": int(float(row.get("sessions", 0)))}
        for row in rows
    ]


# ── Full fetch ─────────────────────────────────────────────────────────────────

def fetch_all(property_id, days=28):
    """
    Run all reports for a single property. Returns a data dict.
    Used by analytics_report.py.
    """
    client = get_data_client()
    cur_start, cur_end, prev_start, prev_end = get_date_ranges(days)

    print(f"    Fetching GA4 data for {property_id}...")
    print(f"    Period: {cur_start} → {cur_end}  |  Compare: {prev_start} → {prev_end}")

    overview   = get_overview(client, property_id, cur_start, cur_end, prev_start, prev_end)
    channels   = get_channel_breakdown(client, property_id, cur_start, cur_end)
    pages      = get_top_pages(client, property_id, cur_start, cur_end)
    events     = get_key_events(client, property_id, cur_start, cur_end)
    devices    = get_device_breakdown(client, property_id, cur_start, cur_end)
    sources    = get_source_medium(client, property_id, cur_start, cur_end)
    daily      = get_daily_sessions(client, property_id, cur_start, cur_end)

    print(f"    Sessions: {overview['current']['sessions']:,}  |  "
          f"Users: {overview['current']['users']:,}  |  "
          f"Conversions: {overview['current']['conversions']:,}")

    return {
        "property_id": property_id,
        "period": {"start": cur_start, "end": cur_end,
                   "prev_start": prev_start, "prev_end": prev_end},
        "overview":  overview,
        "channels":  channels,
        "pages":     pages,
        "events":    events,
        "devices":   devices,
        "sources":   sources,
        "daily":     daily,
    }
