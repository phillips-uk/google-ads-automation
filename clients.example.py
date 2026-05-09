"""
Client configuration — copy this file to clients.py and fill in your values.

    cp clients.example.py clients.py

clients.py is gitignored and never committed. This example file is the only
version that appears in the public repo — no real client names, IDs, or domains.

Add as many clients as you need by duplicating an entry in each dict.
"""

import os

# ── Obsidian base path (also set in .env as OBSIDIAN_BASE) ────────────────────
_VAULT = os.environ.get(
    "OBSIDIAN_BASE",
    os.path.expanduser("~/path/to/your/obsidian/vault/Google Ads Clients"),
)

# ── tracking_audit.py ─────────────────────────────────────────────────────────
AUDIT_ACCOUNTS = {
    "Client A": {
        "folder":                  "Client A",
        "gtm_container_public_id": "GTM-XXXXXXX",
        "ads_customer_id":         "0000000000",    # no dashes
        "platform":                "wordpress",     # or "shopify", "other"
        # Purchase tracking notes go here
    },
    "Client B": {
        "folder":                  "Client B",
        "gtm_container_public_id": "GTM-YYYYYYY",
        "ads_customer_id":         "1111111111",    # no dashes
        "merchant_id":             "000000000",     # Merchant Center ID
        "platform":                "shopify",
        # Purchase tracking: Google & YouTube app (server-side).
    },
}

# ── client_profile_update.py ──────────────────────────────────────────────────
CLIENT_CONFIG = {
    "Client A": {
        "profile_path":       os.path.join(_VAULT, "Client A/Client Profile.md"),
        "ga4_audit_dir":      os.path.join(_VAULT, "Client A/GA4 Audits"),
        "tracking_audit_dir": os.path.join(_VAULT, "Client A/Tracking Audits"),
        "feed_audit_dir":     None,    # set to path string if client has feed audits
    },
    "Client B": {
        "profile_path":       os.path.join(_VAULT, "Client B/Client Profile.md"),
        "ga4_audit_dir":      os.path.join(_VAULT, "Client B/GA4 Audits"),
        "tracking_audit_dir": os.path.join(_VAULT, "Client B/Tracking Audits"),
        "feed_audit_dir":     os.path.join(_VAULT, "Client B/Feed Audits"),
    },
}

# ── ga4_audit.py ──────────────────────────────────────────────────────────────
GA4_CLIENTS = {
    "Client A": {
        "property_id":           "properties/000000000",
        "ads_customer_id":       "0000000000",
        "timezone":              "Europe/London",
        "currency":              "GBP",
        "industry":              "BUSINESS_AND_INDUSTRIAL_MARKETS",
        "site_types":            ["leadgen"],
        "lead_conversion_event": "generate_lead",
        "internal_ips":          [],
        "unwanted_referrals":    ["paypal.com", "stripe.com", "checkout.stripe.com"],
        "search_console_domain": None,
        "merchant_center_id":    None,
    },
    "Client B": {
        "property_id":           "properties/111111111",
        "ads_customer_id":       "1111111111",
        "timezone":              "Europe/London",
        "currency":              "GBP",
        "industry":              "SHOPPING",
        "site_types":            ["ecommerce", "leadgen"],
        "lead_conversion_event": "sign_up",
        "internal_ips":          [],
        "unwanted_referrals":    [
            "shopify.com", "pay.shopify.com",
            "paypal.com", "stripe.com", "checkout.stripe.com",
        ],
        "search_console_domain": "sc-domain:yourclientdomain.com",
        "merchant_center_id":    "000000000",
    },
}

# ── feed_health.py / shopify_feed_optimiser.py / ga4_user_id_setup.py ─────────
# For Shopify-specific scripts that target a single store
SHOPIFY_SHOP            = "yourstore.myshopify.com"
SHOPIFY_ACCOUNT         = "Client B"
SHOPIFY_ACCOUNT_FOLDER  = "Client B"
SHOPIFY_GTM_ID          = "GTM-YYYYYYY"

# ── merchant_feed_audit.py ────────────────────────────────────────────────────
MERCHANT_ID          = "000000000"
MERCHANT_ACCOUNT     = "Client B"
MERCHANT_FOLDER      = "Client B"

# ── seo_search_console.py ─────────────────────────────────────────────────────
# site_url must match the Search Console property exactly.
# Domain properties use "sc-domain:yourdomain.com" format.
# URL-prefix properties use "https://www.yourdomain.com/" format.
SC_CLIENTS = {
    "Client B": {
        "site_url": "sc-domain:yourclientdomain.com",
        "folder":   "Client B",
    },
}

# ── seo_audit.py ─────────────────────────────────────────────────────────────
SEO_AUDIT_CLIENTS = {
    "Client B": {
        "shopify_shop":   "yourstore.myshopify.com",
        "shopify_domain": "yourclientdomain.com",
        "sc_site_url":    "sc-domain:yourclientdomain.com",
        "folder":         "Client B",
        "brand_name":     "Your Brand Name",
    },
}

# ── analytics_report.py ───────────────────────────────────────────────────────
# Combined GA4 Data + Search Console reporting for any property.
#
# ga4_property_id : GA4 property — "properties/XXXXXXXXX"
# sc_site_url     : Search Console property — "sc-domain:example.com" (domain) or
#                   "https://example.com/" (URL-prefix). Must match SC exactly.
#                   Set to None if Search Console is not configured.
# folder          : subfolder under OBSIDIAN_BASE (for client properties)
# obsidian_path   : path relative to vault root, used for non-client properties
#                   (e.g. personal portfolio, personal site). Use instead of folder.
# monday_client   : post priority actions to Monday.com using this client name.
#                   Set to None for properties not tracked in Monday.com.
ANALYTICS_CLIENTS = {
    "Client A": {
        "ga4_property_id": "properties/000000000",
        "sc_site_url":     None,   # add "sc-domain:yourclientdomain.com" when available
        "folder":          "Client A",
        "monday_client":   "Client A",
    },
    "Client B": {
        "ga4_property_id": "properties/111111111",
        "sc_site_url":     "sc-domain:yourclientdomain.com",
        "folder":          "Client B",
        "monday_client":   "Client B",
    },
    # Example: personal/portfolio property stored outside the clients folder
    "My Portfolio": {
        "ga4_property_id": "properties/000000000",
        "sc_site_url":     "https://yourdomain.com/",   # URL-prefix SC property
        "obsidian_path":   "My Brand/Portfolio",        # relative to vault root
        "folder":          None,
        "monday_client":   None,
    },
}

# ── monday_helper.py ──────────────────────────────────────────────────────────
# Your Monday.com board ID (visible in the board URL)
MONDAY_BOARD_ID  = 0000000000

# Map account names → Monday.com board group IDs
# Get group IDs from monday.com board URL or via API.
CLIENT_GROUP_MAP = {
    "Client A": "your_group_id_here",
    "Client B": "your_group_id_here_2",
}
