"""Constants for the Pluxee integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "pluxee"

# OIDC / OAuth2 (Pluxee Connect, Austria consumer client)
OIDC_AUTHORITY = "https://connect.pluxee.app/op"
AUTHORIZE_ENDPOINT = f"{OIDC_AUTHORITY}/oidc/auth"
TOKEN_ENDPOINT = f"{OIDC_AUTHORITY}/oidc/token"
CLIENT_ID = "568135b2-84c2-46a1-b471-9c34238ed924"
REDIRECT_URI = "https://consumers.pluxee.at/oidc/callback"
# NOTE: this client registration does NOT permit "offline_access" - requesting
# it makes the authorize endpoint return error=invalid_scope. Without it the
# refresh token is bound to the OP session and has a short inactivity timeout,
# so the integration must keep it warm with a periodic keep-alive refresh
# (see PluxeeCoordinator) rather than rely on a long-lived offline token.
SCOPE = "openid profile email phone"

# Consumer front API
API_BASE = "https://api.pluxee.app/gl/cwc/consumer-front-api"
OCP_APIM_SUBSCRIPTION_KEY = "76ecbd16117d4163b45136e61f05fb7a"
COUNTRY_CODE = "AT"

# Config entry data keys
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACCESS_TOKEN = "access_token"
CONF_TOKEN_EXPIRES_AT = "token_expires_at"
# Optional raw "Cookie:" request header for connect.pluxee.app (contains the
# long-lived OP "_session" cookie). When present it lets the integration do a
# silent prompt=none re-authentication to recover without user interaction even
# if the (session-bound) refresh token is rejected.
CONF_SESSION_COOKIE = "session_cookie"
CONF_CIAM_ID = "ciam_id"
CONF_EMAIL = "email"
CONF_SCAN_INTERVAL_HOURS = "scan_interval_hours"
CONF_TX_LIMIT = "transaction_count"

DEFAULT_SCAN_INTERVAL_HOURS = 6
DEFAULT_TX_LIMIT = 15
MIN_SCAN_INTERVAL = timedelta(minutes=30)

# The refresh token is session-bound with a short inactivity timeout. The data
# poll can be many hours apart, so we proactively refresh the token on this much
# shorter cadence to keep the session alive between polls. It must stay well
# below the (unknown, but >30min) inactivity window; 25min sits just inside the
# 30min access-token lifetime so each tick rotates the refresh token.
TOKEN_KEEPALIVE_INTERVAL = timedelta(minutes=25)

# Friendly product names by product code (fallback when referentials unavailable)
PRODUCT_NAMES: dict[str, str] = {
    "SPAML": "Meal Pass",
    "SPAFX": "Food Pass",
    "SPAFD": "Food Pass",
    "SPAGF": "Gift Pass",
    "SPAEC": "Eco Pass",
    "SPACL": "Culture Pass",
}
