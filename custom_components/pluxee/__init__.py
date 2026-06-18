"""The Pluxee integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import PluxeeCoordinator
from .frontend import async_register_frontend

PLATFORMS: list[Platform] = [Platform.SENSOR]

type PluxeeConfigEntry = ConfigEntry[PluxeeCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: PluxeeConfigEntry) -> bool:
    """Set up Pluxee from a config entry."""
    await async_register_frontend(hass)

    coordinator = PluxeeCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Keep the (session-bound) refresh token warm between data polls so it never
    # expires from inactivity and forces a re-authentication.
    entry.async_on_unload(coordinator.async_start_keepalive())
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PluxeeConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: PluxeeConfigEntry) -> None:
    """Reload entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
