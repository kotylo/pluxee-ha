"""Config flow for Pluxee."""
from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from .api import (
    PluxeeAuthError,
    PluxeeClient,
    PluxeeError,
    async_exchange_code,
    build_authorize_url,
    extract_code,
    generate_pkce,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CIAM_ID,
    CONF_EMAIL,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL_HOURS,
    CONF_SESSION_COOKIE,
    CONF_TOKEN_EXPIRES_AT,
    CONF_TX_LIMIT,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DEFAULT_TX_LIMIT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CONF_CALLBACK_URL = "callback_url"

_STEP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CALLBACK_URL): str,
        # Multiline so a pasted DevTools cookie table (multiple lines) survives
        # intact instead of being collapsed to a single line.
        vol.Optional(CONF_SESSION_COOKIE, default=""): TextSelector(
            TextSelectorConfig(multiline=True)
        ),
    }
)


def _decode_jwt_email(id_token: str | None) -> str | None:
    """Best-effort extraction of the email claim from an id_token (no verify)."""
    if not id_token:
        return None
    try:
        payload_b64 = id_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("email")
    except Exception:  # noqa: BLE001 - best effort only
        return None


class PluxeeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Pluxee config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._verifier: str | None = None
        self._state: str | None = None
        self._auth_url: str | None = None
        self._reauth_entry: ConfigEntry | None = None

    def _new_auth_url(self) -> str:
        self._verifier, challenge = generate_pkce()
        self._state = secrets.token_urlsafe(16)
        nonce = secrets.token_urlsafe(16)
        self._auth_url = build_authorize_url(challenge, self._state, nonce)
        return self._auth_url

    async def _async_validate(
        self, callback_url: str, session_cookie: str = ""
    ) -> tuple[dict, dict]:
        """Exchange the pasted code and fetch account data.

        Returns (token_state, info). token_state already reflects any rotation
        and carries the (optional) session cookie for later silent re-auth.
        """
        code = extract_code(callback_url)
        if not code:
            raise PluxeeError("no_code")
        session = async_get_clientsession(self.hass)

        # Validate the optional session cookie FIRST (before consuming the
        # single-use code) so a wrong-format/expired cookie gives immediate,
        # specific feedback instead of silently failing hours later.
        cookie = (session_cookie or "").strip() or None
        if cookie:
            probe = PluxeeClient(session=session, refresh_token="", session_cookie=cookie)
            try:
                works = await probe.async_session_cookie_works()
            except PluxeeError:
                works = True  # network hiccup - don't block setup on validation
            if not works:
                raise PluxeeError("session_cookie_invalid")

        tokens = await async_exchange_code(session, code, self._verifier or "")
        # Build a client with a valid expiry so it does NOT refresh (which would
        # rotate/consume the just-issued refresh token) during validation.
        client = PluxeeClient(
            session=session,
            refresh_token=tokens["refresh_token"],
            access_token=tokens["access_token"],
            token_expires_at=time.time() + int(tokens.get("expires_in", 1800)),
            session_cookie=cookie,
        )
        data = await client.async_get_data(tx_limit=0)
        info = {
            "ciam_id": data.ciam_id,
            "email": _decode_jwt_email(tokens.get("id_token")),
            "num_cards": len(data.cards),
        }
        return client.token_state(), info

    def _entry_data(self, token_state: dict, info: dict) -> dict:
        data = {
            CONF_REFRESH_TOKEN: token_state["refresh_token"],
            CONF_ACCESS_TOKEN: token_state["access_token"],
            CONF_TOKEN_EXPIRES_AT: token_state["token_expires_at"],
            CONF_CIAM_ID: info["ciam_id"],
            CONF_EMAIL: info["email"],
        }
        if token_state.get("session_cookie"):
            data[CONF_SESSION_COOKIE] = token_state["session_cookie"]
        return data

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                tokens, info = await self._async_validate(
                    user_input[CONF_CALLBACK_URL],
                    user_input.get(CONF_SESSION_COOKIE, ""),
                )
            except PluxeeAuthError:
                errors["base"] = "invalid_auth"
            except PluxeeError as err:
                errors["base"] = (
                    str(err)
                    if str(err) in ("no_code", "session_cookie_invalid")
                    else "cannot_connect"
                )
                _LOGGER.debug("Validation failed: %s", err)
            else:
                await self.async_set_unique_id(info["ciam_id"])
                self._abort_if_unique_id_configured()
                title = info["email"] or "Pluxee"
                return self.async_create_entry(
                    title=title, data=self._entry_data(tokens, info)
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_STEP_SCHEMA,
            description_placeholders={"auth_url": self._auth_url or self._new_auth_url()},
            errors=errors,
        )

    # ---------------------------- reauth ---------------------------------- #
    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        assert self._reauth_entry is not None
        if user_input is not None:
            try:
                tokens, info = await self._async_validate(
                    user_input[CONF_CALLBACK_URL],
                    user_input.get(CONF_SESSION_COOKIE, ""),
                )
            except PluxeeAuthError:
                errors["base"] = "invalid_auth"
            except PluxeeError as err:
                errors["base"] = (
                    str(err)
                    if str(err) in ("no_code", "session_cookie_invalid")
                    else "cannot_connect"
                )
            else:
                await self.async_set_unique_id(info["ciam_id"])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    self._reauth_entry,
                    data={**self._reauth_entry.data, **self._entry_data(tokens, info)},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_STEP_SCHEMA,
            description_placeholders={"auth_url": self._auth_url or self._new_auth_url()},
            errors=errors,
        )

    # ------------------------- reconfigure -------------------------------- #
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            try:
                tokens, info = await self._async_validate(
                    user_input[CONF_CALLBACK_URL],
                    user_input.get(CONF_SESSION_COOKIE, ""),
                )
            except PluxeeAuthError:
                errors["base"] = "invalid_auth"
            except PluxeeError as err:
                errors["base"] = (
                    str(err)
                    if str(err) in ("no_code", "session_cookie_invalid")
                    else "cannot_connect"
                )
            else:
                await self.async_set_unique_id(info["ciam_id"])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, **self._entry_data(tokens, info)},
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_STEP_SCHEMA,
            description_placeholders={"auth_url": self._auth_url or self._new_auth_url()},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PluxeeOptionsFlow()


class PluxeeOptionsFlow(OptionsFlow):
    """Handle Pluxee options (scan interval)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL_HOURS,
                        default=opts.get(
                            CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)),
                    vol.Required(
                        CONF_TX_LIMIT,
                        default=opts.get(CONF_TX_LIMIT, DEFAULT_TX_LIMIT),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=50)),
                }
            ),
        )
