"""
Conversion Tracking Audit — Google Tag Manager + Google Ads

Run on demand to audit tracking health for any account/container pair.
Checks:
  - All GTM tags, triggers, variables in the live workspace
  - Google Ads conversion actions: status, type, counting, attribution
  - Enhanced Conversions configuration (tag-level + account-level)
  - Consent Mode v2 setup (consent init tags, CMP presence, tag consent settings)
  - GA4 purchase / lead gen events
  - Cross-reference: which Google Ads conversion actions have GTM tags

Usage:
  python3 tracking_audit.py

First run will open a browser to authorise GTM API access. Token saved to gtm_token.json.
"""

import os
import json
from datetime import date, timedelta
from collections import defaultdict

import yaml
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

import anthropic
from config import OBSIDIAN_BASE, load_env
from monday_helper import post_audit_issues

load_env()

# ── Constants ─────────────────────────────────────────────────────────────────

GTM_SCOPES      = [
    "https://www.googleapis.com/auth/tagmanager.readonly",
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
    "https://www.googleapis.com/auth/tagmanager.publish",
]
TOKEN_FILE      = os.path.join(os.path.dirname(__file__), "gtm_token.json")
GTM_CLIENT_FILE = os.path.join(os.path.dirname(__file__), "gtm_client.json")
YAML_FILE       = os.path.join(os.path.dirname(__file__), "google-ads.yaml")

# ── Client accounts — loaded from clients.py (gitignored, never committed) ─────
# To add accounts: edit clients.py (copy from clients.example.py if starting fresh)
try:
    from clients import AUDIT_ACCOUNTS
except ImportError:
    print("[config] ERROR: clients.py not found.")
    print("         Copy clients.example.py → clients.py and fill in your account details.")
    sys.exit(1)

# GTM tag type → human label
TAG_TYPE_LABELS = {
    "awct":   "Google Ads Conversion Tracking",
    "googtag":"Google Tag (gtag.js)",
    "gaawe":  "GA4 Event",
    "gaawc":  "GA4 Configuration",
    "ua":     "Universal Analytics (deprecated)",
    "html":   "Custom HTML",
    "img":    "Custom Image",
    "sp":     "Scroll Depth",
    "fls":    "Floodlight Counter",
    "flc":    "Floodlight Sales",
    "_rmt":   "Remarketing",
}

# Google Ads conversion action type enum → label
CONV_TYPE_LABELS = {
    2:  "Click to Website",
    3:  "App Install",
    4:  "App Action",
    5:  "Upload (Clicks)",
    6:  "Upload (Calls)",
    7:  "Phone Call",
    8:  "Website",
    9:  "Store Visit",
    10: "Store Sale",
    11: "Smart Campaign",
    12: "Manual",
}

COUNTING_LABELS = {1: "One per click", 2: "Many per click"}
ATTRIBUTION_LABELS = {
    2: "Last Click", 3: "First Click", 4: "Linear",
    5: "Time Decay", 6: "Position Based", 7: "Data Driven",
}


# ── GTM Authentication — OAuth (installed-app flow) ───────────────────────────
# GTM uses the cached OAuth token in gtm_token.json.
# The service account cannot be granted GTM manage.users without a fresh OAuth
# flow that includes tagmanager.manage.users scope — which the existing token
# was not authorised with. OAuth token does not expire if used regularly.

def _gtm_credentials():
    """OAuth credentials for GTM API. Auto-refreshes from gtm_token.json.

    First run opens a browser. Subsequent runs use the cached refresh token.
    """
    import json as _json
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    with open(GTM_CLIENT_FILE) as f:
        client_data = _json.load(f)
    creds_data = client_data.get("installed") or client_data.get("web") or client_data

    with open(TOKEN_FILE) as f:
        token_data = _json.load(f)

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        client_id=creds_data.get("client_id"),
        client_secret=creds_data.get("client_secret"),
        token_uri="https://oauth2.googleapis.com/token",
    )
    if not creds.valid:
        creds.refresh(Request())
        # Persist refreshed token
        token_data["token"] = creds.token
        with open(TOKEN_FILE, "w") as f:
            _json.dump(token_data, f, indent=2)
    return creds


def build_gtm_service():
    return build("tagmanager", "v2", credentials=_gtm_credentials(), cache_discovery=False)


# ── GTM Data Fetchers ─────────────────────────────────────────────────────────

def find_container(service, public_id):
    """Return (account_path, container) for the given public GTM ID."""
    try:
        accounts = service.accounts().list().execute().get("account", [])
    except HttpError as e:
        print(f"  [ERROR] GTM accounts list failed: {e}")
        return None, None

    for acct in accounts:
        acct_path = acct["path"]
        try:
            containers = service.accounts().containers().list(
                parent=acct_path
            ).execute().get("container", [])
        except HttpError:
            continue
        for c in containers:
            if c.get("publicId") == public_id:
                return acct_path, c

    return None, None


def get_live_workspace(service, container_path):
    """Return the Default Workspace (or first available)."""
    try:
        workspaces = service.accounts().containers().workspaces().list(
            parent=container_path
        ).execute().get("workspace", [])
    except HttpError as e:
        print(f"  [ERROR] Could not list workspaces: {e}")
        return None

    # Prefer 'Default Workspace'
    for ws in workspaces:
        if ws.get("name") == "Default Workspace":
            return ws
    return workspaces[0] if workspaces else None


def get_tags(service, workspace_path):
    try:
        return service.accounts().containers().workspaces().tags().list(
            parent=workspace_path
        ).execute().get("tag", [])
    except HttpError as e:
        print(f"  [WARN] Could not fetch tags: {e}")
        return []


def get_triggers(service, workspace_path):
    try:
        return service.accounts().containers().workspaces().triggers().list(
            parent=workspace_path
        ).execute().get("trigger", [])
    except HttpError as e:
        print(f"  [WARN] Could not fetch triggers: {e}")
        return []


def get_variables(service, workspace_path):
    try:
        return service.accounts().containers().workspaces().variables().list(
            parent=workspace_path
        ).execute().get("variable", [])
    except HttpError as e:
        print(f"  [WARN] Could not fetch variables: {e}")
        return []


# ── GA4 / Spend Tracking Continuity Check ─────────────────────────────────────

def fetch_daily_ga4_sessions(property_id: str, days: int = 14) -> dict:
    """
    Returns {date_str: session_count} for the past N days using the GA4 Data API.
    Uses GA4DataClient which tries SA first then falls back to OAuth on 403.
    Returns an empty dict on any error (non-fatal — continuity check is best-effort).
    """
    try:
        from ga4_data import GA4DataClient
        from google.analytics.data_v1beta.types import (
            RunReportRequest, Dimension, Metric, DateRange,
        )
        end   = date.today() - timedelta(days=1)
        start = end - timedelta(days=days - 1)
        ga4 = GA4DataClient(property_id)
        resp = ga4._run(RunReportRequest(
            property=ga4.property,
            date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
            dimensions=[Dimension(name="date")],
            metrics=[Metric(name="sessions")],
        ))
        return {row.dimension_values[0].value: int(row.metric_values[0].value) for row in resp.rows}
    except Exception as e:
        print(f"    [WARN] GA4 daily sessions fetch failed: {e}")
        return {}


def fetch_daily_ads_spend(ads_client, customer_id: str, days: int = 14) -> dict:
    """
    Returns {date_str: spend_gbp} summed across all campaigns for the past N days.
    """
    end   = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    ga_service = ads_client.get_service("GoogleAdsService")
    query = f"""
        SELECT segments.date, metrics.cost_micros
        FROM campaign
        WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'
          AND campaign.status = 'ENABLED'
    """
    daily: dict = {}
    try:
        for row in ga_service.search(customer_id=customer_id, query=query):
            d = row.segments.date
            daily[d] = daily.get(d, 0.0) + row.metrics.cost_micros / 1_000_000
    except Exception as e:
        print(f"    [WARN] Google Ads daily spend fetch failed: {e}")
    return {d: round(v, 2) for d, v in daily.items()}


def check_tracking_continuity(property_id: str, ads_client, customer_id: str, days: int = 14) -> list:
    """
    Cross-references GA4 daily sessions against Google Ads daily spend.
    Flags any day where:
      spend > £50  AND  GA4 sessions == 0  →  CRITICAL  (total blackout)
      spend > £50  AND  GA4 sessions < 10  →  HIGH      (near-blackout)
    Returns a list of issue dicts compatible with findings["issues"].
    """
    ga4_sessions = fetch_daily_ga4_sessions(property_id, days)
    ads_spend    = fetch_daily_ads_spend(ads_client, customer_id, days)

    if not ads_spend:
        return []

    issues = []
    for day, spend in sorted(ads_spend.items()):
        if spend < 50:
            continue
        sessions = ga4_sessions.get(day, 0)
        if sessions == 0:
            issues.append({
                "severity": "CRITICAL",
                "msg": (
                    f"Tracking blackout {day}: £{spend:.2f} GAds spend, 0 GA4 sessions. "
                    "Server-side app disconnected or GTM not firing. "
                    "Check Shopify → Google & YouTube → Connected services → Google Analytics tab."
                ),
            })
        elif sessions < 10:
            issues.append({
                "severity": "HIGH",
                "msg": (
                    f"Near-blackout {day}: £{spend:.2f} GAds spend, only {sessions} GA4 session(s). "
                    "Possible partial tracking failure — verify app connection."
                ),
            })
    return issues


def fetch_ga4_channel_breakdown(property_id: str, days: int = 30) -> dict:
    """
    Pull GA4 session channel breakdown for the last N days.
    Returns {channel: {sessions, pct}} ordered by session count desc.
    Uses GA4DataClient (SA primary, OAuth fallback).
    Returns {} on any error (non-fatal).
    """
    try:
        from ga4_data import GA4DataClient
        from google.analytics.data_v1beta.types import (
            RunReportRequest, Dimension, Metric, DateRange, OrderBy,
        )
        end   = date.today() - timedelta(days=1)
        start = end - timedelta(days=days - 1)
        ga4 = GA4DataClient(property_id)
        resp = ga4._run(RunReportRequest(
            property=ga4.property,
            date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
            dimensions=[Dimension(name="sessionDefaultChannelGroup")],
            metrics=[Metric(name="sessions")],
            order_bys=[OrderBy(
                metric=OrderBy.MetricOrderBy(metric_name="sessions"),
                desc=True,
            )],
        ))
        total = sum(int(r.metric_values[0].value) for r in resp.rows) or 1
        return {
            r.dimension_values[0].value: {
                "sessions": int(r.metric_values[0].value),
                "pct":      round(int(r.metric_values[0].value) / total * 100, 1),
            }
            for r in resp.rows
        }
    except Exception as e:
        print(f"    [WARN] GA4 channel breakdown fetch failed: {e}")
        return {}


# ── Google Ads Conversion Fetcher ─────────────────────────────────────────────

def get_conversion_actions(ads_client, customer_id):
    """Return all non-removed conversion actions with full settings."""
    ga_service = ads_client.get_service("GoogleAdsService")
    query = """
        SELECT
            conversion_action.id,
            conversion_action.name,
            conversion_action.status,
            conversion_action.type,
            conversion_action.counting_type,
            conversion_action.primary_for_goal,
            conversion_action.include_in_conversions_metric,
            conversion_action.attribution_model_settings.attribution_model,
            conversion_action.value_settings.default_value,
            conversion_action.value_settings.always_use_default_value,
            conversion_action.tag_snippets
        FROM conversion_action
        WHERE conversion_action.status != 'REMOVED'
    """
    try:
        rows = ga_service.search(customer_id=customer_id, query=query)
    except GoogleAdsException as ex:
        print(f"  [ERROR] Conversion actions fetch failed: {ex.failure.errors[0].message}")
        return []

    out = []
    for row in rows:
        ca = row.conversion_action
        snippets = []
        for s in ca.tag_snippets:
            snippets.append({
                "type":         s.type_,
                "global_site":  s.global_site_tag,
                "event_snippet":s.event_snippet,
            })
        out.append({
            "id":            ca.id,
            "name":          ca.name,
            "status":        ca.status.name,
            "type":          ca.type_.value if hasattr(ca.type_, 'value') else int(ca.type_),
            "counting":      ca.counting_type.name,
            "primary":       ca.primary_for_goal,
            "in_conversions":ca.include_in_conversions_metric,
            "attribution":   ca.attribution_model_settings.attribution_model.name,
            "default_value": round(ca.value_settings.default_value, 2),
            "snippets":      snippets,
        })
    return out


# ── Analysis Engine ───────────────────────────────────────────────────────────

def _param(tag, key):
    """Extract a named parameter value from a GTM tag's parameter list."""
    for p in tag.get("parameter", []):
        if p.get("key") == key:
            return p.get("value", "")
    return ""


def _event_params(tag):
    """
    Extract event parameters from a gaawe (GA4 Event) tag as {param_name: value_ref}.
    Event parameters are stored as a list of maps under key 'eventParameters'.
    """
    result = {}
    for p in tag.get("parameter", []):
        if p.get("key") == "eventParameters" and p.get("type") == "list":
            for list_item in p.get("list", []):
                if list_item.get("type") == "map":
                    m = {x.get("key", ""): x.get("value", "") for x in list_item.get("map", [])}
                    if "key" in m and "value" in m:
                        result[m["key"]] = m["value"]
    return result


def analyse_container(tags, triggers, variables, conversion_actions):
    """
    Return a structured findings dict covering:
      - tag inventory by type
      - Google Ads tags (conversion + remarketing)
      - GA4 tags
      - Enhanced Conversions status
      - Consent Mode status
      - issues list
    """
    findings = {
        "tag_inventory":       defaultdict(list),
        "ads_tags":            [],
        "ga4_tags":            [],
        "consent_tags":        [],
        "cmp_detected":        False,
        "consent_mode_v2":     False,
        "ec_detected":         False,
        "issues":              [],
        "trigger_map":         {},   # triggerId → trigger name
        "variable_names":      [],
        "orphaned_tags":       [],   # tags with no firing trigger
        # ── New checks ────────────────────────────────────────────────────────
        "purchase_ga4_tags":   [],   # [{name, has_items, items_var}]
        "consent_inside_gtm":  False,
        "channel_breakdown":   {},   # filled in main() via GA4 Data API
    }

    # Build trigger lookup
    for t in triggers:
        findings["trigger_map"][t["triggerId"]] = t.get("name", "Unknown")

    # Build variable name list
    findings["variable_names"] = [v.get("name", "") for v in variables]

    all_trigger_ids = {t["triggerId"] for t in triggers}

    for tag in tags:
        tag_type = tag.get("type", "unknown")
        tag_name = tag.get("name", "Unnamed")
        label    = TAG_TYPE_LABELS.get(tag_type, tag_type)
        fired_by = [findings["trigger_map"].get(tid, tid) for tid in tag.get("firingTriggerId", [])]
        paused   = tag.get("paused", False)
        has_consent = "consentSettings" in tag

        entry = {
            "name":        tag_name,
            "type":        tag_type,
            "label":       label,
            "fired_by":    fired_by,
            "paused":      paused,
            "has_consent": has_consent,
        }

        findings["tag_inventory"][tag_type].append(entry)

        # Check for orphaned tags (no firing trigger, not paused)
        if not tag.get("firingTriggerId") and not paused:
            findings["orphaned_tags"].append(tag_name)

        # ── Google Ads conversion tags ────────────────────────────────────────
        if tag_type == "awct":
            conv_id   = _param(tag, "conversionId")
            conv_label = _param(tag, "conversionLabel")
            ec_enabled = _param(tag, "enhance_conversions") == "true"

            if ec_enabled:
                findings["ec_detected"] = True

            findings["ads_tags"].append({
                **entry,
                "conversion_id":    conv_id,
                "conversion_label": conv_label,
                "ec_enabled":       ec_enabled,
                "remarketing":      False,
            })

        elif tag_type == "googtag":
            tag_id = _param(tag, "tagId") or _param(tag, "id")
            findings["ads_tags"].append({
                **entry,
                "tag_id":    tag_id,
                "remarketing": False,
            })

        elif tag_type in ("fls", "flc", "_rmt"):
            findings["ads_tags"].append({**entry, "remarketing": True})

        # ── GA4 tags ──────────────────────────────────────────────────────────
        elif tag_type in ("gaawe", "gaawc", "ua"):
            event_name = _param(tag, "eventName") or _param(tag, "trackType") or "config"
            findings["ga4_tags"].append({**entry, "event_name": event_name})
            # Track purchase event tags for Cart Data check
            if tag_type == "gaawe" and event_name.lower() == "purchase" and not paused:
                ep = _event_params(tag)
                findings["purchase_ga4_tags"].append({
                    "name":      tag_name,
                    "has_items": "items" in ep,
                    "items_var": ep.get("items", ""),
                })

        # ── Custom HTML — scan for consent / EC signals ───────────────────────
        elif tag_type == "html":
            html = _param(tag, "html")
            if "gtag('consent'" in html or 'gtag("consent"' in html:
                findings["consent_tags"].append({**entry, "signal": "gtag consent command"})
                if "denied" in html or "granted" in html:
                    findings["consent_mode_v2"] = True
            if "OnetrustActiveGroups" in html or "OneTrust" in html:
                findings["cmp_detected"] = True
                findings["consent_mode_v2"] = True
            if "Cookiebot" in html or "CookieConsent" in html:
                findings["cmp_detected"] = True
                findings["consent_mode_v2"] = True
            if "dataLayer.push" in html and ("enhance_conversions" in html or "sha256" in html.lower()):
                findings["ec_detected"] = True

        # ── Consent init trigger type ─────────────────────────────────────────
        for trig in triggers:
            if trig.get("type") == "consentInit":
                findings["consent_mode_v2"] = True

    # ── Issue detection ───────────────────────────────────────────────────────
    issues = findings["issues"]

    if not findings["ads_tags"]:
        issues.append({
            "severity": "CRITICAL",
            "msg": "No Google Ads conversion or Google Tag found in GTM container.",
        })

    non_paused_ads = [t for t in findings["ads_tags"] if not t["paused"] and not t.get("remarketing")]
    if not non_paused_ads:
        issues.append({
            "severity": "CRITICAL",
            "msg": "All Google Ads tags are paused — no conversions are being tracked.",
        })

    if not findings["ec_detected"]:
        issues.append({
            "severity": "HIGH",
            "msg": "Enhanced Conversions not detected. Missing hashed user data (email/phone) "
                   "reduces match rates and bidding signal quality.",
        })

    if not findings["consent_mode_v2"]:
        issues.append({
            "severity": "HIGH",
            "msg": "Consent Mode v2 not detected. Required for EU compliance and modelled "
                   "conversions when users decline cookies. Google mandates this for EEA traffic.",
        })

    if not findings["cmp_detected"]:
        issues.append({
            "severity": "HIGH",
            "msg": "No CMP (Consent Management Platform) detected in GTM. "
                   "Consent Mode v2 requires a CMP (e.g. Cookiebot, OneTrust) to signal user consent.",
        })

    # Tags without consent settings
    tags_missing_consent = [
        t["name"] for t in tags
        if t.get("type") in ("awct", "googtag", "gaawe", "gaawc")
        and "consentSettings" not in t
    ]
    if tags_missing_consent:
        issues.append({
            "severity": "MEDIUM",
            "msg": f"{len(tags_missing_consent)} tracking tag(s) have no consent settings configured in GTM: "
                   + ", ".join(tags_missing_consent[:5])
                   + ("..." if len(tags_missing_consent) > 5 else ""),
        })

    if findings["orphaned_tags"]:
        issues.append({
            "severity": "MEDIUM",
            "msg": f"{len(findings['orphaned_tags'])} tag(s) have no firing trigger (will never fire): "
                   + ", ".join(findings["orphaned_tags"][:5]),
        })

    # GA4 purchase event check
    has_purchase = any(
        t.get("event_name", "").lower() in ("purchase", "ecommerce")
        for t in findings["ga4_tags"]
    )
    if not has_purchase and findings["ga4_tags"]:
        issues.append({
            "severity": "MEDIUM",
            "msg": "No GA4 'purchase' event tag found. E-commerce revenue tracking may be missing.",
        })

    # UA tags still present
    ua_tags = [t["name"] for t in findings["ga4_tags"] if t["type"] == "ua"]
    if ua_tags:
        issues.append({
            "severity": "LOW",
            "msg": f"Universal Analytics tags still present (UA is dead — data not collected): "
                   + ", ".join(ua_tags),
        })

    # Conversion action settings checks
    # Nuance: secondary (in_conversions=False) is intentional when the account has ≥1 enabled
    # primary action. Only flag as MEDIUM if there are NO primary actions at all — meaning
    # Smart Bidding has no optimisation target. Secondary-only setups (e.g. observed purchase
    # alongside a primary lead conversion, or Android app set to secondary) are INFO only.
    enabled_cas = [c for c in conversion_actions if c.get("status") == "ENABLED"]
    primary_cas  = [c for c in enabled_cas if c["in_conversions"]]
    secondary_cas = [c for c in enabled_cas if not c["in_conversions"]]

    for ca in secondary_cas:
        if primary_cas:
            # Intentional secondary — observed only alongside a proper primary signal
            issues.append({
                "severity": "INFO",
                "msg": f"'{ca['name']}' is secondary (observed only, not in Smart Bidding). "
                       f"Intentional if this is a supplementary signal. "
                       f"Primary: {', '.join(p['name'] for p in primary_cas[:2])}.",
            })
        else:
            # No primary conversion action — Smart Bidding has nothing to optimise toward
            issues.append({
                "severity": "MEDIUM",
                "msg": f"'{ca['name']}' is excluded from Smart Bidding and no primary "
                       "conversion actions are enabled. Smart Bidding has no optimisation target.",
            })

    for ca in conversion_actions:
        if ca["attribution"] == "LAST_CLICK" and ca["primary"]:
            issues.append({
                "severity": "LOW",
                "msg": f"'{ca['name']}' uses Last Click attribution. Consider Data-Driven "
                       "if you have sufficient conversion volume (50+/month).",
            })

    # ── Cart Data: GA4 purchase event items parameter ─────────────────────────
    # Build variable DataLayer path index for the id-field check
    var_dl_paths = {}
    for v in variables:
        if v.get("type") == "v":
            for p in v.get("parameter", []):
                if p.get("key") == "name":
                    var_dl_paths[v.get("name", "")] = p.get("value", "")

    for pt in findings["purchase_ga4_tags"]:
        if not pt["has_items"]:
            issues.append({
                "severity": "MEDIUM",
                "msg": (
                    f"GA4 purchase tag '{pt['name']}' has no 'items' event parameter. "
                    "Product-level data is missing from GA4 ecommerce reports and "
                    "Google Ads Cart Data will not work."
                ),
            })
        else:
            # Check if any variable specifically maps an .id path from the items array
            # (e.g. ecommerce.items[0].id) — evidence that id is extracted intentionally
            has_items_id_var = any(
                ".id" in path.lower() and "ecommerce" in path.lower()
                for path in var_dl_paths.values()
            )
            if not has_items_id_var:
                issues.append({
                    "severity": "LOW",
                    "msg": (
                        f"Cart Data: '{pt['name']}' sends items via '{pt['items_var']}'. "
                        "Verify the dataLayer push includes 'id' (Google Shopping feed format) "
                        "alongside 'item_id' in each item object. Without 'id', Google Ads "
                        "Conversions with Cart Data cannot match items to the product feed."
                    ),
                })

    # ── Consent timing: flag if consent default fires inside GTM ─────────────
    # The consent default must be pushed to the dataLayer BEFORE the GTM snippet
    # loads. If it fires inside GTM via a consentInit tag, it is already too late —
    # GTM's first beacon (gtm.js) is sent without consent context.
    consent_init_tids = {t["triggerId"] for t in triggers if t.get("type") == "consentInit"}
    if consent_init_tids:
        for tag in tags:
            if tag.get("type") != "html":
                continue
            html_content = _param(tag, "html")
            firing_ids   = set(tag.get("firingTriggerId", []))
            if (firing_ids & consent_init_tids
                    and ("gtag('consent'" in html_content or 'gtag("consent"' in html_content)
                    and "default" in html_content):
                findings["consent_inside_gtm"] = True
                issues.append({
                    "severity": "MEDIUM",
                    "msg": (
                        f"Consent Mode v2 default fires inside GTM via '{tag.get('name', 'tag')}' "
                        "(consentInit trigger). The container has already loaded before this fires — "
                        "the initial gtm.js hit carries no consent context, causing pre-consent "
                        "sessions to appear as Unassigned in GA4. "
                        "Fix: push the consent default in the CMP script or page source "
                        "before the GTM snippet, not inside a GTM tag."
                    ),
                })
                break

    return findings


# ── AI Recommendations ────────────────────────────────────────────────────────

def generate_recommendations(account_name, findings, conversion_actions, container_info):
    """Ask Claude Sonnet for a prioritised fix plan based on audit findings."""
    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return "*(ANTHROPIC_API_KEY not set — skipping AI recommendations)*"

        issues_text = "\n".join(
            f"  [{i['severity']}] {i['msg']}"
            for i in findings["issues"]
        )

        ads_tags_text = ""
        for t in findings["ads_tags"]:
            ec = " | EC: ✅" if t.get("ec_enabled") else " | EC: ❌"
            consent = " | Consent: ✅" if t["has_consent"] else " | Consent: ❌"
            ads_tags_text += f"  - {t['name']} ({t['label']}) | Paused: {t['paused']}{ec}{consent}\n"

        ga4_text = "\n".join(
            f"  - {t['name']} | Event: {t.get('event_name','—')} | Consent: {t['has_consent']}"
            for t in findings["ga4_tags"]
        )

        conv_text = "\n".join(
            f"  - {ca['name']} | Status: {ca['status']} | Type: {ca['type']} | "
            f"In Conv: {ca['in_conversions']} | Attribution: {ca['attribution']} | "
            f"Value: £{ca['default_value']}"
            for ca in conversion_actions
        )

        # Build channel breakdown context
        channel_text = ""
        if findings.get("channel_breakdown"):
            lines_ch = []
            for ch, data in list(findings["channel_breakdown"].items())[:12]:
                flag = " ⚠️" if ch == "Unassigned" and data["pct"] >= 5 else ""
                lines_ch.append(f"  {ch}{flag}: {data['sessions']:,} sessions ({data['pct']:.1f}%)")
            channel_text = "GA4 CHANNEL BREAKDOWN (last 30 days):\n" + "\n".join(lines_ch) + "\n\n"

        prompt = (
            f"You are a senior Google Ads tracking specialist auditing **{account_name}**.\n\n"
            f"GTM Container: {container_info.get('publicId')} | "
            f"Tags: {container_info.get('tag_count', '?')} | "
            f"Triggers: {container_info.get('trigger_count', '?')} | "
            f"Variables: {container_info.get('variable_count', '?')}\n\n"
            f"ISSUES FOUND:\n{issues_text or '  None detected.'}\n\n"
            f"GOOGLE ADS TAGS IN GTM:\n{ads_tags_text or '  None found.'}\n\n"
            f"GA4 TAGS IN GTM:\n{ga4_text or '  None found.'}\n\n"
            f"CONSENT MODE v2 DETECTED: {findings['consent_mode_v2']}\n"
            f"CONSENT DEFAULT FIRES INSIDE GTM: {findings.get('consent_inside_gtm', False)}\n"
            f"ENHANCED CONVERSIONS DETECTED: {findings['ec_detected']}\n"
            f"CMP DETECTED: {findings['cmp_detected']}\n\n"
            f"{channel_text}"
            f"GOOGLE ADS CONVERSION ACTIONS:\n{conv_text or '  None.'}\n\n"
            "Write a structured implementation plan using these exact sections:\n\n"
            "## Critical Fixes (do these first)\n"
            "Step-by-step instructions to fix CRITICAL and HIGH severity issues. "
            "Be specific about which GTM tags to create/edit and what settings to use.\n\n"
            "## Enhanced Conversions Setup\n"
            "Exact steps to implement Enhanced Conversions in GTM for this account. "
            "Include the dataLayer variable setup needed and which GTM tag to modify.\n\n"
            "## Consent Mode v2 Implementation\n"
            "Exact steps to implement Consent Mode v2. Recommend a specific CMP and "
            "show the gtag consent default code to add. Include the GTM consent "
            "initialization tag setup.\n\n"
            "## Conversion Action Optimisations\n"
            "Specific recommendations on counting type, attribution model, and value settings "
            "for each conversion action listed above.\n\n"
            "## Priority Order\n"
            "Numbered list of the top 6 actions ordered by impact. Be specific."
        )

        ai_client = anthropic.Anthropic(api_key=api_key)
        msg = ai_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=(
                "You are a senior Google Ads tracking specialist. "
                "Write precise, actionable implementation steps — no filler. "
                "Reference specific GTM tag types, parameter names, and trigger configurations. "
                "Use markdown with clear headers."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()

    except Exception as e:
        return f"*(Recommendations generation failed: {e})*"


# ── Obsidian Report Writer ────────────────────────────────────────────────────

def write_audit_report(account_name, findings, conversion_actions, recommendations, container_info):
    folder = os.path.join(OBSIDIAN_BASE, account_name, "Tracking Audits")
    os.makedirs(folder, exist_ok=True)
    date_slug = date.today().strftime("%Y-%m-%d")
    filepath  = os.path.join(folder, f"tracking_audit_{date_slug}.md")

    def _sev_icon(s):
        return {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}.get(s, "⚪")

    has_critical = any(i["severity"] == "CRITICAL" for i in findings["issues"])
    has_high     = any(i["severity"] == "HIGH"     for i in findings["issues"])
    overall      = "🔴 Critical Issues" if has_critical else ("🟠 Action Required" if has_high else "✅ Healthy")

    # ── Derived status values for new checks ─────────────────────────────────
    # Cart Data
    purchase_tags = findings.get("purchase_ga4_tags", [])
    if not purchase_tags:
        cart_data_status = "— no purchase tag"
    elif all(pt["has_items"] for pt in purchase_tags):
        cart_data_status = "✅ items parameter present"
    else:
        cart_data_status = "❌ items parameter missing"

    # Consent timing
    consent_timing_status = (
        "⚠️ Fires inside GTM (too late)" if findings.get("consent_inside_gtm")
        else "✅ OK / outside GTM"
    )

    # Channel breakdown
    channel_breakdown = findings.get("channel_breakdown", {})
    if not channel_breakdown:
        channel_status = "— GA4 Data API not configured"
    else:
        unassigned_pct = channel_breakdown.get("Unassigned", {}).get("pct", 0)
        if unassigned_pct >= 15:
            channel_status = f"🔴 {unassigned_pct:.1f}% Unassigned"
        elif unassigned_pct >= 5:
            channel_status = f"🟠 {unassigned_pct:.1f}% Unassigned"
        else:
            channel_status = f"✅ {unassigned_pct:.1f}% Unassigned"

    lines = [
        "---",
        f"tags: [google-ads, tracking-audit, {account_name.lower().replace(' ', '-')}]",
        f"date: {date.today().isoformat()}",
        f"account: {account_name}",
        "---",
        "",
        f"# {account_name} — Conversion Tracking Audit",
        f"**Date:** {date.today().strftime('%d %b %Y')}  ",
        f"**Overall Status:** {overall}",
        "",
        "---",
        "",
        "## Health Summary",
        "",
        "| Check | Status |",
        "| --- | --- |",
        f"| Google Ads tags in GTM | {'✅ Found' if findings['ads_tags'] else '❌ Missing'} |",
        f"| Active conversion tag | {'✅ Yes' if any(not t['paused'] for t in findings['ads_tags']) else '❌ All paused'} |",
        f"| Enhanced Conversions | {'✅ Detected' if findings['ec_detected'] else '❌ Not configured'} |",
        f"| Consent Mode v2 | {'✅ Detected' if findings['consent_mode_v2'] else '❌ Not configured'} |",
        f"| Consent timing | {consent_timing_status} |",
        f"| CMP present | {'✅ Detected' if findings['cmp_detected'] else '❌ Not found'} |",
        "| GA4 purchase event | " + ("✅ Found" if any(t.get("event_name","").lower() == "purchase" for t in findings["ga4_tags"]) else "⚠️ Not found") + " |",
        f"| Cart Data (items parameter) | {cart_data_status} |",
        f"| Orphaned tags | {'⚠️ ' + str(len(findings['orphaned_tags'])) + ' tag(s)' if findings['orphaned_tags'] else '✅ None'} |",
        "| GA4/spend continuity (14d) | " + (
            "🔴 " + str(sum(1 for i in findings["issues"] if i["severity"] == "CRITICAL" and "blackout" in i["msg"])) + " blackout day(s)"
            if any("blackout" in i["msg"] for i in findings["issues"])
            else "✅ No blackout days"
        ) + " |",
        f"| GA4 % Unassigned (30d) | {channel_status} |",
        "",
        "---",
        "",
        "## Issues Found",
        "",
    ]

    if findings["issues"]:
        for issue in findings["issues"]:
            lines.append(f"{_sev_icon(issue['severity'])} **{issue['severity']}** — {issue['msg']}")
            lines.append("")
    else:
        lines += ["*No issues detected.*", ""]

    # ── GA4 Channel Breakdown table
    if findings.get("channel_breakdown"):
        lines += ["---", "", "## GA4 Channel Breakdown (Last 30 Days)", ""]
        lines += [
            "| Channel | Sessions | % |",
            "| --- | --- | --- |",
        ]
        for channel, data in findings["channel_breakdown"].items():
            flag = " ⚠️" if channel == "Unassigned" and data["pct"] >= 5 else ""
            lines.append(f"| {channel}{flag} | {data['sessions']:,} | {data['pct']:.1f}% |")
        lines += [""]

    lines += ["---", "", "## GTM Container", ""]
    lines.append(
        f"**Container:** `{container_info.get('publicId')}` — {container_info.get('name', '?')}  \n"
        f"**Tags:** {container_info.get('tag_count', '?')}  &nbsp; "
        f"**Triggers:** {container_info.get('trigger_count', '?')}  &nbsp; "
        f"**Variables:** {container_info.get('variable_count', '?')}"
    )
    lines += [""]

    # ── Google Ads Tags
    lines += ["### Google Ads Tags", ""]
    if findings["ads_tags"]:
        lines += [
            "| Tag Name | Type | Firing Trigger | EC | Consent | Status |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for t in findings["ads_tags"]:
            triggers_str = ", ".join(t["fired_by"]) if t["fired_by"] else "⚠️ None"
            ec      = "✅" if t.get("ec_enabled") else "❌"
            consent = "✅" if t["has_consent"] else "❌"
            status  = "⏸ Paused" if t["paused"] else "▶ Active"
            extra   = ""
            if t.get("conversion_id"):
                extra = f"`{t['conversion_id']}`"
            elif t.get("tag_id"):
                extra = f"`{t['tag_id']}`"
            lines.append(f"| {t['name']} | {t['label']} | {triggers_str} | {ec} | {consent} | {status} |")
    else:
        lines += ["*No Google Ads tags found.*"]
    lines += [""]

    # ── GA4 Tags
    lines += ["### GA4 Tags", ""]
    if findings["ga4_tags"]:
        lines += [
            "| Tag Name | Event | Consent | Status |",
            "| --- | --- | --- | --- |",
        ]
        for t in findings["ga4_tags"]:
            consent = "✅" if t["has_consent"] else "❌"
            status  = "⏸ Paused" if t["paused"] else "▶ Active"
            lines.append(f"| {t['name']} | `{t.get('event_name','—')}` | {consent} | {status} |")
    else:
        lines += ["*No GA4 tags found.*"]
    lines += [""]

    # ── Consent tags
    if findings["consent_tags"]:
        lines += ["### Consent Signals Found", ""]
        for t in findings["consent_tags"]:
            lines.append(f"- **{t['name']}** — {t.get('signal', '')}")
        lines += [""]

    lines += ["---", "", "## Google Ads Conversion Actions", ""]
    if conversion_actions:
        lines += [
            "| Name | Status | Type | Counting | Attribution | In Conv. | Value |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for ca in conversion_actions:
            lines.append(
                f"| {ca['name']} | {ca['status']} | {ca['type']} "
                f"| {ca['counting']} | {ca['attribution']} "
                f"| {'✅' if ca['in_conversions'] else '❌'} "
                f"| £{ca['default_value']:.2f} |"
            )
    else:
        lines += ["*No conversion actions found.*"]

    lines += ["", "---", "", "## Implementation Plan & Recommendations", "", recommendations, ""]

    with open(filepath, "w") as f:
        f.write("\n".join(lines) + "\n")

    return filepath


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ads_client = GoogleAdsClient.load_from_storage(YAML_FILE)

    print("\nConversion Tracking Audit")
    print("=" * 70)

    gtm_service = build_gtm_service()

    for account_name, cfg in AUDIT_ACCOUNTS.items():
        print(f"\n  [{account_name}]")

        # ── GTM ───────────────────────────────────────────────────────────────
        public_id = cfg["gtm_container_public_id"]
        print(f"    Finding GTM container {public_id}...")
        acct_path, container = find_container(gtm_service, public_id)

        if not container:
            print(f"    [ERROR] Container {public_id} not found. Check GTM account access.")
            continue

        container_path = container["path"]
        print(f"    Container found: {container.get('name')} ({container_path})")

        workspace = get_live_workspace(gtm_service, container_path)
        if not workspace:
            print("    [ERROR] No workspace found.")
            continue

        ws_path = workspace["path"]
        print(f"    Workspace: {workspace.get('name')} ({ws_path})")

        print("    Pulling tags, triggers, variables...")
        tags      = get_tags(gtm_service, ws_path)
        triggers  = get_triggers(gtm_service, ws_path)
        variables = get_variables(gtm_service, ws_path)
        print(f"    Tags: {len(tags)} | Triggers: {len(triggers)} | Variables: {len(variables)}")

        container_info = {
            "publicId":       container.get("publicId"),
            "name":           container.get("name"),
            "tag_count":      len(tags),
            "trigger_count":  len(triggers),
            "variable_count": len(variables),
        }

        # ── Google Ads ────────────────────────────────────────────────────────
        print("    Pulling Google Ads conversion actions...")
        conversion_actions = get_conversion_actions(ads_client, cfg["ads_customer_id"])
        print(f"    Conversion actions: {len(conversion_actions)}")

        # ── GA4 / spend continuity check ──────────────────────────────────────
        ga4_prop = cfg.get("ga4_property_id")
        if ga4_prop:
            print("    Checking GA4/spend continuity (14-day)...")
            continuity_issues = check_tracking_continuity(ga4_prop, ads_client, cfg["ads_customer_id"])
            if continuity_issues:
                print(f"    ⚠️  {len(continuity_issues)} tracking continuity issue(s) found.")
            else:
                print("    GA4/spend continuity: OK — no blackout days in last 14 days.")
        else:
            continuity_issues = []

        # ── Analyse ───────────────────────────────────────────────────────────
        print("    Analysing...")
        findings = analyse_container(tags, triggers, variables, conversion_actions)
        findings["issues"].extend(continuity_issues)  # merge continuity issues in

        # ── GA4 channel breakdown (30-day % Unassigned check) ─────────────────
        if ga4_prop:
            print("    Checking GA4 channel attribution (30-day)...")
            channel_breakdown = fetch_ga4_channel_breakdown(ga4_prop)
            findings["channel_breakdown"] = channel_breakdown
            if channel_breakdown:
                unassigned_pct = channel_breakdown.get("Unassigned", {}).get("pct", 0)
                total_sessions = sum(v["sessions"] for v in channel_breakdown.values())
                print(f"    Channel breakdown: {total_sessions:,} sessions, "
                      f"{unassigned_pct:.1f}% Unassigned")
                if unassigned_pct >= 15:
                    consent_note = (
                        " Likely linked to Consent Mode default firing inside GTM."
                        if findings.get("consent_inside_gtm") else ""
                    )
                    findings["issues"].append({
                        "severity": "HIGH",
                        "msg": (
                            f"GA4 channel attribution: {unassigned_pct:.1f}% Unassigned "
                            f"in last 30 days (threshold: 15%). Sessions are reaching GA4 "
                            f"without source attribution — campaign optimisation is partially "
                            f"blind.{consent_note}"
                        ),
                    })
                elif unassigned_pct >= 5:
                    findings["issues"].append({
                        "severity": "MEDIUM",
                        "msg": (
                            f"GA4 channel attribution: {unassigned_pct:.1f}% Unassigned "
                            "in last 30 days. Check consent timing, UTM parameter coverage, "
                            "and referral exclusions."
                        ),
                    })
            else:
                print("    Channel breakdown: skipped (GA4 Data API not yet configured for this property)")
        print(f"    Issues found: {len(findings['issues'])}")
        for issue in findings["issues"]:
            icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}.get(issue["severity"], "")
            print(f"      {icon} [{issue['severity']}] {issue['msg'][:80]}{'...' if len(issue['msg']) > 80 else ''}")

        # ── AI recommendations ────────────────────────────────────────────────
        print("    Generating recommendations with Claude Sonnet...")
        recommendations = generate_recommendations(
            account_name, findings, conversion_actions, container_info
        )

        # ── Write report ──────────────────────────────────────────────────────
        print("    Writing audit report...")
        report_path = write_audit_report(
            account_name, findings, conversion_actions, recommendations, container_info
        )
        print(f"    → {report_path}")

        # ── Post to Monday.com ────────────────────────────────────────────────
        if findings["issues"]:
            print("    Posting action items to Monday.com...")
            post_audit_issues(account_name, findings["issues"], report_path=str(report_path))

    print("\n" + "=" * 70)
    print("Audit complete.")


if __name__ == "__main__":
    main()
