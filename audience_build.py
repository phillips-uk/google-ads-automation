"""
Audience Build — Lee Renée Jewellery

Full audience stack in one run:
  1. Shopify order pull → customer segments by product category
  2. Google Ads Customer Match lists (create + upload hashed emails)
  3. GA4 audiences (Abandoned Cart, Checkout Started, Product Viewers,
     Past Purchasers, Newsletter Subscribers)
  4. PMax signals — Customer Match lists attached as audience signals
     to their matching asset groups
  5. RLSA observation layers on brand Search campaign

Usage:
  python3 audience_build.py                   # full run (live)
  python3 audience_build.py --dry-run         # audit only — no writes
  python3 audience_build.py --skip-shopify    # skip Shopify pull (reuse last counts)
  python3 audience_build.py --phase 1,2,3     # run specific phases only

Config (edit ACCOUNT below if running on a different client):
  CID            : 9364748087
  GA4 property   : properties/393708418
  Shopify shop   : leereneejewellery.myshopify.com
  Shopify token  : SHOPIFY_ACCESS_TOKEN_LEE_RENEE (in .env)
"""

import argparse
import hashlib
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, timedelta

import requests

from config import load_env
load_env()

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# ── Account constants ──────────────────────────────────────────────────────────

CUSTOMER_ID    = "9364748087"
GA4_PROPERTY   = "properties/393708418"
SHOPIFY_SHOP   = "leereneejewellery.myshopify.com"
SHOPIFY_API_V  = "2024-01"

# Asset group IDs → product_type values they cover
ASSET_GROUPS = {
    "Earrings":  {"id": 6714504847, "categories": ["earrings"]},
    "Bracelets": {"id": 6714374021, "categories": ["bracelets"]},
    "Necklaces": {"id": 6470519511, "categories": ["necklaces"]},
    "Rings":     {"id": 6470538875, "categories": ["rings"]},
    "General":   {"id": 6462628693, "categories": []},           # all-purchasers signal
}

# Segments → Customer Match list names
SEGMENTS = {
    "All Purchasers":      "LR — All Purchasers",
    "Repeat Buyers":       "LR — Repeat Buyers (2+ orders)",
    "Recent 90d":          "LR — Recent Purchasers (90d)",
    "Earrings Buyers":     "LR — Category: Earrings Buyers",
    "Bracelets Buyers":    "LR — Category: Bracelets Buyers",
    "Necklaces Buyers":    "LR — Category: Necklaces Buyers",
    "Rings Buyers":        "LR — Category: Rings Buyers",
}

# RLSA bid modifiers on brand Search (+X% for each audience)
RLSA_MODIFIERS = {
    "LR — All Purchasers":              1.40,   # +40%
    "LR — Repeat Buyers (2+ orders)":  1.60,   # +60%
    "LR — Recent Purchasers (90d)":    1.50,   # +50%
    "LR — Category: Earrings Buyers":  1.30,   # +30%
    "LR — Category: Bracelets Buyers": 1.30,
    "LR — Category: Necklaces Buyers": 1.30,
    "LR — Category: Rings Buyers":     1.30,
}

# GA4 audiences to create
GA4_AUDIENCES = [
    {"name": "Abandoned Cart",          "event": "add_to_cart",    "days": 7,
     "desc": "Added to cart but did not purchase — high-intent remarketing"},
    {"name": "Checkout Started",        "event": "begin_checkout", "days": 14,
     "desc": "Started checkout but did not complete — bottom-funnel"},
    {"name": "Product Viewers",         "event": "view_item",      "days": 14,
     "desc": "Viewed a product page but did not purchase"},
    {"name": "Past Purchasers",         "event": "purchase",       "days": 540,
     "desc": "Completed a purchase — loyalty and upsell audience"},
    {"name": "Newsletter Subscribers",  "event": "sign_up",        "days": 90,
     "desc": "Signed up to mailing list — brand awareness audience"},
]


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Shopify: pull + segment customers
# ══════════════════════════════════════════════════════════════════════════════

def _shopify_headers():
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN_LEE_RENEE") or os.environ.get("SHOPIFY_ACCESS_TOKEN")
    if not token:
        print("  [ERROR] SHOPIFY_ACCESS_TOKEN_LEE_RENEE not set in .env")
        sys.exit(1)
    return {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}


def _shopify_get(path, params=None):
    url = f"https://{SHOPIFY_SHOP}/admin/api/{SHOPIFY_API_V}/{path}"
    headers = _shopify_headers()
    results = []
    while url:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        # Return first key's list (products, orders, customers, etc.)
        key = next((k for k in data if isinstance(data[k], list)), None)
        if key:
            results.extend(data[key])
        # Pagination
        link = resp.headers.get("Link", "")
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                m = re.search(r"<([^>]+)>", part)
                if m:
                    next_url = m.group(1)
        url = next_url
        params = {}     # params only on first request
        if next_url:
            time.sleep(0.3)
    return results


def pull_shopify_customers():
    """
    Pull customer data from Shopify.

    Primary path: orders.json (requires read_orders/read_all_orders scope).
    Fallback: customers.json (uses orders_count field for basic segmentation).
    Category-level segmentation (earrings/bracelets/etc.) requires order line
    items and only works on the primary path.

    Returns:
        customer_types: {email: set_of_product_type_strings}
        customer_orders: {email: order_count}
        customer_latest: {email: date_of_latest_order}
        customer_phones: {email: phone_string}
    """
    customer_types  = defaultdict(set)
    customer_orders = defaultdict(int)
    customer_latest = {}
    customer_phones = {}

    # ── Primary: orders API ───────────────────────────────────────────────
    print("  Fetching Shopify products → product_type map...")
    try:
        products = _shopify_get("products.json", {
            "limit": 250,
            "status": "active",
            "fields": "id,product_type",
        })
        product_type_map = {str(p["id"]): (p.get("product_type") or "").strip().lower()
                            for p in products}
        print(f"    {len(product_type_map)} products loaded")
    except Exception as e:
        print(f"    [WARN] Products fetch failed: {e} — skipping product_type map")
        product_type_map = {}

    print("  Fetching Shopify orders (requires read_orders scope)...")
    try:
        orders = _shopify_get("orders.json", {
            "limit": 250,
            "status": "any",
            "financial_status": "paid",
            "fields": "id,email,phone,created_at,line_items",
        })
        print(f"    {len(orders)} paid orders found — category segmentation available")

        for order in orders:
            email = (order.get("email") or "").strip().lower()
            if not email:
                continue
            phone = (order.get("phone") or "").strip()
            if phone:
                customer_phones[email] = phone

            created = order.get("created_at", "")[:10]
            customer_orders[email] += 1
            if email not in customer_latest or created > customer_latest[email]:
                customer_latest[email] = created

            for item in order.get("line_items", []):
                pid = str(item.get("product_id", ""))
                ptype = product_type_map.get(pid, "").lower()
                if ptype:
                    customer_types[email].add(ptype)

        return customer_types, customer_orders, customer_latest, customer_phones

    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            print(f"""
  [WARN] Orders API returned 403 — the access token is missing read_orders scope.

  This token was set up for product/feed management and currently only has
  read_products scope. To add customer + order access:

  Option A — Custom App (recommended):
    1. Shopify Admin → Settings → Apps → Develop apps → Create/edit app
    2. Configuration → Admin API access scopes
    3. Add: read_customers, read_orders, read_all_orders
    4. API credentials → Generate/re-generate access token
    5. Update SHOPIFY_ACCESS_TOKEN_LEE_RENEE in .env

  Option B — Collaborator (if store owner can update permissions):
    dev.shopify.com → Lee Renée store → Permissions
    Add: Customers (Read), Orders (Read), All orders

  Falling back to customers.json — basic segmentation only.
  Category-specific audiences will be empty until scope is granted.
""")
        else:
            print(f"  [WARN] Orders API error: {e} — falling back to customers.json")

    # ── Fallback: customers API ────────────────────────────────────────────
    print("  Fetching Shopify customers (fallback — no category data)...")
    try:
        customers = _shopify_get("customers.json", {
            "limit": 250,
            "fields": "id,email,phone,orders_count,updated_at",
        })
        print(f"    {len(customers)} customers found")
        for c in customers:
            email = (c.get("email") or "").strip().lower()
            if not email:
                continue
            phone = (c.get("phone") or "").strip()
            if phone:
                customer_phones[email] = phone
            oc = int(c.get("orders_count") or 0)
            if oc == 0:
                continue   # never purchased
            customer_orders[email] = oc
            updated = (c.get("updated_at") or "")[:10]
            customer_latest[email] = updated
            # No product_type data available in customers endpoint
    except Exception as e2:
        if hasattr(e2, 'response') and e2.response is not None and e2.response.status_code == 403:
            print("  [WARN] Customers API also returned 403 — add read_customers scope (see instructions above)")
        else:
            print(f"  [ERROR] Customers API also failed: {e2}")
        print("  Customer Match lists will be created as empty shells — upload data once scope is granted")

    return customer_types, customer_orders, customer_latest, customer_phones


def build_segments(customer_types, customer_orders, customer_latest, customer_phones):
    """Returns {segment_key: [email, ...]}"""
    ninety_days_ago = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")
    all_emails = sorted(customer_orders.keys())

    def _match(email, categories):
        if not categories:
            return True
        types = customer_types.get(email, set())
        return bool(types.intersection(set(categories)))

    segments = {
        "All Purchasers":  all_emails,
        "Repeat Buyers":   [e for e in all_emails if customer_orders[e] >= 2],
        "Recent 90d":      [e for e in all_emails if customer_latest.get(e, "") >= ninety_days_ago],
        "Earrings Buyers": [e for e in all_emails if _match(e, ["earrings"])],
        "Bracelets Buyers":[e for e in all_emails if _match(e, ["bracelets"])],
        "Necklaces Buyers":[e for e in all_emails if _match(e, ["necklaces"])],
        "Rings Buyers":    [e for e in all_emails if _match(e, ["rings"])],
    }

    print("\n  Segments:")
    for name, emails in segments.items():
        print(f"    {name:28s}  {len(emails):5,} customers")

    return segments, customer_phones


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Customer Match: create lists + upload hashed emails
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_email(email):
    return email.strip().lower()


def _normalize_phone(phone):
    """Normalise phone to E.164 format (UK: prefix 44)."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("0") and len(digits) == 11:
        digits = "44" + digits[1:]
    elif not digits.startswith("44") and len(digits) == 10:
        digits = "44" + digits
    return "+" + digits if digits else ""


def _sha256(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _search_ads(ads_client, customer_id, query, label="query"):
    ga_service = ads_client.get_service("GoogleAdsService")
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        return list(response)
    except GoogleAdsException as ex:
        msg = ex.failure.errors[0].message if ex.failure.errors else str(ex)
        print(f"  [WARN] GAQL {label}: {msg}")
        return []


def get_existing_user_lists(ads_client, customer_id):
    """Returns {name: resource_name} for all CRM user lists."""
    rows = _search_ads(ads_client, customer_id, """
        SELECT user_list.name, user_list.resource_name, user_list.type
        FROM user_list
        WHERE user_list.type = 'CRM_BASED'
        ORDER BY user_list.name
    """, "user_lists")
    return {r.user_list.name: r.user_list.resource_name for r in rows}


def ensure_user_list(ads_client, customer_id, list_name, description, dry_run=False):
    """
    Get or create a CRM_BASED UserList.
    Returns resource_name.
    """
    existing = get_existing_user_lists(ads_client, customer_id)
    if list_name in existing:
        print(f"    ✓ Exists: {list_name}")
        return existing[list_name]

    if dry_run:
        print(f"    DRY RUN — would create: {list_name}")
        return None

    ul_service = ads_client.get_service("UserListService")
    op = ads_client.get_type("UserListOperation")
    ul = op.create
    ul.name = list_name
    ul.description = description
    ul.membership_status = ads_client.enums.UserListMembershipStatusEnum.OPEN
    ul.membership_life_span = 540   # max 540 days
    ul.crm_based_user_list.upload_key_type = (
        ads_client.enums.CustomerMatchUploadKeyTypeEnum.CONTACT_INFO
    )

    try:
        resp = ul_service.mutate_user_lists(customer_id=customer_id, operations=[op])
        resource_name = resp.results[0].resource_name
        print(f"    + Created: {list_name} → {resource_name}")
        return resource_name
    except GoogleAdsException as ex:
        msg = ex.failure.errors[0].message if ex.failure.errors else str(ex)
        print(f"    [ERROR] Could not create user list '{list_name}': {msg}")
        return None


def upload_customer_match(ads_client, customer_id, user_list_resource,
                          emails, phones_by_email, dry_run=False):
    """
    Upload hashed email (+ phone where available) to a Customer Match list.
    Returns number of members uploaded.
    """
    if not emails:
        print("    (no emails to upload)")
        return 0

    if dry_run:
        print(f"    DRY RUN — would upload {len(emails):,} hashed emails")
        return len(emails)

    job_service = ads_client.get_service("OfflineUserDataJobService")

    # Create job
    job = ads_client.get_type("OfflineUserDataJob")
    job.type_ = ads_client.enums.OfflineUserDataJobTypeEnum.CUSTOMER_MATCH_USER_LIST
    job.customer_match_user_list_metadata.user_list = user_list_resource
    # Consent — confirmed: opt-out consent model (all true by default via Consentik)
    job.customer_match_user_list_metadata.consent.ad_user_data = (
        ads_client.enums.ConsentStatusEnum.GRANTED
    )
    job.customer_match_user_list_metadata.consent.ad_personalization = (
        ads_client.enums.ConsentStatusEnum.GRANTED
    )

    try:
        job_resp = job_service.create_offline_user_data_job(
            customer_id=customer_id, job=job
        )
        job_resource = job_resp.resource_name
    except GoogleAdsException as ex:
        msg = ex.failure.errors[0].message if ex.failure.errors else str(ex)
        print(f"    [ERROR] Could not create upload job: {msg}")
        return 0

    # Build operations in chunks of 1000
    CHUNK = 1000
    total_uploaded = 0

    for chunk_start in range(0, len(emails), CHUNK):
        chunk = emails[chunk_start: chunk_start + CHUNK]
        operations = []
        for email in chunk:
            op = ads_client.get_type("OfflineUserDataJobOperation")
            user_data = op.create
            # Email identifier (SHA-256)
            id_email = user_data.user_identifiers.add()
            id_email.hashed_email = _sha256(_normalize_email(email))

            # Phone identifier if available
            phone = phones_by_email.get(email, "")
            if phone:
                normalised = _normalize_phone(phone)
                if normalised and len(normalised) >= 7:
                    id_phone = user_data.user_identifiers.add()
                    id_phone.hashed_phone_number = _sha256(normalised)
            operations.append(op)

        try:
            job_service.add_offline_user_data_job_operations(
                resource_name=job_resource,
                operations=operations,
            )
            total_uploaded += len(chunk)
        except GoogleAdsException as ex:
            msg = ex.failure.errors[0].message if ex.failure.errors else str(ex)
            print(f"    [ERROR] Batch upload failed (chunk {chunk_start}): {msg}")

    # Run the job
    try:
        job_service.run_offline_user_data_job(resource_name=job_resource)
        print(f"    ↑ Uploading {total_uploaded:,} members (job queued — populates within ~6h)")
    except GoogleAdsException as ex:
        msg = ex.failure.errors[0].message if ex.failure.errors else str(ex)
        print(f"    [ERROR] Could not run job: {msg}")

    return total_uploaded


def run_customer_match(ads_client, segments, phones_by_email, dry_run=False):
    """Phase 2: create lists + upload. Returns {segment_key: user_list_resource}."""
    print("\n── Phase 2: Customer Match ─────────────────────────────────────────────")
    list_resources = {}

    for seg_key, list_name in SEGMENTS.items():
        emails = segments.get(seg_key, [])
        print(f"\n  [{seg_key}] → {list_name}")
        description = f"Lee Renée Jewellery — {seg_key} — built {date.today()}"
        resource = ensure_user_list(ads_client, CUSTOMER_ID, list_name, description, dry_run)
        if resource:
            upload_customer_match(ads_client, CUSTOMER_ID, resource, emails,
                                  phones_by_email, dry_run)
            list_resources[seg_key] = resource

    return list_resources


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — GA4 audiences
# ══════════════════════════════════════════════════════════════════════════════

def _build_event_audience(ga4_types, name, description, event_name, duration_days):
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


def run_ga4_audiences(dry_run=False):
    """Phase 3: create missing GA4 audiences."""
    print("\n── Phase 3: GA4 Audiences ──────────────────────────────────────────────")
    try:
        from ga4_auth import get_admin_client
        from google.analytics.admin_v1alpha import types as ga4_types
        from google.api_core.exceptions import GoogleAPIError, AlreadyExists
    except ImportError as e:
        print(f"  [ERROR] GA4 imports failed: {e}")
        return {}

    ga4_client = get_admin_client()
    existing = {a.display_name: a.name
                for a in ga4_client.list_audiences(parent=GA4_PROPERTY)}
    print(f"  Existing audiences: {len(existing)}")

    created = {}
    for aud in GA4_AUDIENCES:
        name = aud["name"]
        if name in existing:
            print(f"  ✓ {name}")
            created[name] = existing[name]
            continue
        if dry_run:
            print(f"  DRY RUN — would create: {name} ({aud['event']}, {aud['days']}d)")
            continue
        try:
            result = ga4_client.create_audience(
                parent=GA4_PROPERTY,
                audience=_build_event_audience(
                    ga4_types, name, aud["desc"], aud["event"], aud["days"]
                ),
            )
            created[name] = result.name
            print(f"  + Created: {name} → {result.name}")
        except AlreadyExists:
            print(f"  ✓ Already exists: {name}")
        except GoogleAPIError as e:
            print(f"  [ERROR] {name}: {e}")

    return created


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — PMax audience signals
# ══════════════════════════════════════════════════════════════════════════════

# One audience signal per asset group (API limit).
# Category-specific lists are preferred; only one entry per key.
# Groups that already have a signal will be skipped automatically.
PMAX_SIGNAL_MAP = {
    "Earrings":  "Earrings Buyers",
    "Bracelets": "Bracelets Buyers",
    "Necklaces": "Necklaces Buyers",
    "Rings":     "Rings Buyers",
    "General":   "All Purchasers",
}


def get_existing_signals(ads_client, customer_id):
    """Returns set of (asset_group_id, audience_resource) that already exist."""
    rows = _search_ads(ads_client, customer_id, """
        SELECT
          asset_group_signal.asset_group,
          asset_group_signal.audience.audience
        FROM asset_group_signal
        WHERE campaign.status = 'ENABLED'
    """, "existing_signals")
    existing = set()
    for r in rows:
        ag  = r.asset_group_signal.asset_group
        aud = r.asset_group_signal.audience.audience
        existing.add((ag, aud))
    return existing


def get_or_create_audience_wrapper(ads_client, customer_id, user_list_resource,
                                   list_name, dry_run=False):
    """
    Find or create an Audience resource that wraps the given UserList.
    Google Ads AssetGroupSignal requires an Audience resource, not a UserList directly.
    Returns audience resource_name or None.
    """
    audience_name = f"{list_name} (PMax Signal)"
    # Check existing
    rows = _search_ads(ads_client, customer_id, f"""
        SELECT audience.resource_name, audience.name
        FROM audience
        WHERE audience.status != 'REMOVED'
    """, "audiences")
    for r in rows:
        if r.audience.name == audience_name:
            return r.audience.resource_name

    if dry_run:
        print(f"      DRY RUN — would create Audience wrapper: {audience_name}")
        return None

    aud_service = ads_client.get_service("AudienceService")
    op = ads_client.get_type("AudienceOperation")
    aud = op.create
    aud.name = audience_name
    aud.description = f"PMax signal wrapper for Customer Match list: {list_name}"

    # Build user list segment dimension
    # proto-plus uses append() not add(); field name is audience_segments not user_list
    from google.ads.googleads.v24.common.types import audiences as aud_types
    seg = aud_types.AudienceSegment()
    seg.user_list.user_list = user_list_resource

    dim = aud_types.AudienceDimension()
    dim.audience_segments.segments.append(seg)
    aud.dimensions.append(dim)

    try:
        resp = aud_service.mutate_audiences(customer_id=customer_id, operations=[op])
        resource = resp.results[0].resource_name
        print(f"      + Audience wrapper created: {audience_name}")
        return resource
    except GoogleAdsException as ex:
        msg = ex.failure.errors[0].message if ex.failure.errors else str(ex)
        print(f"      [ERROR] Audience wrapper '{audience_name}': {msg}")
        return None


def run_pmax_signals(ads_client, list_resources, dry_run=False):
    """Phase 4: attach Customer Match lists as audience signals to asset groups."""
    print("\n── Phase 4: PMax Audience Signals ──────────────────────────────────────")
    if not list_resources:
        print("  No user lists available — skipping PMax signals")
        return

    existing_signals = get_existing_signals(ads_client, CUSTOMER_ID)
    signal_service = ads_client.get_service("AssetGroupSignalService")

    # Build set of asset_group resources that already have a signal (any audience)
    occupied_groups = {ag for (ag, _) in existing_signals}

    for ag_name, seg_key in PMAX_SIGNAL_MAP.items():
        ag_config = ASSET_GROUPS.get(ag_name)
        if not ag_config:
            continue
        ag_id = ag_config["id"]
        ag_resource = f"customers/{CUSTOMER_ID}/assetGroups/{ag_id}"
        print(f"\n  [{ag_name}] Asset group {ag_id}")

        if ag_resource in occupied_groups:
            print(f"    ✓ Already has a signal — skipping (API allows one audience per group)")
            continue

        user_list_res = list_resources.get(seg_key)
        if not user_list_res:
            print(f"    – {seg_key}: no user list resource — skipping")
            continue

        list_name = SEGMENTS.get(seg_key, seg_key)
        aud_resource = get_or_create_audience_wrapper(
            ads_client, CUSTOMER_ID, user_list_res, list_name, dry_run
        )
        if not aud_resource:
            continue

        if dry_run:
            print(f"    DRY RUN — would attach signal: {seg_key}")
            continue

        op = ads_client.get_type("AssetGroupSignalOperation")
        signal = op.create
        signal.asset_group = ag_resource
        signal.audience.audience = aud_resource

        try:
            signal_service.mutate_asset_group_signals(
                customer_id=CUSTOMER_ID,
                operations=[op],
            )
            print(f"    + Signal attached: {seg_key}")
        except GoogleAdsException as ex:
            msg = ex.failure.errors[0].message if ex.failure.errors else str(ex)
            print(f"    [ERROR] Signal ({seg_key}): {msg}")


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 5 — RLSA observation layers on brand Search campaign
# ══════════════════════════════════════════════════════════════════════════════

def find_brand_search_campaign(ads_client, customer_id):
    """Returns {campaign_id: campaign_name} for Search campaigns."""
    rows = _search_ads(ads_client, customer_id, """
        SELECT campaign.id, campaign.name, campaign.advertising_channel_type
        FROM campaign
        WHERE campaign.status = 'ENABLED'
          AND campaign.advertising_channel_type = 'SEARCH'
    """, "search_campaigns")
    campaigns = {r.campaign.id: r.campaign.name for r in rows}
    print(f"  Search campaigns found: {list(campaigns.values())}")
    return campaigns


def get_ad_groups_in_campaign(ads_client, customer_id, campaign_id):
    """Returns {ad_group_id: ad_group_name}."""
    rows = _search_ads(ads_client, customer_id, f"""
        SELECT ad_group.id, ad_group.name
        FROM ad_group
        WHERE ad_group.status = 'ENABLED'
          AND campaign.id = {campaign_id}
    """, f"ad_groups_{campaign_id}")
    return {r.ad_group.id: r.ad_group.name for r in rows}


def get_existing_audience_criteria(ads_client, customer_id, ad_group_id):
    """Returns set of user_list resource_names already added to this ad group."""
    rows = _search_ads(ads_client, customer_id, f"""
        SELECT
          ad_group_criterion.criterion_id,
          ad_group_criterion.type,
          ad_group_criterion.user_list.user_list
        FROM ad_group_criterion
        WHERE ad_group_criterion.type = 'USER_LIST'
          AND ad_group.id = {ad_group_id}
          AND ad_group_criterion.status != 'REMOVED'
    """, f"audience_criteria_{ad_group_id}")
    return {r.ad_group_criterion.user_list.user_list for r in rows}


def get_existing_campaign_audience_criteria(ads_client, customer_id, campaign_id):
    """Returns set of user_list resource_names already added at campaign level."""
    rows = _search_ads(ads_client, customer_id, f"""
        SELECT
          campaign_criterion.criterion_id,
          campaign_criterion.type,
          campaign_criterion.user_list.user_list
        FROM campaign_criterion
        WHERE campaign_criterion.type = 'USER_LIST'
          AND campaign.id = {campaign_id}
          AND campaign_criterion.status != 'REMOVED'
    """, f"campaign_audience_criteria_{campaign_id}")
    return {r.campaign_criterion.user_list.user_list for r in rows}


def run_rlsa_layers(ads_client, list_resources, dry_run=False):
    """
    Phase 5: add observation audience bid modifiers to brand Search campaign.

    Audience criteria are added at CAMPAIGN level (not ad group level).
    Smart Bidding campaigns require this — ad group-level audience criteria
    conflict with campaign-level criteria (API error 7 if mixed).

    Note: bid modifiers are informational under Smart Bidding (Google's model
    overrides them) but the audience observation is still used for reporting
    and as a signal. Google recommends adding audiences in observation mode
    regardless of bidding strategy.
    """
    print("\n── Phase 5: RLSA Observation Layers ────────────────────────────────────")
    if not list_resources:
        print("  No user lists available — skipping RLSA")
        return

    campaigns = find_brand_search_campaign(ads_client, CUSTOMER_ID)
    if not campaigns:
        print("  No enabled Search campaigns found")
        return

    campaign_criterion_service = ads_client.get_service("CampaignCriterionService")

    for campaign_id, campaign_name in campaigns.items():
        print(f"\n  Campaign: {campaign_name} (ID: {campaign_id})")
        existing_lists = get_existing_campaign_audience_criteria(
            ads_client, CUSTOMER_ID, campaign_id
        )
        operations = []

        for seg_key, list_name in SEGMENTS.items():
            user_list_res = list_resources.get(seg_key)
            if not user_list_res:
                continue
            modifier = RLSA_MODIFIERS.get(list_name, 1.0)
            pct = f"+{(modifier - 1) * 100:.0f}%"

            if user_list_res in existing_lists:
                print(f"    ✓ Already added: {list_name}")
                continue
            if dry_run:
                print(f"    DRY RUN — would add: {list_name} ({pct})")
                continue

            op = ads_client.get_type("CampaignCriterionOperation")
            criterion = op.create
            criterion.campaign = f"customers/{CUSTOMER_ID}/campaigns/{campaign_id}"
            # bid_modifier is largely informational under Smart Bidding but
            # documents intent and aids Google's model as an audience signal
            criterion.bid_modifier = modifier
            criterion.user_list.user_list = user_list_res
            operations.append((op, list_name, pct))

        if not operations:
            print("    (all audiences already added)")
            continue

        try:
            campaign_criterion_service.mutate_campaign_criteria(
                customer_id=CUSTOMER_ID,
                operations=[op for op, _, _ in operations],
            )
            for _, list_name, pct in operations:
                print(f"    + {list_name} ({pct})")
        except GoogleAdsException as ex:
            msg = ex.failure.errors[0].message if ex.failure.errors else str(ex)
            print(f"    [ERROR] RLSA: {msg}")


# ══════════════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════════════

def print_summary(segments, list_resources, dry_run):
    mode = "DRY RUN — no changes made" if dry_run else "LIVE RUN"
    print(f"\n{'=' * 68}")
    print(f"Audience Build Complete — {mode}")
    print(f"{'=' * 68}")
    print("\nCustomer segments:")
    for seg_key, emails in sorted(segments.items(), key=lambda x: -len(x[1])):
        list_name = SEGMENTS.get(seg_key, seg_key)
        uploaded = "→ uploaded" if list_resources.get(seg_key) else "→ FAILED"
        print(f"  {seg_key:28s}  {len(emails):5,}  {uploaded}")

    print("\nNext steps:")
    print("  • Customer Match lists populate within ~6h (min 1,000 users for active targeting)")
    print("  • GA4 audiences start populating immediately; RLSA audiences need 7 days minimum")
    print("  • After 7 days, check audience sizes in Google Ads → Tools → Audience Manager")
    print("  • PMax signals take effect immediately but Google blends them with its own signals")
    print("  • Review RLSA bid modifiers after 4 weeks once data accumulates")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Audit only — no API writes")
    parser.add_argument("--skip-shopify", action="store_true",
                        help="Skip Shopify pull (uses dummy empty segments)")
    parser.add_argument("--phase", default="1,2,3,4,5",
                        help="Comma-separated phases to run (default: 1,2,3,4,5)")
    return parser.parse_args()


def main():
    args = parse_args()
    phases = {int(p.strip()) for p in args.phase.split(",")}
    dry_run = args.dry_run

    if dry_run:
        print("\n[DRY RUN MODE — no changes will be made]\n")

    print(f"Audience Build — Lee Renée Jewellery")
    print(f"  CID:          {CUSTOMER_ID}")
    print(f"  GA4:          {GA4_PROPERTY}")
    print(f"  Date:         {date.today()}")
    print(f"  Phases:       {sorted(phases)}")
    print("=" * 68)

    ads_client = GoogleAdsClient.load_from_storage("google-ads.yaml")

    # ── Phase 1: Shopify ──────────────────────────────────────────────────
    segments = {k: [] for k in SEGMENTS}
    phones_by_email = {}

    if 1 in phases and not args.skip_shopify:
        print("\n── Phase 1: Shopify Customer Pull ──────────────────────────────────────")
        customer_types, customer_orders, customer_latest, phones_by_email = (
            pull_shopify_customers()
        )
        segments, phones_by_email = build_segments(
            customer_types, customer_orders, customer_latest, phones_by_email
        )
    elif args.skip_shopify:
        print("\n── Phase 1: Skipped (--skip-shopify) ───────────────────────────────────")

    # ── Phase 2: Customer Match ───────────────────────────────────────────
    list_resources = {}
    if 2 in phases:
        list_resources = run_customer_match(ads_client, segments, phones_by_email, dry_run)
    else:
        # Load existing LR lists so phases 4 & 5 can reference them
        print("\n── Phase 2: Skipped — loading existing user lists from API ─────────────")
        existing = get_existing_user_lists(ads_client, CUSTOMER_ID)
        for seg_key, list_name in SEGMENTS.items():
            if list_name in existing:
                list_resources[seg_key] = existing[list_name]
                print(f"  ✓ {list_name}")
            else:
                print(f"  – {list_name}: not found in account")

    # ── Phase 3: GA4 Audiences ────────────────────────────────────────────
    if 3 in phases:
        run_ga4_audiences(dry_run)

    # ── Phase 4: PMax Signals ─────────────────────────────────────────────
    if 4 in phases:
        run_pmax_signals(ads_client, list_resources, dry_run)

    # ── Phase 5: RLSA ─────────────────────────────────────────────────────
    if 5 in phases:
        run_rlsa_layers(ads_client, list_resources, dry_run)

    # ── Summary ───────────────────────────────────────────────────────────
    print_summary(segments, list_resources, dry_run)


if __name__ == "__main__":
    main()
