"""
GA4 Property Setup & Audit

Audits and applies best-practice configuration for GA4 properties:
  - Property settings (timezone, currency, industry category)
  - Data retention (14 months)
  - Attribution (data-driven, correct lookback windows)
  - Conversion events (purchase + lead gen per site type)
  - Google Ads link (audience personalisation enabled)
  - Custom dimensions (transaction_id, session_id)
  - Audiences (purchasers, abandoned cart, lead submitters, etc.)
  - Enhanced measurement settings
  - GTM tag checklist (manual — printed to report)

Modes:
  python3 ga4_audit.py                          # audit all, write report
  python3 ga4_audit.py --apply                  # apply all API fixes
  python3 ga4_audit.py --client "Client A"      # single client (name from clients.py)
  python3 ga4_audit.py --audit-only             # no report written
"""

import argparse
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(__file__))
from config import OBSIDIAN_BASE, load_env

load_env()

from ga4_auth import get_admin_client
from google.analytics.admin_v1alpha import types as ga4_types
from google.api_core.exceptions import GoogleAPIError, AlreadyExists

# ── Client registry — loaded from clients.py (gitignored, never committed) ─────
# Add new clients by editing clients.py (copy clients.example.py if starting fresh).
# Keys per client: property_id, ads_customer_id, timezone, currency, industry,
# site_types, lead_conversion_event, internal_ips, unwanted_referrals,
# search_console_domain, merchant_center_id
try:
    from clients import GA4_CLIENTS
except ImportError:
    print("[config] ERROR: clients.py not found.")
    print("         Copy clients.example.py → clients.py and fill in your GA4 client details.")
    sys.exit(1)

REPORT_DIR  = OBSIDIAN_BASE
TODAY       = date.today().isoformat()

# ── Helpers ────────────────────────────────────────────────────────────────────

def _icon(ok):
    return "✅" if ok else "❌"


def _prop_label(prop_name):
    return prop_name.replace("properties/", "")


def _existing_names(items):
    return {getattr(i, "display_name", None) for i in items}


# ── Audit ──────────────────────────────────────────────────────────────────────

def audit_property(client, client_name, cfg):
    prop = cfg["property_id"]
    findings = {
        "client":       client_name,
        "property_id":  prop,
        "issues":       [],
        "ok":           [],
        "manual":       [],
    }

    # Property object
    p = client.get_property(name=prop)
    _check(findings, p.time_zone == cfg["timezone"],
           f"Timezone is {cfg['timezone']}",
           f"Timezone is '{p.time_zone}' — should be '{cfg['timezone']}'",
           fix_type="property", fix_field="time_zone", fix_value=cfg["timezone"])

    _check(findings, p.currency_code == cfg["currency"],
           f"Currency is {cfg['currency']}",
           f"Currency is '{p.currency_code}' — should be '{cfg['currency']}'",
           fix_type="property", fix_field="currency_code", fix_value=cfg["currency"])

    _check(findings, p.industry_category.name == cfg["industry"],
           f"Industry category: {cfg['industry']}",
           f"Industry category: '{p.industry_category.name}' — should be '{cfg['industry']}'",
           fix_type="property", fix_field="industry_category",
           fix_value=getattr(ga4_types.IndustryCategory, cfg["industry"]))

    # Retention
    dr = client.get_data_retention_settings(name=f"{prop}/dataRetentionSettings")
    _check(findings,
           dr.event_data_retention.name == "FOURTEEN_MONTHS",
           "Data retention: 14 months",
           f"Data retention: {dr.event_data_retention.name} — should be FOURTEEN_MONTHS",
           fix_type="retention")

    # Attribution
    try:
        attr = client.get_attribution_settings(name=f"{prop}/attributionSettings")
        _check(findings,
               "DATA_DRIVEN" in attr.reporting_attribution_model.name,
               f"Attribution model: data-driven",
               f"Attribution model: {attr.reporting_attribution_model.name} — should be data-driven")
        _check(findings,
               attr.acquisition_conversion_event_lookback_window.name
               == "ACQUISITION_CONVERSION_EVENT_LOOKBACK_WINDOW_30_DAYS",
               "Acquisition lookback: 30 days",
               f"Acquisition lookback: {attr.acquisition_conversion_event_lookback_window.name}")
    except GoogleAPIError as e:
        findings["issues"].append({"msg": f"Attribution settings unavailable: {e}", "fix_type": None})

    # Enhanced measurement
    for ds in client.list_data_streams(parent=prop):
        if ds.type_.name == "WEB_DATA_STREAM":
            try:
                em = client.get_enhanced_measurement_settings(
                    name=f"{ds.name}/enhancedMeasurementSettings"
                )
                problems = []
                if not em.scrolls_enabled:        problems.append("scrolls")
                if not em.outbound_clicks_enabled: problems.append("outbound clicks")
                if not em.form_interactions_enabled: problems.append("form interactions")
                if not em.video_engagement_enabled: problems.append("video engagement")
                if not em.file_downloads_enabled:  problems.append("file downloads")
                _check(findings, not problems,
                       "Enhanced measurement: all events enabled",
                       f"Enhanced measurement: {', '.join(problems)} disabled",
                       fix_type="enhanced_measurement",
                       fix_field="stream_name", fix_value=ds.name,
                       fix_em=em)
            except GoogleAPIError:
                pass

    # Conversion events
    existing_conversions = {ce.event_name for ce in client.list_conversion_events(parent=prop)}
    if "ecommerce" in cfg["site_types"]:
        _check(findings, "purchase" in existing_conversions,
               "Conversion event: purchase",
               "Conversion event 'purchase' not marked",
               fix_type="conversion", fix_value="purchase")

    if "leadgen" in cfg["site_types"]:
        lead_ev = cfg["lead_conversion_event"]
        _check(findings, lead_ev in existing_conversions,
               f"Conversion event: {lead_ev}",
               f"Conversion event '{lead_ev}' not marked",
               fix_type="conversion", fix_value=lead_ev)

    # Google Ads link
    ads_links = list(client.list_google_ads_links(parent=prop))
    linked_cids = {link.customer_id: link for link in ads_links}
    cid = cfg["ads_customer_id"].replace("-", "")
    if cid in linked_cids:
        link = linked_cids[cid]
        _check(findings, link.ads_personalization_enabled,
               "Google Ads link: audience personalisation enabled",
               "Google Ads link: audience personalisation disabled (blocks remarketing audiences)",
               fix_type="ads_link", fix_value=link.name)
        _check(findings, True, f"Google Ads account {cid} linked", "")
    else:
        findings["issues"].append({
            "msg": f"Google Ads account {cid} not linked",
            "fix_type": "manual",
        })
        findings["manual"].append(
            f"Link Google Ads account {cid}: GA4 → Admin → Google Ads Links → Add"
        )

    # Search Console link
    if cfg.get("search_console_domain"):
        try:
            sc_links = list(client.list_search_console_links(parent=prop))
            sc_linked = any(
                cfg["search_console_domain"] in (getattr(lnk, "site_url", "") or "")
                for lnk in sc_links
            )
            _check(findings, sc_linked,
                   f"Search Console linked: {cfg['search_console_domain']}",
                   f"Search Console not linked ({cfg['search_console_domain']}) — organic search data unavailable in GA4",
                   fix_type="manual")
            if not sc_linked:
                findings["manual"].append(
                    f"Link Search Console ({cfg['search_console_domain']}): GA4 → Admin → Product Links → Search Console links → Link"
                )
        except Exception as e:
            findings["manual"].append(
                f"Search Console link check failed — verify manually (API error: {e})"
            )

    # Merchant Center link
    if cfg.get("merchant_center_id"):
        try:
            mc_links = list(client.list_merchant_center_links(parent=prop))
            mc_linked = any(
                str(cfg["merchant_center_id"]) in str(getattr(lnk, "merchant_center_account_id", ""))
                for lnk in mc_links
            )
            _check(findings, mc_linked,
                   f"Merchant Center linked: {cfg['merchant_center_id']}",
                   f"Merchant Center not linked ({cfg['merchant_center_id']}) — shopping data unavailable in GA4",
                   fix_type="manual")
            if not mc_linked:
                findings["manual"].append(
                    f"Link Merchant Center ({cfg['merchant_center_id']}): GA4 → Admin → Product Links → Merchant Center links → Link"
                )
        except Exception as e:
            findings["manual"].append(
                f"Merchant Center link check failed — verify manually (API error: {e})"
            )

    # Custom dimensions
    existing_dims = {d.parameter_name for d in client.list_custom_dimensions(parent=prop)}
    needed_dims = [
        ("transaction_id", "Transaction ID",
         "Ecommerce deduplication — match GA4 purchase to Google Ads conversion"),
        ("page_type",      "Page Type",
         "Content grouping: homepage / product / cart / checkout / blog — enables segment-level analysis"),
    ]
    for param, display, desc in needed_dims:
        _check(findings, param in existing_dims,
               f"Custom dimension: {param}",
               f"Custom dimension '{param}' missing ({desc})",
               fix_type="custom_dimension",
               fix_value={"param": param, "display": display, "desc": desc})

    # Audiences
    existing_audiences = _existing_names(client.list_audiences(parent=prop))
    wanted_audiences = _desired_audiences(cfg)
    for aud_def in wanted_audiences:
        _check(findings, aud_def["name"] in existing_audiences,
               f"Audience: {aud_def['name']}",
               f"Audience '{aud_def['name']}' missing",
               fix_type="audience", fix_value=aud_def)

    # Manual-only items
    findings["manual"].extend([
        "Internal traffic filter: GA4 → Admin → Data Filters → Create filter → Developer traffic (add office/dev IPs)",
        f"Unwanted referrals: GA4 → Admin → Data Streams → web stream → Configure tag → Unwanted referrals → add: "
        + ", ".join(cfg["unwanted_referrals"]),
        "Verify Google Ads auto-tagging is ON: Google Ads → Settings → Account → Auto-tagging",
        "Import GA4 conversions into Google Ads (SECONDARY/OBSERVED only): Google Ads → Tools → Conversions → Import → Google Analytics 4 → "
        "after import, open each conversion action → set 'Include in Conversions' = OFF (observed in All Conversions only). "
        "Primary conversion signal must come from GTM-direct Google Ads Conversion Tracking tags, not GA4 imports.",
    ])

    return findings


def _check(findings, condition, ok_msg, issue_msg, **fix_kwargs):
    if condition:
        if ok_msg:
            findings["ok"].append(ok_msg)
    else:
        entry = {"msg": issue_msg}
        entry.update(fix_kwargs)
        findings["issues"].append(entry)


# ── Desired audiences ──────────────────────────────────────────────────────────

def _desired_audiences(cfg):
    audiences = []
    if "ecommerce" in cfg["site_types"]:
        audiences += [
            {"name": "Abandoned Cart",    "event": "add_to_cart",    "days": 7,  "desc": "Added to cart, did not purchase — high-intent remarketing"},
            {"name": "Checkout Started",  "event": "begin_checkout", "days": 14, "desc": "Started checkout, did not complete — bottom-funnel"},
            {"name": "Product Viewers",   "event": "view_item",      "days": 14, "desc": "Viewed a product, did not purchase"},
        ]
    if "leadgen" in cfg["site_types"]:
        audiences += [
            {"name": "Lead Submitted",   "event": cfg["lead_conversion_event"], "days": 30, "desc": "Submitted lead/enquiry form"},
        ]
    if "leadgen" in cfg["site_types"] and cfg["lead_conversion_event"] == "sign_up":
        audiences += [
            {"name": "Newsletter Subscribers", "event": "sign_up", "days": 90,  "desc": "Signed up to mailing list — brand awareness audience"},
        ]
    return audiences


def _build_event_audience(name, description, event_name, duration_days):
    return ga4_types.Audience(
        display_name=name,
        description=description,
        membership_duration_days=duration_days,
        filter_clauses=[
            ga4_types.AudienceFilterClause(
                clause_type=ga4_types.AudienceFilterClause.AudienceClauseType.INCLUDE,
                simple_filter=ga4_types.AudienceSimpleFilter(
                    scope=ga4_types.AudienceFilterScope.AUDIENCE_FILTER_SCOPE_ACROSS_ALL_SESSIONS,
                    filter_expression=ga4_types.AudienceFilterExpression(
                        and_group=ga4_types.AudienceFilterExpressionList(
                            filter_expressions=[
                                ga4_types.AudienceFilterExpression(
                                    or_group=ga4_types.AudienceFilterExpressionList(
                                        filter_expressions=[
                                            ga4_types.AudienceFilterExpression(
                                                event_filter=ga4_types.AudienceEventFilter(
                                                    event_name=event_name
                                                )
                                            )
                                        ]
                                    )
                                )
                            ]
                        )
                    )
                )
            )
        ]
    )


# ── Apply fixes ────────────────────────────────────────────────────────────────

def apply_fixes(client, findings, cfg, dry_run=False):
    prop = cfg["property_id"]
    applied = []
    errors  = []
    prop_updates = {}
    prop_mask    = []

    for issue in findings["issues"]:
        ft = issue.get("fix_type")
        if not ft or ft == "manual":
            continue

        label = f"DRY RUN — " if dry_run else ""

        if ft == "property":
            prop_updates[issue["fix_field"]] = issue["fix_value"]
            prop_mask.append(issue["fix_field"])

        elif ft == "retention":
            if not dry_run:
                try:
                    client.update_data_retention_settings(
                        data_retention_settings=ga4_types.DataRetentionSettings(
                            name=f"{prop}/dataRetentionSettings",
                            event_data_retention=ga4_types.DataRetentionSettings.RetentionDuration.FOURTEEN_MONTHS,
                            reset_user_data_on_new_activity=True,
                        ),
                        update_mask={"paths": ["event_data_retention"]},
                    )
                    applied.append("Data retention → 14 months")
                except GoogleAPIError as e:
                    errors.append(f"Retention: {e}")
            else:
                applied.append(f"{label}Data retention → 14 months")

        elif ft == "enhanced_measurement":
            if not dry_run:
                try:
                    em = issue["fix_em"]
                    em.scrolls_enabled          = True
                    em.outbound_clicks_enabled  = True
                    em.form_interactions_enabled = True
                    em.video_engagement_enabled = True
                    em.file_downloads_enabled   = True
                    client.update_enhanced_measurement_settings(
                        enhanced_measurement_settings=em,
                        update_mask={"paths": [
                            "scrolls_enabled", "outbound_clicks_enabled",
                            "form_interactions_enabled", "video_engagement_enabled",
                            "file_downloads_enabled",
                        ]},
                    )
                    applied.append("Enhanced measurement → all events enabled")
                except GoogleAPIError as e:
                    errors.append(f"Enhanced measurement: {e}")
            else:
                applied.append(f"{label}Enhanced measurement → all events enabled")

        elif ft == "conversion":
            event_name = issue["fix_value"]
            if not dry_run:
                try:
                    client.create_conversion_event(
                        parent=prop,
                        conversion_event=ga4_types.ConversionEvent(event_name=event_name),
                    )
                    applied.append(f"Conversion event marked: {event_name}")
                except AlreadyExists:
                    applied.append(f"Conversion event already exists: {event_name}")
                except GoogleAPIError as e:
                    errors.append(f"Conversion ({event_name}): {e}")
            else:
                applied.append(f"{label}Mark conversion: {event_name}")

        elif ft == "ads_link":
            link_name = issue["fix_value"]
            if not dry_run:
                try:
                    client.update_google_ads_link(
                        google_ads_link=ga4_types.GoogleAdsLink(
                            name=link_name,
                            ads_personalization_enabled=True,
                        ),
                        update_mask={"paths": ["ads_personalization_enabled"]},
                    )
                    applied.append("Google Ads link: audience personalisation enabled")
                except GoogleAPIError as e:
                    errors.append(f"Ads link: {e}")
            else:
                applied.append(f"{label}Google Ads link personalisation → enabled")

        elif ft == "custom_dimension":
            d = issue["fix_value"]
            if not dry_run:
                try:
                    client.create_custom_dimension(
                        parent=prop,
                        custom_dimension=ga4_types.CustomDimension(
                            parameter_name=d["param"],
                            display_name=d["display"],
                            description=d["desc"],
                            scope=ga4_types.CustomDimension.DimensionScope.EVENT,
                        ),
                    )
                    applied.append(f"Custom dimension created: {d['param']}")
                except AlreadyExists:
                    applied.append(f"Custom dimension already exists: {d['param']}")
                except GoogleAPIError as e:
                    errors.append(f"Custom dimension ({d['param']}): {e}")
            else:
                applied.append(f"{label}Create custom dimension: {d['param']}")

        elif ft == "audience":
            aud = issue["fix_value"]
            if not dry_run:
                try:
                    client.create_audience(
                        parent=prop,
                        audience=_build_event_audience(
                            aud["name"], aud["desc"], aud["event"], aud["days"]
                        ),
                    )
                    applied.append(f"Audience created: {aud['name']}")
                except AlreadyExists:
                    applied.append(f"Audience already exists: {aud['name']}")
                except GoogleAPIError as e:
                    errors.append(f"Audience ({aud['name']}): {e}")
            else:
                applied.append(f"{label}Create audience: {aud['name']} ({aud['event']}, {aud['days']}d)")

    # Apply batched property updates last
    if prop_updates:
        if not dry_run:
            try:
                p_obj = ga4_types.Property(name=prop, **prop_updates)
                client.update_property(
                    property=p_obj,
                    update_mask={"paths": prop_mask},
                )
                for field in prop_mask:
                    applied.append(f"Property {field} → {prop_updates[field]}")
            except GoogleAPIError as e:
                errors.append(f"Property update: {e}")
        else:
            for field in prop_mask:
                applied.append(f"DRY RUN — Property {field} → {prop_updates[field]}")

    return applied, errors


# ── GTM checklist ──────────────────────────────────────────────────────────────

def gtm_checklist(cfg):
    lines = []
    if "ecommerce" in cfg["site_types"]:
        lines += [
            "GA4 Configuration tag firing on All Pages (Measurement ID hardcoded or via Constant variable)",
            "GA4 Event — `view_item`: fires on product page, passes items[] array with item_id, item_name, price",
            "GA4 Event — `add_to_cart`: fires on add-to-cart click/success, passes items[] + value + currency",
            "GA4 Event — `begin_checkout`: fires on checkout start, passes items[] + value + currency",
            "GA4 Event — `purchase`: fires on order confirmation page, passes transaction_id, value, tax, shipping, currency, items[]",
            "  → Shopify: purchase event may require Shopify Customer Events pixel or theme.liquid dataLayer push",
            "  → Ensure transaction_id is deduped against GA4 custom dimension (prevents double-counting with GA4 auto-detected purchases)",
        ]
    if "leadgen" in cfg["site_types"]:
        lead_ev = cfg["lead_conversion_event"]
        if lead_ev == "sign_up":
            lines += [
                f"GA4 Event — `{lead_ev}`: fires when newsletter/mailing list form is submitted successfully",
                "  → Trigger: Form Submission on the newsletter signup form (or Thank You page URL for double opt-in)",
                "  → Pass: method='newsletter', optional: email (as user property if consent given)",
            ]
        else:
            lines += [
                f"GA4 Event — `{lead_ev}`: fires on contact/enquiry form thank-you page",
                "  → Trigger: Page URL contains '/thank-you' or '/contact-success'",
                "  → Pass: form_type (contact / quote / booking) as event parameter",
            ]
    return lines


# ── Report writer ──────────────────────────────────────────────────────────────

def write_report(all_findings, audit_only=False):
    lines = [
        f"---",
        f"date: {TODAY}",
        f"tags: [google-analytics, ga4, audit]",
        f"---",
        f"",
        f"# GA4 Audit — {TODAY}",
        f"",
    ]

    for findings in all_findings:
        name    = findings["client"]
        prop_id = findings["property_id"]
        issues  = findings["issues"]
        ok      = findings["ok"]
        manual  = findings["manual"]
        applied = findings.get("applied", [])
        errors  = findings.get("errors", [])

        api_fixable = [i for i in issues if i.get("fix_type") and i["fix_type"] != "manual"]
        score = len(ok) / max(1, len(ok) + len(issues))

        lines += [
            f"---",
            f"",
            f"## {name}  `{prop_id}`",
            f"",
            f"**Score:** {len(ok)}/{len(ok)+len(issues)} checks passing  {'🟢' if score >= 0.8 else '🟡' if score >= 0.5 else '🔴'}",
            f"",
        ]

        if ok:
            lines += ["### ✅ Already correct", ""]
            for msg in ok:
                lines.append(f"- {msg}")
            lines.append("")

        if issues:
            lines += ["### ❌ Issues found", ""]
            for issue in issues:
                ft = issue.get("fix_type")
                tag = " *(API fix available)*" if ft and ft != "manual" else " *(manual)*"
                lines.append(f"- {issue['msg']}{tag}")
            lines.append("")

        if applied:
            lines += ["### 🔧 Applied via API", ""]
            for a in applied:
                lines.append(f"- {a}")
            lines.append("")

        if errors:
            lines += ["### ⚠️ Errors during apply", ""]
            for e in errors:
                lines.append(f"- {e}")
            lines.append("")

        if api_fixable and not applied:
            lines += [
                f"> **{len(api_fixable)} issue(s) can be fixed via API.** Run with `--apply` to apply them.",
                "",
            ]

        if manual:
            lines += ["### 📋 Manual steps required", ""]
            for m in manual:
                lines.append(f"- [ ] {m}")
            lines.append("")

        cfg = next(v for k, v in GA4_CLIENTS.items() if k == findings["client"])
        lines += ["### 🏷 GTM Tags checklist", ""]
        for item in gtm_checklist(cfg):
            if item.startswith("  →"):
                lines.append(f"  {item}")
            else:
                lines.append(f"- [ ] {item}")
        lines.append("")

    if audit_only:
        print("\n".join(lines))
        return

    for findings in all_findings:
        name     = findings["client"]
        folder   = os.path.join(REPORT_DIR, name.replace(" ", " "), "GA4 Audits")
        os.makedirs(folder, exist_ok=True)
        path     = os.path.join(folder, f"ga4_audit_{TODAY}.md")
        with open(path, "w") as f:
            f.write("\n".join(lines))
        print(f"  Report → {path}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply",       action="store_true", help="Apply API fixes")
    parser.add_argument("--dry-run",     action="store_true", help="Show what would be applied")
    parser.add_argument("--audit-only",  action="store_true", help="Print only, no report file")
    parser.add_argument("--client",      help="Run for a single client by name")
    args = parser.parse_args()

    client = get_admin_client()

    targets = GA4_CLIENTS.items()
    if args.client:
        targets = [(k, v) for k, v in targets if args.client.lower() in k.lower()]
        if not targets:
            print(f"Client '{args.client}' not found. Available: {list(GA4_CLIENTS.keys())}")
            sys.exit(1)

    all_findings = []
    for client_name, cfg in targets:
        print(f"\nAuditing {client_name}...")
        try:
            findings = audit_property(client, client_name, cfg)
        except GoogleAPIError as e:
            print(f"  [ERROR] {e}")
            continue

        issues     = findings["issues"]
        api_issues = [i for i in issues if i.get("fix_type") and i["fix_type"] != "manual"]
        print(f"  {_icon(not issues)} {len(findings['ok'])} OK  |  {len(issues)} issues  ({len(api_issues)} API-fixable)")

        if args.apply or args.dry_run:
            print(f"  {'[DRY RUN] ' if args.dry_run else ''}Applying fixes...")
            applied, errors = apply_fixes(client, findings, cfg, dry_run=args.dry_run)
            findings["applied"] = applied
            findings["errors"]  = errors
            for a in applied:
                print(f"    ✓ {a}")
            for e in errors:
                print(f"    ✗ {e}")

        all_findings.append(findings)

    print(f"\nWriting report...")
    write_report(all_findings, audit_only=args.audit_only)

    print("\n" + "="*60)
    print("  Manual steps for each client are in the report.")
    print("  Key next action: set up GTM tags per the checklist,")
    print("  then add internal traffic filter IPs in the GA4 UI.")
    print("="*60)


if __name__ == "__main__":
    main()
