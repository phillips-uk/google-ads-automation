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
from datetime import date
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


# ── GTM Authentication ────────────────────────────────────────────────────────

def _gtm_credentials():
    """Load or refresh GTM OAuth credentials. Opens browser on first run."""
    # Load saved token
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            td = json.load(f)
        creds = Credentials(
            token=td.get("token"),
            refresh_token=td.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=td.get("client_id"),
            client_secret=td.get("client_secret"),
            scopes=GTM_SCOPES,
        )
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds)
            return creds

    # First run — browser OAuth using Desktop app client
    with open(GTM_CLIENT_FILE) as f:
        gc = json.load(f)

    # gtm_client.json may be {"installed": {...}} or flat {"client_id": ...}
    installed = gc.get("installed", gc)

    client_config = {
        "installed": {
            "client_id":     installed["client_id"],
            "client_secret": installed["client_secret"],
            "redirect_uris": installed.get("redirect_uris", ["http://localhost"]),
            "auth_uri":      installed.get("auth_uri", "https://accounts.google.com/o/oauth2/auth"),
            "token_uri":     installed.get("token_uri", "https://oauth2.googleapis.com/token"),
        }
    }

    print("\n" + "=" * 70)
    print("  GTM API authorisation required (one-time setup).")
    print("  A browser window will open — log in and click Allow.")
    print("  The script continues automatically once approved.")
    print("=" * 70 + "\n")

    flow  = InstalledAppFlow.from_client_config(client_config, GTM_SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_token(creds)
    print("  GTM auth approved. Token saved — will not prompt again.\n")
    return creds


def _save_token(creds):
    with open(TOKEN_FILE, "w") as f:
        json.dump({
            "token":         creds.token,
            "refresh_token": creds.refresh_token,
            "client_id":     creds.client_id,
            "client_secret": creds.client_secret,
        }, f)


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
        "tag_inventory":     defaultdict(list),
        "ads_tags":          [],
        "ga4_tags":          [],
        "consent_tags":      [],
        "cmp_detected":      False,
        "consent_mode_v2":   False,
        "ec_detected":       False,
        "issues":            [],
        "trigger_map":       {},   # triggerId → trigger name
        "variable_names":    [],
        "orphaned_tags":     [],   # tags with no firing trigger
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
    for ca in conversion_actions:
        if not ca["in_conversions"]:
            issues.append({
                "severity": "MEDIUM",
                "msg": f"Conversion action '{ca['name']}' is excluded from 'Conversions' — "
                       "won't influence Smart Bidding.",
            })
        if ca["attribution"] == "LAST_CLICK" and ca["primary"]:
            issues.append({
                "severity": "LOW",
                "msg": f"'{ca['name']}' uses Last Click attribution. Consider Data-Driven "
                       "if you have sufficient conversion volume (50+/month).",
            })

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
            f"ENHANCED CONVERSIONS DETECTED: {findings['ec_detected']}\n"
            f"CMP DETECTED: {findings['cmp_detected']}\n\n"
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
        f"| CMP present | {'✅ Detected' if findings['cmp_detected'] else '❌ Not found'} |",
        "| GA4 purchase event | " + ("✅ Found" if any(t.get("event_name","").lower() == "purchase" for t in findings["ga4_tags"]) else "⚠️ Not found") + " |",
        f"| Orphaned tags | {'⚠️ ' + str(len(findings['orphaned_tags'])) + ' tag(s)' if findings['orphaned_tags'] else '✅ None'} |",
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

        # ── Analyse ───────────────────────────────────────────────────────────
        print("    Analysing...")
        findings = analyse_container(tags, triggers, variables, conversion_actions)
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
            post_audit_issues(account_name, findings["issues"])

    print("\n" + "=" * 70)
    print("Audit complete.")


if __name__ == "__main__":
    main()
