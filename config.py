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
