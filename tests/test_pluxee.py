"""Tests for the Pluxee integration."""
import base64
import json
import time

import pytest
from homeassistant.config_entries import SOURCE_USER, SOURCE_REAUTH
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pluxee.const import (
    API_BASE,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    DOMAIN,
    TOKEN_ENDPOINT,
)

CALLBACK_URL = "https://consumers.pluxee.at/oidc/callback?code=THECODE&state=xyz&iss=x"


def _id_token(email: str) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'email': email})}.sig"


def _token_response(refresh="REFRESH1") -> dict:
    return {
        "access_token": "ACCESS1",
        "refresh_token": refresh,
        "expires_in": 1800,
        "id_token": _id_token("sodexo@gmail.com"),
        "scope": "openid profile email phone",
        "token_type": "Bearer",
    }


CARDS_INFOS = {
    "ciamId": "CIAM123",
    "consumerCardList": [
        {
            "uniqueConsumerId": "C1",
            "cardInfoList": [
                {
                    "uniqueCardId": "CARD1",
                    "productCode": "SPAML",
                    "maskedPan": "123456XXXXXX1234",
                    "panLastFourDigits": "1234",
                    "nameOnCard": "SOME/SODEXO",
                    "cardStatus": "A",
                    "expiryDate": "012026",
                    "preferredCard": True,
                    "accountBalanceList": [
                        {"walletBalanceList": [
                            {"uniqueWalletId": "W1", "currency": "EUR",
                             "amount": 8311, "exponent": 2,
                             "walletType": "ML", "walletStatus": "N"}]}
                    ],
                },
                {
                    "uniqueCardId": "CARD2",
                    "productCode": "SPAFX",
                    "maskedPan": "12345604XXXX5678",
                    "panLastFourDigits": "5678",
                    "nameOnCard": "SOME/SODEXO",
                    "cardStatus": "A",
                    "expiryDate": "012026",
                    "preferredCard": True,
                    "accountBalanceList": [
                        {"walletBalanceList": [
                            {"uniqueWalletId": "W2", "currency": "EUR",
                             "amount": 630, "exponent": 2,
                             "walletType": "ML", "walletStatus": "N"}]}
                    ],
                },
            ],
        }
    ],
}

REFERENTIALS = {
    "data": [
        {"type": "product-referentials", "id": "28",
         "attributes": {"type": "SPAML", "name": "Meal Pass"}},
        {"type": "product-referentials", "id": "29",
         "attributes": {"type": "SPAFX", "name": "Food Pass"}},
    ]
}


TRANSACTIONS = {
    "data": [
        {"type": "transactions", "id": "T1", "attributes": {
            "transactionAmount": -24, "transactionCurrency": "EUR",
            "merchantName": "Cameo Lounge", "description": "Purchase",
            "transactionCode": "Purchase", "mobilePayment": "Y",
            "transactionDateTime": "2026-06-11T13:15:47.000Z"}},
        {"type": "transactions", "id": "T2", "attributes": {
            "transactionAmount": -8.4, "transactionCurrency": "EUR",
            "merchantName": "BILLA", "description": "Purchase",
            "transactionCode": "Purchase", "mobilePayment": "Y",
            "transactionDateTime": "2026-06-09T12:29:05.000Z"}},
    ]
}


def _mock_api(aioclient_mock: AiohttpClientMocker, token=None):
    aioclient_mock.post(TOKEN_ENDPOINT, json=token or _token_response())
    aioclient_mock.get(f"{API_BASE}/v2/product-referentials", json=REFERENTIALS)
    aioclient_mock.get(f"{API_BASE}/v3/spl/cardsInfos", json=CARDS_INFOS)
    aioclient_mock.get(f"{API_BASE}/v2/spl/cards/CARD1/transactions", json=TRANSACTIONS)
    aioclient_mock.get(f"{API_BASE}/v2/spl/cards/CARD2/transactions", json=TRANSACTIONS)


def test_authorize_url_does_not_request_offline_access():
    """offline_access is rejected (invalid_scope) by this client - must be absent."""
    from urllib.parse import parse_qs, urlparse

    from custom_components.pluxee.api import build_authorize_url

    url = build_authorize_url("challenge", "state", "nonce")
    q = parse_qs(urlparse(url).query)
    assert "offline_access" not in q["scope"][0].split()
    assert "prompt" not in q


async def test_config_flow_creates_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    _mock_api(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    # The auth URL must be shown to the user.
    assert "connect.pluxee.app" in result["description_placeholders"]["auth_url"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback_url": CALLBACK_URL}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "sodexo@gmail.com"
    data = result["data"]
    assert data[CONF_REFRESH_TOKEN] == "REFRESH1"
    assert data["ciam_id"] == "CIAM123"
    # token endpoint should have been called exactly once (no premature refresh)
    token_calls = [c for c in aioclient_mock.mock_calls if str(c[1]) == TOKEN_ENDPOINT]
    assert len(token_calls) == 1


async def test_config_flow_rejects_invalid_session_cookie(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """A wrong-format/expired cookie is rejected at paste time with a clear error."""
    from custom_components.pluxee.api import AUTHORIZE_ENDPOINT

    _mock_api(aioclient_mock)
    # Authorize returns a 200 interaction (login) page -> cookie is not valid.
    aioclient_mock.get(AUTHORIZE_ENDPOINT, status=200, text="<login page>")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"callback_url": CALLBACK_URL, "session_cookie": "garbage-without-op-session"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "session_cookie_invalid"}
    # The single-use code must NOT have been consumed (validation ran first).
    token_calls = [c for c in aioclient_mock.mock_calls if str(c[1]) == TOKEN_ENDPOINT]
    assert len(token_calls) == 0


async def test_config_flow_bad_code(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    _mock_api(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback_url": "https://example.com/no-code-here"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "no_code"}


async def _setup_entry(hass, aioclient_mock, expires_at):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="CIAM123",
        title="sodexo@gmail.com",
        data={
            CONF_REFRESH_TOKEN: "OLD_REFRESH",
            CONF_ACCESS_TOKEN: "ACCESS_OLD",
            CONF_TOKEN_EXPIRES_AT: expires_at,
            "ciam_id": "CIAM123",
            "email": "sodexo@gmail.com",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_sensors_created(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    _mock_api(aioclient_mock)
    # token valid far in the future -> no refresh needed
    await _setup_entry(hass, aioclient_mock, time.time() + 9999)

    meal = hass.states.get("sensor.meal_pass_1234_balance")
    food = hass.states.get("sensor.food_pass_5678_balance")
    assert meal is not None, hass.states.async_entity_ids("sensor")
    assert meal.state == "83.11"
    assert meal.attributes["product_code"] == "SPAML"
    assert meal.attributes["last4"] == "1234"
    assert food.state == "6.3"
    # No token refresh because access token was valid
    token_calls = [c for c in aioclient_mock.mock_calls if str(c[1]) == TOKEN_ENDPOINT]
    assert len(token_calls) == 0

    # Last-transaction sensor with history in attributes
    tx = hass.states.get("sensor.meal_pass_1234_last_transaction")
    assert tx is not None and tx.state == "-24"
    assert tx.attributes["last_merchant"] == "Cameo Lounge"
    assert len(tx.attributes["transactions"]) == 2
    assert tx.attributes["transactions"][1]["merchant"] == "BILLA"


async def test_token_refresh_and_rotation_persisted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    # refresh returns a NEW rotated refresh token
    _mock_api(aioclient_mock, token=_token_response(refresh="ROTATED_REFRESH"))
    # token already expired -> coordinator must refresh on first update
    entry = await _setup_entry(hass, aioclient_mock, time.time() - 10)

    meal = hass.states.get("sensor.meal_pass_1234_balance")
    assert meal is not None and meal.state == "83.11"
    # rotated refresh token must be persisted back to the entry
    assert entry.data[CONF_REFRESH_TOKEN] == "ROTATED_REFRESH"
    token_calls = [c for c in aioclient_mock.mock_calls if str(c[1]) == TOKEN_ENDPOINT]
    assert len(token_calls) == 1


async def test_keepalive_refreshes_and_rotates_while_token_still_valid(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """Keep-alive must rotate the refresh token even when access token is valid.

    This is what keeps the session-bound refresh token from expiring between the
    (hours-apart) data polls.
    """
    _mock_api(aioclient_mock, token=_token_response(refresh="KEEPALIVE_REFRESH"))
    # access token valid far into the future -> a data poll would NOT refresh
    entry = await _setup_entry(hass, aioclient_mock, time.time() + 9999)
    coordinator = entry.runtime_data

    token_calls_before = len(
        [c for c in aioclient_mock.mock_calls if str(c[1]) == TOKEN_ENDPOINT]
    )
    await coordinator._async_keepalive(None)
    await hass.async_block_till_done()

    # keep-alive forced a refresh despite the still-valid access token...
    token_calls_after = len(
        [c for c in aioclient_mock.mock_calls if str(c[1]) == TOKEN_ENDPOINT]
    )
    assert token_calls_after == token_calls_before + 1
    # ...and the rotated refresh token was persisted to the entry
    assert entry.data[CONF_REFRESH_TOKEN] == "KEEPALIVE_REFRESH"


async def test_token_4xx_raises_auth_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """A 4xx invalid_grant means the refresh token is dead -> auth error."""
    from custom_components.pluxee.api import (
        PluxeeAuthError,
        async_refresh,
    )
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.post(
        TOKEN_ENDPOINT, status=400, json={"error": "invalid_grant"}
    )
    with pytest.raises(PluxeeAuthError):
        await async_refresh(async_get_clientsession(hass), "DEAD_REFRESH")


async def test_token_5xx_is_transient_not_auth_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """A 5xx from the token endpoint is transient and must NOT force reauth."""
    from custom_components.pluxee.api import (
        PluxeeApiError,
        PluxeeAuthError,
        async_refresh,
    )
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.post(TOKEN_ENDPOINT, status=503, text="upstream down")
    with pytest.raises(PluxeeApiError) as exc:
        await async_refresh(async_get_clientsession(hass), "GOOD_REFRESH")
    assert not isinstance(exc.value, PluxeeAuthError)


def test_client_reports_session_cookie_presence():
    from custom_components.pluxee.api import PluxeeClient

    assert not PluxeeClient(session=None, refresh_token="r").has_session_cookie
    assert not PluxeeClient(
        session=None, refresh_token="r", session_cookie=""
    ).has_session_cookie
    assert PluxeeClient(
        session=None, refresh_token="r", session_cookie="op_session=x"
    ).has_session_cookie


def test_parse_cookie_header_raw_and_table_forms():
    """Accept both a raw Cookie header and the DevTools Application-tab table."""
    from custom_components.pluxee.api import parse_cookie_header, serialize_cookies

    raw = "op_session=ABC; op_session.sig=SIG; op_interaction=X"
    jar = parse_cookie_header(raw)
    assert jar == {"op_session": "ABC", "op_session.sig": "SIG"}  # interaction dropped

    # Application -> Cookies table paste (tab-separated columns, one per line).
    table = (
        "op_interaction.legacy\tNt259\tconnect.pluxee.app\t/op\t...\t42\t✓\n"
        "op_session\t8kxyRxMQJht3kgcjELety\tconnect.pluxee.app\t/op\t...\t31\t✓\n"
        "op_session.sig\toKjZwbJJLWk44YfkNrWzl7FbZui\tconnect.pluxee.app\t/op\t...\n"
        "op_session.legacy\t8kxyRxMQJht3kgcjELety\tconnect.pluxee.app\t/op\t...\n"
        "op_session.legacy.sig\tLkcdbkHu5pNgubhqIF4WafPXZui\tconnect.pluxee.app\t/op\n"
        "theme\tundefined\tconsumers.pluxee.at\t/\t...\n"
    )
    jar = parse_cookie_header(table)
    assert jar["op_session"] == "8kxyRxMQJht3kgcjELety"
    assert jar["op_session.sig"] == "oKjZwbJJLWk44YfkNrWzl7FbZui"
    assert "op_interaction.legacy" not in jar  # transient cookie dropped
    header = serialize_cookies(jar)
    assert "op_session=8kxyRxMQJht3kgcjELety" in header
    assert "op_session.sig=oKjZwbJJLWk44YfkNrWzl7FbZui" in header


async def _silent_authorize_side_effect(code="SILENT_CODE", error=None):
    """Build a side_effect that echoes the request's state into a 302 redirect."""
    from urllib.parse import parse_qs, urlparse

    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMockResponse,
    )
    from yarl import URL

    async def _se(method, url, data):
        state = parse_qs(urlparse(str(url)).query).get("state", [""])[0]
        if error:
            loc = f"https://consumers.pluxee.at/oidc/callback?error={error}&state={state}"
        else:
            loc = (
                "https://consumers.pluxee.at/oidc/callback"
                f"?code={code}&state={state}"
            )
        return AiohttpClientMockResponse(
            method,
            URL(url),
            status=302,
            headers={"Location": loc, "Set-Cookie": "_session=ROLLED; Path=/op"},
        )

    return _se


async def test_silent_reauth_recovers_without_user(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """When the refresh token is dead but a session cookie exists, recover silently."""
    from custom_components.pluxee.api import AUTHORIZE_ENDPOINT
    from custom_components.pluxee.const import CONF_SESSION_COOKIE

    # refresh_token grant is rejected (dead session-bound RT)...
    aioclient_mock.post(
        TOKEN_ENDPOINT,
        side_effect=await _make_token_side_effect(),
    )
    # ...but the prompt=none authorize (with the cookie) returns a fresh code.
    aioclient_mock.get(
        AUTHORIZE_ENDPOINT, side_effect=await _silent_authorize_side_effect()
    )
    aioclient_mock.get(f"{API_BASE}/v2/product-referentials", json=REFERENTIALS)
    aioclient_mock.get(f"{API_BASE}/v3/spl/cardsInfos", json=CARDS_INFOS)
    aioclient_mock.get(f"{API_BASE}/v2/spl/cards/CARD1/transactions", json=TRANSACTIONS)
    aioclient_mock.get(f"{API_BASE}/v2/spl/cards/CARD2/transactions", json=TRANSACTIONS)

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="CIAM123", title="sodexo@gmail.com",
        data={
            CONF_REFRESH_TOKEN: "DEAD_RT", CONF_ACCESS_TOKEN: "DEAD_AT",
            CONF_TOKEN_EXPIRES_AT: time.time() - 10,  # expired -> forces refresh
            CONF_SESSION_COOKIE: "_session=GOOD; _session.sig=SIG",
            "ciam_id": "CIAM123", "email": "sodexo@gmail.com",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Sensors came up -> silent re-auth succeeded without reauth flow.
    meal = hass.states.get("sensor.meal_pass_1234_balance")
    assert meal is not None and meal.state == "83.11"
    # New tokens from the silent exchange were persisted.
    assert entry.data[CONF_REFRESH_TOKEN] == "SILENT_REFRESH"
    # The rolled session cookie was captured and persisted.
    assert "_session=ROLLED" in entry.data[CONF_SESSION_COOKIE]
    # No reauth flow should be in progress.
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def _make_token_side_effect():
    """Token endpoint: reject refresh_token grant (400), accept auth code grant."""
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMockResponse,
    )
    from yarl import URL

    async def _se(method, url, data):
        grant = (data or {}).get("grant_type")
        if grant == "refresh_token":
            return AiohttpClientMockResponse(
                method, URL(url), status=400, json={"error": "invalid_grant"}
            )
        return AiohttpClientMockResponse(
            method, URL(url), json=_token_response(refresh="SILENT_REFRESH")
        )

    return _se


async def test_silent_reauth_interaction_required_triggers_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """If the session cookie is also dead, fall back to a reauth flow."""
    from custom_components.pluxee.api import AUTHORIZE_ENDPOINT
    from custom_components.pluxee.const import CONF_SESSION_COOKIE

    aioclient_mock.post(TOKEN_ENDPOINT, status=400, json={"error": "invalid_grant"})
    # prompt=none returns a 200 interaction page (session cookie expired).
    aioclient_mock.get(AUTHORIZE_ENDPOINT, status=200, text="<login page>")

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="CIAM123", title="sodexo@gmail.com",
        data={
            CONF_REFRESH_TOKEN: "DEAD_RT", CONF_ACCESS_TOKEN: "DEAD_AT",
            CONF_TOKEN_EXPIRES_AT: time.time() - 10,
            CONF_SESSION_COOKIE: "_session=EXPIRED",
            "ciam_id": "CIAM123", "email": "sodexo@gmail.com",
        },
    )
    entry.add_to_hass(hass)
    # First refresh fails auth -> setup returns False and a reauth flow starts.
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # A reauth flow should have been started.
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_reauth_flow(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    _mock_api(aioclient_mock, token=_token_response(refresh="REAUTH_REFRESH"))
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="CIAM123", title="sodexo@gmail.com",
        data={
            CONF_REFRESH_TOKEN: "DEAD", CONF_ACCESS_TOKEN: "DEAD",
            CONF_TOKEN_EXPIRES_AT: time.time() + 9999,
            "ciam_id": "CIAM123", "email": "sodexo@gmail.com",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback_url": CALLBACK_URL}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "REAUTH_REFRESH"


# --------------------------------------------------------------------------- #
# Reactive 401 recovery (regression test for auth-lost-after-restart)
# --------------------------------------------------------------------------- #
class _FakeHeaders(dict):
    def getall(self, key, default=None):
        val = self.get(key)
        if val is None:
            return default if default is not None else []
        return val if isinstance(val, list) else [val]


class _FakeResp:
    def __init__(self, status, body="", json_data=None, headers=None):
        self.status = status
        self._body = body
        self._json = json_data
        self.headers = _FakeHeaders(headers or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._body

    async def json(self):
        return self._json


class _FakeSession:
    def __init__(self, get_responses, post_responses):
        self._get = list(get_responses)
        self._post = list(post_responses)
        self.get_calls = 0
        self.post_calls = 0

    def get(self, url, headers=None, allow_redirects=True, **kwargs):
        self.get_calls += 1
        return self._get.pop(0)

    def post(self, url, data=None, headers=None, **kwargs):
        self.post_calls += 1
        return self._post.pop(0)


async def test_async_get_recovers_from_401_by_refreshing():
    """A stale access token (401) must trigger a refresh + retry, not give up.

    This is the after-restart scenario: the stored access token looks unexpired
    but the server rejects it; the client should refresh (rotating the token),
    persist the new tokens, and succeed without prompting for re-auth.
    """
    from custom_components.pluxee.api import PluxeeClient

    cards = {"ciamId": "X", "consumerCardList": []}
    sess = _FakeSession(
        get_responses=[_FakeResp(401), _FakeResp(200, json_data=cards)],
        post_responses=[
            _FakeResp(
                200,
                body=json.dumps(
                    {"access_token": "AT2", "refresh_token": "RT2", "expires_in": 1800}
                ),
            )
        ],
    )
    saved = []

    async def cb(tokens):
        saved.append(tokens)

    client = PluxeeClient(
        session=sess,
        refresh_token="RT1",
        access_token="AT_STALE",
        token_expires_at=time.time() + 1800,  # looks valid -> no proactive refresh
        token_updated_cb=cb,
    )

    data = await client._async_get("/v3/spl/cardsInfos")

    assert data == cards
    assert sess.get_calls == 2  # 401, then successful retry
    assert sess.post_calls == 1  # exactly one refresh
    assert client.refresh_token == "RT2"  # rotated token kept
    assert saved and saved[-1]["refresh_token"] == "RT2"  # and persisted


async def test_async_get_401_twice_raises_auth_error():
    """If it still 401s after a refresh, surface a PluxeeAuthError (real reauth)."""
    from custom_components.pluxee.api import PluxeeAuthError, PluxeeClient

    sess = _FakeSession(
        get_responses=[_FakeResp(401), _FakeResp(401)],
        post_responses=[
            _FakeResp(
                200,
                body=json.dumps(
                    {"access_token": "AT2", "refresh_token": "RT2", "expires_in": 1800}
                ),
            )
        ],
    )
    client = PluxeeClient(
        session=sess,
        refresh_token="RT1",
        access_token="AT_STALE",
        token_expires_at=time.time() + 1800,
    )
    with pytest.raises(PluxeeAuthError):
        await client._async_get("/v3/spl/cardsInfos")
    assert sess.get_calls == 2


# --------------------------------------------------------------------------- #
# Cookie filtering + transient silent-reauth handling (the overnight-408 fix)
# --------------------------------------------------------------------------- #
def test_parse_cookie_header_drops_loadbalancer_cookies():
    from custom_components.pluxee.api import parse_cookie_header

    raw = (
        "ASLBSA=abc; ASLBSACORS=def; op_device=d1; op_device.sig=s1; "
        "op_session=sess; op_session.sig=ssig; op_interaction=junk"
    )
    jar = parse_cookie_header(raw)
    assert "op_session" in jar and jar["op_session"] == "sess"
    assert "op_device" in jar
    # Infra / transient cookies must be dropped.
    assert "ASLBSA" not in jar
    assert "ASLBSACORS" not in jar
    assert "op_interaction" not in jar


def test_update_jar_from_response_drops_affinity_cookies():
    from custom_components.pluxee.api import update_jar_from_response

    jar = {"op_session": "old"}
    update_jar_from_response(
        jar,
        [
            "ASLBSA=newaffinity; Path=/; HttpOnly",
            "op_session=rolled; Path=/op; Secure",
            # The transient interaction cookie the OP sets mid-flow MUST be kept
            # in-flight: the next redirect hop requires it.
            "op_interaction=INT; Path=/op; HttpOnly",
        ],
    )
    assert jar["op_session"] == "rolled"  # rolled SSO cookie tracked
    assert "ASLBSA" not in jar  # affinity cookie ignored
    assert jar["op_interaction"] == "INT"  # interaction cookie carried across hops


async def test_silent_reauth_408_is_transient_not_auth_error():
    """A 408 during silent re-auth must be transient (retry), not 'session lost'."""
    from custom_components.pluxee.api import PluxeeApiError, PluxeeClient

    sess = _FakeSession(get_responses=[_FakeResp(408)], post_responses=[])
    client = PluxeeClient(
        session=sess,
        refresh_token="RT",
        session_cookie="op_session=abc; op_session.sig=def",
    )
    with pytest.raises(PluxeeApiError):
        await client._async_follow_silent_authorize(
            "https://connect.pluxee.app/op/oidc/auth?x=1", "state123"
        )


async def test_silent_reauth_400_at_interaction_hop_is_transient():
    """A stray 400 mid-redirect-chain must NOT force a user reauth prompt.

    Only a 200 (rendered interaction page) means the session is genuinely dead;
    everything else is transient infra noise that the keep-alive retries.
    """
    from custom_components.pluxee.api import (
        PluxeeApiError,
        PluxeeAuthError,
        PluxeeClient,
    )

    sess = _FakeSession(
        get_responses=[
            _FakeResp(303, headers={"Location": "https://connect.pluxee.app/op/x"}),
            _FakeResp(400),
        ],
        post_responses=[],
    )
    client = PluxeeClient(
        session=sess,
        refresh_token="RT",
        session_cookie="op_session=abc; op_session.sig=def",
    )
    with pytest.raises(PluxeeApiError) as exc:
        await client._async_follow_silent_authorize(
            "https://connect.pluxee.app/op/oidc/auth?x=1", "state123"
        )
    assert not isinstance(exc.value, PluxeeAuthError)


async def test_silent_reauth_carries_interaction_cookie_across_hops():
    """Regression (v0.2.3): when the OP routes silent re-auth through an
    /interaction consent step, it sets a transient op_interaction cookie at one
    hop that the NEXT hop requires. That cookie must be carried forward (only the
    load-balancer affinity cookie is dropped in-flight), and must NOT leak into
    the persisted durable session cookie.
    """
    from custom_components.pluxee.api import PluxeeClient, parse_cookie_header

    sent_cookies: list[str] = []

    class _Sess:
        def __init__(self):
            self.hop = 0

        def get(self, url, headers=None, allow_redirects=True, **kwargs):
            sent_cookies.append((headers or {}).get("Cookie", ""))
            hop, self.hop = self.hop, self.hop + 1
            if hop == 0:
                return _FakeResp(
                    303,
                    headers={
                        "Location": "https://connect.pluxee.app/op/interaction/UID/consent",
                        # affinity cookie must be ignored; interaction cookie kept
                        "Set-Cookie": [
                            "ASLBSA=affinity; Path=/; HttpOnly",
                            "op_interaction=INT; Path=/op; HttpOnly",
                        ],
                    },
                )
            return _FakeResp(
                303,
                headers={
                    "Location": (
                        "https://consumers.pluxee.at/oidc/callback"
                        "?code=CODE1&state=STATE"
                    ),
                    "Set-Cookie": "op_session=ROLLED; Path=/op; Secure",
                },
            )

    client = PluxeeClient(
        session=_Sess(),
        refresh_token="RT",
        session_cookie="op_session=OLD; op_session.sig=SIG",
    )
    code = await client._async_follow_silent_authorize(
        "https://connect.pluxee.app/op/oidc/auth?x=1", "STATE"
    )
    assert code == "CODE1"
    # hop 1 must have replayed the interaction cookie set at hop 0...
    assert "op_interaction=INT" in sent_cookies[1]
    # ...but never the load-balancer affinity cookie.
    assert "ASLBSA" not in sent_cookies[1]
    # The persisted cookie tracks the rolled op_session but drops the transient
    # interaction cookie.
    persisted = client.token_state()["session_cookie"]
    assert parse_cookie_header(persisted).get("op_session") == "ROLLED"
    assert "op_interaction" not in persisted
