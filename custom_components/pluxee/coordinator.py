"""DataUpdateCoordinator for Pluxee."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from homeassistant.helpers.event import async_track_time_interval

from .api import PluxeeApiError, PluxeeAuthError, PluxeeClient, PluxeeData
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL_HOURS,
    CONF_SESSION_COOKIE,
    CONF_TOKEN_EXPIRES_AT,
    CONF_TX_LIMIT,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DEFAULT_TX_LIMIT,
    DOMAIN,
    MAX_CONSECUTIVE_AUTH_FAILURES,
    TOKEN_KEEPALIVE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class PluxeeCoordinator(DataUpdateCoordinator[PluxeeData]):
    """Coordinates fetching Pluxee card balances."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        # Snapshot of options so the update listener can tell an options change
        # (which needs a reload) from our own frequent token-data writes (which
        # must NOT reload - reloading mid-refresh churns the client and can
        # trigger refresh-token reuse revocation).
        self.options = dict(entry.options)
        hours = entry.options.get(
            CONF_SCAN_INTERVAL_HOURS,
            entry.data.get(CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS),
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=hours),
        )
        self._tx_limit = entry.options.get(CONF_TX_LIMIT, DEFAULT_TX_LIMIT)
        # Count consecutive auth failures so a transiently-wedged API doesn't get
        # mistaken for a dead session (see _async_handle_auth_error).
        self._consecutive_auth_failures = 0
        self.client = PluxeeClient(
            session=async_get_clientsession(hass),
            refresh_token=entry.data[CONF_REFRESH_TOKEN],
            access_token=entry.data.get(CONF_ACCESS_TOKEN),
            token_expires_at=entry.data.get(CONF_TOKEN_EXPIRES_AT),
            session_cookie=entry.data.get(CONF_SESSION_COOKIE),
            token_updated_cb=self._async_save_tokens,
        )
        if self.client.has_session_cookie:
            _LOGGER.info(
                "Pluxee session cookie is configured - silent re-authentication "
                "is enabled, so an expired refresh token will recover without a "
                "re-login prompt."
            )
        else:
            _LOGGER.warning(
                "Pluxee session cookie is NOT configured. The integration will "
                "have to ask you to re-authenticate when the session ends. Add the "
                "optional session cookie via the integration's Reconfigure dialog "
                "to avoid this."
            )

    @callback
    def async_start_keepalive(self) -> CALLBACK_TYPE:
        """Start the periodic token keep-alive. Returns an unsubscribe callback.

        The refresh token expires from inactivity if left unused between the
        (potentially many-hour) data polls. Refreshing it on a short cadence
        keeps the session alive indefinitely.
        """
        return async_track_time_interval(
            self.hass, self._async_keepalive, TOKEN_KEEPALIVE_INTERVAL
        )

    async def _async_keepalive(self, _now) -> None:
        """Refresh the token to keep the session warm (best effort)."""
        try:
            await self.client.async_keepalive()
        except PluxeeAuthError as err:
            # The session died despite keep-alive (e.g. an absolute session cap
            # or a server-side revocation). Trigger reauth proactively.
            _LOGGER.warning(
                "Pluxee token keep-alive failed, session needs re-auth: %s", err
            )
            self.entry.async_start_reauth(self.hass)
        except PluxeeApiError as err:
            # Transient (network / 5xx). The next tick or data poll will retry.
            _LOGGER.debug("Pluxee token keep-alive transient error: %s", err)

    async def _async_save_tokens(self, tokens: dict) -> None:
        """Persist rotated tokens (and any rolled session cookie) to the entry."""
        new_data = {
            **self.entry.data,
            CONF_ACCESS_TOKEN: tokens["access_token"],
            CONF_REFRESH_TOKEN: tokens["refresh_token"],
            CONF_TOKEN_EXPIRES_AT: tokens["token_expires_at"],
        }
        if tokens.get("session_cookie"):
            new_data[CONF_SESSION_COOKIE] = tokens["session_cookie"]
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    async def _async_update_data(self) -> PluxeeData:
        try:
            data = await self.client.async_get_data(tx_limit=self._tx_limit)
        except PluxeeAuthError as err:
            await self._async_handle_auth_error(err)  # always raises
            raise  # unreachable, keeps the type checker happy
        except PluxeeApiError as err:
            raise UpdateFailed(f"Error fetching Pluxee data: {err}") from err
        self._consecutive_auth_failures = 0
        return data

    async def _async_handle_auth_error(self, err: PluxeeAuthError) -> None:
        """Decide between a transient retry and a real re-auth prompt.

        The data API sometimes rejects a freshly-minted, valid token with 401/403
        while the underlying SSO session is still perfectly alive - e.g. a backend
        instance recycle, or a just-issued grant that has not yet propagated to
        the resource API. Forcing the user to re-login in that case is wrong: the
        session recovers on its own within a poll or two. So before prompting,
        probe the stored session cookie; while it still authorizes, treat the
        rejection as transient and retry on the next poll. Only give up (and
        prompt for re-auth) when the session is genuinely gone, or after several
        consecutive failures as a safety backstop. Always raises.
        """
        self._consecutive_auth_failures += 1
        if (
            self.client.has_session_cookie
            and self._consecutive_auth_failures < MAX_CONSECUTIVE_AUTH_FAILURES
        ):
            try:
                session_alive = await self.client.async_session_cookie_works()
            except PluxeeApiError:
                # Couldn't probe (transient infra noise) - assume alive and retry.
                session_alive = True
            if session_alive:
                _LOGGER.warning(
                    "Pluxee API rejected the token (%s), but the SSO session "
                    "cookie still authorizes - treating this as a transient "
                    "rejection (attempt %s/%s) and retrying on the next poll "
                    "instead of asking you to re-login.",
                    err,
                    self._consecutive_auth_failures,
                    MAX_CONSECUTIVE_AUTH_FAILURES,
                )
                raise UpdateFailed(
                    f"Pluxee API auth rejected but the session is still valid; "
                    f"will retry: {err}"
                ) from err

        _LOGGER.warning(
            "Pluxee authentication failed, re-auth required: %s. Providing the "
            "optional session cookie when re-authenticating lets the "
            "integration recover silently in future without this prompt.",
            err,
        )
        raise ConfigEntryAuthFailed(
            f"Pluxee session expired, please re-authenticate: {err}"
        ) from err
