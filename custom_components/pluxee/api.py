"""API client for Pluxee Austria (OIDC auth + consumer-front balance API)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode, urljoin, urlparse, parse_qs

from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import (
    API_BASE,
    AUTHORIZE_ENDPOINT,
    CLIENT_ID,
    COUNTRY_CODE,
    OCP_APIM_SUBSCRIPTION_KEY,
    PRODUCT_NAMES,
    REDIRECT_URI,
    SCOPE,
    TOKEN_ENDPOINT,
)

_LOGGER = logging.getLogger(__name__)

# Refresh the access token this many seconds before it actually expires.
_TOKEN_EXPIRY_MARGIN = 60


class PluxeeError(Exception):
    """Base error."""


class PluxeeAuthError(PluxeeError):
    """Authentication failed / token invalid or expired (needs re-login)."""


class PluxeeApiError(PluxeeError):
    """A data API call failed."""


# --------------------------------------------------------------------------- #
# PKCE / authorize-url helpers (used by the config flow)
# --------------------------------------------------------------------------- #
def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def build_authorize_url(
    code_challenge: str, state: str, nonce: str, prompt: str | None = None
) -> str:
    """Build the OAuth2 authorize URL.

    ``prompt`` is normally omitted for the interactive login. ``prompt="none"``
    is used for silent re-authentication (no user interaction) when a valid OP
    session cookie is available.
    """
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
    }
    if prompt:
        params["prompt"] = prompt
    return f"{AUTHORIZE_ENDPOINT}?{urlencode(params)}"


# --------------------------------------------------------------------------- #
# Cookie helpers (for silent re-auth via the OP session cookie)
# --------------------------------------------------------------------------- #
# Silent re-auth needs TWO different cookie denylists, because "what to carry
# while following the redirect chain" and "what to persist long-term" are not the
# same set:
#
# 1. IN-FLIGHT - cookies sent while following the authorize -> /interaction ->
#    callback redirect chain. Drop ONLY the Azure load-balancer affinity cookie
#    (ASLBSA/ASLBSACORS): replaying a stale one pins the request to a recycled
#    backend instance that times out (HTTP 408). Everything else MUST be carried,
#    in particular the transient op_interaction/_interaction cookie the OP sets
#    mid-chain: when it routes through a consent step it hands that cookie back at
#    one hop and the NEXT hop requires it. Dropping it makes the next hop fail
#    with HTTP 400 and masquerades as a dead session (the v0.2.3 regression).
_DROP_IN_FLIGHT_PREFIXES = ("aslbsa",)

# 2. DURABLE - what we persist as the long-term session cookie and what the user
#    pastes. Keep only the SSO/device cookies (op_session/_session/op_device);
#    the per-flow interaction/resume cookies and the LB affinity cookie are
#    transient and must never be stored or replayed at the start of a fresh flow.
_DROP_DURABLE_PREFIXES = (
    "aslbsa",
    "op_interaction",
    "_interaction",
    "op_resume",
    "_resume",
)


def _keep_in_flight(name: str) -> bool:
    """Whether a cookie should be carried across silent-reauth redirect hops."""
    return not name.lower().startswith(_DROP_IN_FLIGHT_PREFIXES)


def _is_durable_session_cookie(name: str) -> bool:
    """Whether a cookie belongs in the persisted long-term session cookie."""
    return not name.lower().startswith(_DROP_DURABLE_PREFIXES)


def parse_cookie_header(header: str) -> dict[str, str]:
    """Parse pasted cookies into an ordered name->value dict.

    Accepts either form the user is likely to paste:
      * a raw ``Cookie:`` request header - ``a=b; c=d`` (single line); or
      * the DevTools "Application -> Cookies" table, one cookie per line with
        whitespace/tab-separated columns (``name<TAB>value<TAB>domain...``).

    Only the relevant session cookies (``op_session``/``op_device`` family) are
    kept; load-balancer and transient cookies are filtered out.
    """
    text = (header or "").strip()
    if not text:
        return {}

    jar: dict[str, str] = {}

    def _add(name: str, value: str) -> None:
        name, value = name.strip(), value.strip()
        if name and value and _is_durable_session_cookie(name):
            jar[name] = value

    if "\n" not in text and ";" in text:
        # Raw Cookie header: "a=b; c=d".
        for part in text.split(";"):
            if "=" in part:
                name, value = part.split("=", 1)
                _add(name, value)
        return jar

    # Table / multi-line paste: take name + value from each line.
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "=" in line and "\t" not in line and "  " not in line:
            # e.g. a single "name=value" line.
            name, value = line.split("=", 1)
            _add(name, value)
            continue
        tokens = line.split()
        if len(tokens) >= 2:
            _add(tokens[0], tokens[1])
    return jar


def serialize_cookies(jar: dict[str, str]) -> str:
    """Serialize a cookie dict back into a ``Cookie:`` header value."""
    return "; ".join(f"{k}={v}" for k, v in jar.items() if v)


def update_jar_from_response(jar: dict[str, str], set_cookies: list[str]) -> None:
    """Apply ``Set-Cookie`` headers to the in-flight redirect-chain cookie jar.

    Keeps the rolling SSO cookie AND the transient ``op_interaction`` cookie the
    OP sets mid-flow (the next redirect hop needs it); drops only the load-balancer
    affinity cookie. The interaction cookie is stripped later, when the durable
    session cookie is persisted (see ``_is_durable_session_cookie``).
    """
    for raw in set_cookies:
        first = raw.split(";", 1)[0].strip()
        if "=" not in first:
            continue
        name, value = first.split("=", 1)
        name, value = name.strip(), value.strip()
        if value and value not in ('""', "deleted") and _keep_in_flight(name):
            jar[name] = value


def extract_code(pasted: str) -> str | None:
    """Extract the auth code from a pasted callback URL or a raw code."""
    pasted = pasted.strip()
    if not pasted:
        return None
    if "code=" in pasted or pasted.startswith("http"):
        query = urlparse(pasted).query or pasted.split("?", 1)[-1]
        codes = parse_qs(query).get("code")
        if codes:
            return codes[0]
        # maybe they pasted only "code=XXX"
        if pasted.startswith("code="):
            return pasted[5:]
        return None
    # assume the whole string is the code
    return pasted


async def async_exchange_code(
    session: ClientSession, code: str, code_verifier: str
) -> dict:
    """Exchange an authorization code for tokens."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": code_verifier,
    }
    return await _token_request(session, data)


async def async_refresh(session: ClientSession, refresh_token: str) -> dict:
    """Exchange a refresh token for new tokens (refresh tokens rotate!)."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }
    return await _token_request(session, data)


async def _token_request(session: ClientSession, data: dict) -> dict:
    grant_type = data.get("grant_type", "?")
    try:
        async with session.post(
            TOKEN_ENDPOINT,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                # 4xx with an OAuth error (e.g. invalid_grant) means the refresh
                # token is dead and the user must re-authenticate. Other statuses
                # (5xx, network blips) are transient and must NOT trigger reauth.
                error_code = _oauth_error_code(body)
                _LOGGER.warning(
                    "Pluxee token request (%s) failed: HTTP %s, error=%s, body=%s",
                    grant_type,
                    resp.status,
                    error_code or "?",
                    body[:500],
                )
                if 400 <= resp.status < 500:
                    raise PluxeeAuthError(
                        f"Token endpoint returned {resp.status} ({error_code or 'unknown'})"
                    )
                raise PluxeeApiError(
                    f"Token endpoint returned {resp.status}: {body[:200]}"
                )
            return json.loads(body)
    except ClientError as err:
        raise PluxeeApiError(f"Network error during token request: {err}") from err


def _oauth_error_code(body: str) -> str | None:
    """Best-effort extraction of the OAuth ``error`` field from a token body."""
    try:
        return json.loads(body).get("error")
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Wallet:
    wallet_id: str
    wallet_type: str | None
    balance: float
    currency: str
    status: str | None


@dataclass
class Card:
    unique_card_id: str
    product_code: str
    product_name: str
    masked_pan: str | None
    last4: str | None
    name_on_card: str | None
    status: str | None
    expiry_date: str | None
    preferred: bool
    wallets: list[Wallet] = field(default_factory=list)

    @property
    def balance(self) -> float:
        """Total balance across active wallets."""
        return round(sum(w.balance for w in self.wallets), 2)

    @property
    def currency(self) -> str:
        return self.wallets[0].currency if self.wallets else "EUR"


@dataclass
class Transaction:
    date: str | None
    amount: float
    currency: str
    merchant: str | None
    description: str | None
    transaction_code: str | None
    mobile: bool

    def as_dict(self) -> dict:
        return {
            "date": self.date,
            "amount": self.amount,
            "currency": self.currency,
            "merchant": self.merchant,
            "description": self.description,
            "type": self.transaction_code,
            "mobile": self.mobile,
        }


@dataclass
class PluxeeData:
    ciam_id: str | None
    cards: list[Card]
    transactions: dict[str, list[Transaction]] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class PluxeeClient:
    """Authenticated client. Manages a rotating refresh token + access token."""

    def __init__(
        self,
        session: ClientSession,
        refresh_token: str,
        access_token: str | None = None,
        token_expires_at: float | None = None,
        session_cookie: str | None = None,
        token_updated_cb: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self._refresh_token = refresh_token
        self._access_token = access_token
        self._expires_at = token_expires_at or 0.0
        self._session_cookie = session_cookie or None
        self._token_updated_cb = token_updated_cb
        self._product_names: dict[str, str] = dict(PRODUCT_NAMES)
        self._referentials_loaded = False
        self._refresh_lock = asyncio.Lock()
        self._last_refresh_at = 0.0

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    @property
    def access_token(self) -> str | None:
        return self._access_token

    @property
    def expires_at(self) -> float:
        return self._expires_at

    @property
    def has_session_cookie(self) -> bool:
        """Whether an OP session cookie is available for silent re-auth."""
        return bool(self._session_cookie)

    def token_state(self) -> dict:
        """Current token state, for persisting to the config entry."""
        return {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "token_expires_at": self._expires_at,
            "session_cookie": self._session_cookie,
        }

    @property
    def seconds_until_expiry(self) -> float:
        """Seconds until the current access token expires (<=0 if expired)."""
        return self._expires_at - time.time()

    async def _async_ensure_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        if self._access_token and time.time() < self._expires_at - _TOKEN_EXPIRY_MARGIN:
            return self._access_token
        await self._async_refresh_locked()
        return self._access_token  # type: ignore[return-value]

    async def async_keepalive(self) -> None:
        """Proactively refresh the token to keep the session/refresh token warm.

        The refresh token is bound to the OP session and has a short inactivity
        timeout. The data poll can be hours apart, which would let the refresh
        token expire from inactivity. Calling this on a short cadence rotates the
        refresh token well within that window so the session never lapses.
        """
        await self._async_refresh_locked(force=True)

    async def _async_refresh_locked(self, force: bool = False) -> None:
        """Refresh the access/refresh token under a lock (no concurrent refresh)."""
        async with self._refresh_lock:
            # Another waiter may have just refreshed while we waited on the lock.
            if (
                not force
                and self._access_token
                and time.time() < self._expires_at - _TOKEN_EXPIRY_MARGIN
            ):
                return
            _LOGGER.debug(
                "Refreshing Pluxee access token (force=%s, was_valid_for=%.0fs)",
                force,
                self.seconds_until_expiry,
            )
            try:
                tokens = await async_refresh(self._session, self._refresh_token)
            except PluxeeAuthError:
                # The session-bound refresh token was rejected. If we have an OP
                # session cookie, recover silently (prompt=none) instead of
                # forcing the user to re-authenticate.
                if not self._session_cookie:
                    raise
                _LOGGER.info(
                    "Pluxee refresh token rejected; attempting silent re-auth "
                    "via the stored session cookie"
                )
                await self._async_silent_reauth()
                return
            await self._async_store_tokens(tokens)

    async def _async_silent_reauth(self) -> None:
        """Mint fresh tokens without user interaction using the OP session cookie.

        Performs an OAuth2 authorize request with ``prompt=none`` carrying the
        session cookie, captures the returned authorization code from the
        redirect, and exchanges it for new tokens. Raises ``PluxeeAuthError`` if
        the session cookie is missing/expired (the OP then requires interaction).
        """
        if not self._session_cookie:
            raise PluxeeAuthError("No session cookie available for silent re-auth")
        verifier, challenge = generate_pkce()
        state = secrets.token_urlsafe(16)
        nonce = secrets.token_urlsafe(16)
        # Deliberately NOT prompt=none: once the short-lived grant needs a
        # refresh, prompt=none makes the OP reject with interaction_required.
        # A normal authorize instead walks through the /interaction/consent
        # step, which node-oidc-provider auto-confirms (the user already
        # consented at login) and returns a code - all unattended, as long as
        # the op_session SSO cookie is still valid.
        url = build_authorize_url(challenge, state, nonce)
        code = await self._async_follow_silent_authorize(url, state)
        tokens = await async_exchange_code(self._session, code, verifier)
        _LOGGER.info("Pluxee silent re-auth succeeded")
        await self._async_store_tokens(tokens)

    async def async_session_cookie_works(self) -> bool:
        """Live-check the stored session cookie (used to validate at paste time).

        Returns True if a normal authorize reaches an auth code (the cookie is a
        valid, logged-in SSO session). Returns False if the OP requires
        interaction (cookie missing/expired). Network errors propagate so the
        caller can treat them as "could not validate" rather than "invalid".
        """
        if not self._session_cookie:
            return False
        verifier, challenge = generate_pkce()
        state = secrets.token_urlsafe(16)
        nonce = secrets.token_urlsafe(16)
        url = build_authorize_url(challenge, state, nonce)
        try:
            await self._async_follow_silent_authorize(url, state)
        except PluxeeAuthError:
            return False
        return True

    async def _async_follow_silent_authorize(self, url: str, state: str) -> str:
        """Follow the prompt=none authorize redirects and return the auth code."""
        jar = parse_cookie_header(self._session_cookie or "")
        _LOGGER.debug("Silent re-auth sending session cookies: %s", sorted(jar))
        if "op_session" not in jar:
            _LOGGER.warning(
                "Silent re-auth: stored cookie has no 'op_session' (only %s); it "
                "will be rejected. Re-paste the cookie via Reconfigure.",
                sorted(jar) or "nothing",
            )
        for hop in range(10):
            # Clear the shared session's jar for this domain before every hop so
            # only our explicitly chosen (filtered) cookies are sent. Otherwise
            # aiohttp auto-replays Set-Cookie from earlier hops - including the
            # Azure load-balancer affinity cookie (ASLBSA) that pins us to a
            # recycled backend and causes the intermittent HTTP 408 failures.
            try:
                self._session.cookie_jar.clear_domain("connect.pluxee.app")
            except (AttributeError, TypeError):  # pragma: no cover - jar may be mocked
                pass
            try:
                async with self._session.get(
                    url,
                    headers={
                        "Cookie": serialize_cookies(jar),
                        "User-Agent": "HomeAssistant-Pluxee",
                    },
                    allow_redirects=False,
                    timeout=ClientTimeout(total=20),
                ) as resp:
                    update_jar_from_response(jar, resp.headers.getall("Set-Cookie", []))
                    status = resp.status
                    location = resp.headers.get("Location", "")
            except (ClientError, asyncio.TimeoutError) as err:
                raise PluxeeApiError(
                    f"Network error during silent re-auth: {err}"
                ) from err

            _LOGGER.debug("Silent re-auth hop %s -> HTTP %s", hop, status)
            if status not in (301, 302, 303, 307, 308):
                if 200 <= status < 300:
                    # The OP rendered an interaction (login/consent) page instead
                    # of redirecting: the SSO session is genuinely expired and a
                    # real (OTP) login is required. This 200 is the ONLY
                    # definitive "session lost" signal.
                    raise PluxeeAuthError(
                        f"Silent re-auth needs interaction (HTTP {status}); "
                        "the session cookie is invalid or expired"
                    )
                # Anything else (408/425/429, 5xx backend recycle, or a stray 400
                # from a half-built interaction hop) is transient infra noise, not
                # proof the session died. Raise a transient error so the keep-alive
                # retries next cycle instead of forcing a re-auth prompt.
                raise PluxeeApiError(
                    f"Silent re-auth got transient HTTP {status}; will retry later"
                )
            if not location:
                raise PluxeeAuthError("Silent re-auth redirect missing Location header")
            if location.startswith(REDIRECT_URI):
                query = parse_qs(urlparse(location).query)
                if query.get("state", [None])[0] != state:
                    raise PluxeeAuthError("Silent re-auth state mismatch")
                if "code" in query:
                    # Persist any rolled session cookie before returning, and
                    # log when the op_session value actually changes (rolls) -
                    # this tells us whether Pluxee keeps the SSO session alive by
                    # rotating it (so the integration can stay logged in for as
                    # long as it tracks the new value).
                    old_session = parse_cookie_header(
                        self._session_cookie or ""
                    ).get("op_session")
                    new_session = jar.get("op_session")
                    if new_session and new_session != old_session:
                        _LOGGER.info(
                            "Pluxee op_session ROLLED during silent re-auth "
                            "(value changed) - tracking the new session cookie."
                        )
                    else:
                        _LOGGER.debug(
                            "Pluxee op_session unchanged after silent re-auth."
                        )
                    # Persist only the durable SSO cookies - never the transient
                    # op_interaction cookie we carried across hops to get here.
                    self._session_cookie = serialize_cookies(
                        {k: v for k, v in jar.items() if _is_durable_session_cookie(k)}
                    )
                    return query["code"][0]
                err = query.get("error", ["unknown"])[0]
                raise PluxeeAuthError(
                    f"Silent re-auth rejected by OP: {err} "
                    "(session cookie not accepted - re-paste a fresh one)"
                )
            url = urljoin(url, location)
        raise PluxeeAuthError("Silent re-auth exceeded redirect limit")

    async def _async_store_tokens(self, tokens: dict) -> None:
        self._access_token = tokens["access_token"]
        expires_in = int(tokens.get("expires_in", 1800))
        self._expires_at = time.time() + expires_in
        # Refresh tokens rotate; always keep the newest one.
        rotated = bool(tokens.get("refresh_token"))
        if rotated:
            self._refresh_token = tokens["refresh_token"]

        now = time.time()
        since = (now - self._last_refresh_at) if self._last_refresh_at else None
        self._last_refresh_at = now
        _LOGGER.debug(
            "Pluxee tokens updated: expires_in=%ss, refresh_token_rotated=%s, "
            "scope=%r, seconds_since_previous_refresh=%s",
            expires_in,
            rotated,
            tokens.get("scope", ""),
            f"{since:.0f}" if since is not None else "n/a",
        )

        if self._token_updated_cb:
            await self._token_updated_cb(
                {
                    "access_token": self._access_token,
                    "refresh_token": self._refresh_token,
                    "token_expires_at": self._expires_at,
                    "session_cookie": self._session_cookie,
                }
            )

    def _headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Ocp-Apim-Subscription-Key": OCP_APIM_SUBSCRIPTION_KEY,
            "Country-Code": COUNTRY_CODE,
            "Accept": "application/json",
            "User-Agent": "HomeAssistant-Pluxee",
        }

    async def _async_get(self, path: str, _allow_recovery: bool = True) -> dict:
        access_token = await self._async_ensure_token()
        url = f"{API_BASE}{path}"
        try:
            async with self._session.get(url, headers=self._headers(access_token)) as resp:
                if resp.status in (401, 403):
                    if _allow_recovery:
                        # The stored access token is stale/revoked (common right
                        # after an HA/Docker restart, where token rotation across
                        # the restart can revoke the chain). Don't give up: force a
                        # token refresh - which falls back to silent re-auth via
                        # the stored session cookie - and retry the call once.
                        _LOGGER.debug(
                            "Pluxee API %s returned %s; forcing token refresh and "
                            "retrying once before reporting an auth failure.",
                            path,
                            resp.status,
                        )
                        await self._async_refresh_locked(force=True)
                        return await self._async_get(path, _allow_recovery=False)
                    raise PluxeeAuthError(
                        f"API {path} returned {resp.status} after a token refresh"
                    )
                if resp.status != 200:
                    text = await resp.text()
                    raise PluxeeApiError(f"API {path} returned {resp.status}: {text}")
                return await resp.json()
        except ClientError as err:
            raise PluxeeApiError(f"Network error calling {path}: {err}") from err

    async def _async_load_referentials(self) -> None:
        if self._referentials_loaded:
            return
        try:
            data = await self._async_get("/v2/product-referentials")
            for item in data.get("data", []):
                attrs = item.get("attributes", {})
                code = attrs.get("type") or attrs.get("countryProductTypeId")
                name = attrs.get("name")
                if code and name:
                    self._product_names[code] = name
            self._referentials_loaded = True
        except PluxeeError as err:
            _LOGGER.debug("Could not load product referentials: %s", err)

    async def async_get_data(self, tx_limit: int = 15) -> PluxeeData:
        """Fetch all cards, balances and (optionally) recent transactions."""
        await self._async_load_referentials()
        raw = await self._async_get("/v3/spl/cardsInfos")
        data = self._parse_cards_infos(raw)
        if tx_limit > 0:
            for card in data.cards:
                try:
                    data.transactions[card.unique_card_id] = (
                        await self.async_get_transactions(card.unique_card_id, tx_limit)
                    )
                except PluxeeApiError as err:
                    _LOGGER.debug(
                        "Could not load transactions for %s: %s",
                        card.unique_card_id,
                        err,
                    )
                    data.transactions[card.unique_card_id] = []
        return data

    async def async_get_transactions(
        self, card_id: str, limit: int = 15
    ) -> list[Transaction]:
        """Fetch the most recent transactions for a card."""
        raw = await self._async_get(f"/v2/spl/cards/{card_id}/transactions")
        result: list[Transaction] = []
        for item in raw.get("data", []):
            a = item.get("attributes", {})
            result.append(
                Transaction(
                    date=a.get("transactionDateTime") or a.get("settlementDate"),
                    amount=a.get("transactionAmount", a.get("billingAmount", 0)),
                    currency=a.get("transactionCurrency", "EUR"),
                    merchant=a.get("merchantName"),
                    description=a.get("description"),
                    transaction_code=a.get("transactionCode"),
                    mobile=a.get("mobilePayment") == "Y",
                )
            )
        return result[:limit]

    def _parse_cards_infos(self, raw: dict) -> PluxeeData:
        cards: list[Card] = []
        for consumer in raw.get("consumerCardList", []):
            for info in consumer.get("cardInfoList", []):
                wallets: list[Wallet] = []
                for account in info.get("accountBalanceList", []):
                    for w in account.get("walletBalanceList", []):
                        exponent = w.get("exponent", 2)
                        amount = w.get("amount", 0)
                        balance = amount / (10 ** exponent)
                        wallets.append(
                            Wallet(
                                wallet_id=w.get("uniqueWalletId", ""),
                                wallet_type=w.get("walletType"),
                                balance=round(balance, 2),
                                currency=w.get("currency", "EUR"),
                                status=w.get("walletStatus"),
                            )
                        )
                code = info.get("productCode", "")
                cards.append(
                    Card(
                        unique_card_id=info.get("uniqueCardId", ""),
                        product_code=code,
                        product_name=self._product_names.get(code, code or "Pluxee Card"),
                        masked_pan=info.get("maskedPan"),
                        last4=info.get("panLastFourDigits"),
                        name_on_card=info.get("nameOnCard"),
                        status=info.get("cardStatus"),
                        expiry_date=info.get("expiryDate"),
                        preferred=bool(info.get("preferredCard")),
                        wallets=wallets,
                    )
                )
        return PluxeeData(ciam_id=raw.get("ciamId"), cards=cards)
