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
    assert food.state == "6.5"
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
