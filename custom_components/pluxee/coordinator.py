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
    TOKEN_KEEPALIVE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class PluxeeCoordinator(DataUpdateCoordinator[PluxeeData]):
    """Coordinates fetching Pluxee card balances."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
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
            return await self.client.async_get_data(tx_limit=self._tx_limit)
        except PluxeeAuthError as err:
            _LOGGER.warning(
                "Pluxee authentication failed, re-auth required: %s. Providing the "
                "optional session cookie when re-authenticating lets the "
                "integration recover silently in future without this prompt.",
                err,
            )
            raise ConfigEntryAuthFailed(
                f"Pluxee session expired, please re-authenticate: {err}"
            ) from err
        except PluxeeApiError as err:
            raise UpdateFailed(f"Error fetching Pluxee data: {err}") from err
