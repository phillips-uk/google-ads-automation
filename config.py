"""Shared configuration for all Google Ads automation scripts."""

import os
import sys


def load_env():
    """Load .env file from the project directory if present."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    k, v = key.strip(), value.strip()
                    if v and not os.environ.get(k):  # don't override already-set env vars
                        os.environ[k] = v


# Load .env before reading any env vars
load_env()


def _require(key: str) -> str:
    """Return env var value or exit with a clear error if missing."""
    value = os.getenv(key)
    if not value:
        print(f"[config] ERROR: {key} is not set. Add it to your .env file. See .env.example.")
        sys.exit(1)
    return value


MCC_ID        = _require("MCC_ID")
OBSIDIAN_BASE = _require("OBSIDIAN_BASE")
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", 30))


# ── Per-client token helpers ──────────────────────────────────────────────────
#
# Token isolation rule: every client with a Shopify store gets its own env var.
# Naming convention: SHOPIFY_ACCESS_TOKEN_<CLIENT_SLUG>
# e.g. SHOPIFY_ACCESS_TOKEN_LEE_RENEE, SHOPIFY_ACCESS_TOKEN_ENGINE_HOUSE
#
# OAuth token files (GTM, GA4, SC, Merchant Center) follow the same pattern:
# gtm_token.json          → shared (covers all containers under one Google account)
# ga4_token.json          → shared (covers all GA4 properties under one Google account)
# sc_token.json           → shared (covers all Search Console properties)
# merchant_token_<slug>.json → per-client (each MC account is a separate OAuth grant)
#
# When adding a new client: create a fresh token — never reuse an existing one.
# Isolated tokens mean a revoked/expired token only breaks one client,
# and API error logs identify the exact client without ambiguity.

def get_shopify_token(client_name: str) -> str:
    """
    Return the Shopify access token for the given client.

    Looks for SHOPIFY_ACCESS_TOKEN_<CLIENT_SLUG> first (preferred).
    Falls back to SHOPIFY_ACCESS_TOKEN for backwards compatibility,
    printing a deprecation warning so old usages are easy to find.

    client_name: human-readable name matching clients.py key,
                 e.g. "Lee Renee Jewellery" or "Engine House".
    """
    slug = client_name.upper().replace(" ", "_").replace("-", "_")
    per_client_key = f"SHOPIFY_ACCESS_TOKEN_{slug}"
    token = os.getenv(per_client_key)
    if token:
        return token
    # Backwards-compat fallback
    fallback = os.getenv("SHOPIFY_ACCESS_TOKEN")
    if fallback:
        print(
            f"[config] WARNING: {per_client_key} not set — falling back to SHOPIFY_ACCESS_TOKEN. "
            f"Rename it in .env to isolate {client_name} credentials."
        )
        return fallback
    print(
        f"[config] ERROR: Neither {per_client_key} nor SHOPIFY_ACCESS_TOKEN is set. "
        f"Add the token to .env."
    )
    sys.exit(1)


def get_merchant_token_file(client_name: str) -> str:
    """
    Return the path to the Merchant Center OAuth token file for the given client.
    Convention: merchant_token_<slug>.json
    Falls back to merchant_token.json for backwards compatibility.
    """
    import os as _os
    _dir = _os.path.dirname(__file__)
    slug = client_name.lower().replace(" ", "_").replace("-", "_")
    per_client = _os.path.join(_dir, f"merchant_token_{slug}.json")
    if _os.path.exists(per_client):
        return per_client
    fallback = _os.path.join(_dir, "merchant_token.json")
    if _os.path.exists(fallback):
        print(
            f"[config] WARNING: {per_client} not found — falling back to merchant_token.json. "
            f"Re-auth for {client_name} to create an isolated token file."
        )
        return fallback
    # Neither exists — return the per-client path so the auth flow creates the right file
    return per_client
