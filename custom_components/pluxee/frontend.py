"""Auto-register and serve the Pluxee Lovelace card.

Installing the integration makes the custom "Pluxee Card" available on
dashboards with no manual resource setup: we serve the bundled JS via a static
path and load it as both a Lovelace resource and a frontend module.
"""
from __future__ import annotations

from hashlib import sha256
import logging
import os
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_URL = "/pluxee/pluxee-card.js"
_REGISTERED = "_frontend_registered"


def _card_url(card_path: str) -> str:
    """Return a URL whose cache key changes whenever the bundled card changes."""
    digest = sha256(Path(card_path).read_bytes()).hexdigest()[:12]
    return f"{CARD_URL}?v={digest}"


async def _async_register_lovelace_resource(
    hass: HomeAssistant, card_url: str
) -> None:
    """Persist the card as a Lovelace resource in storage resource mode.

    Extra frontend modules are announced through both the initial HTML and a
    websocket subscription. A client connecting between those two can miss a
    module added during config-entry setup. Lovelace resources are loaded as
    part of dashboard setup, so keeping the card there avoids that race.
    """
    from homeassistant.components.lovelace.const import (
        CONF_RESOURCE_TYPE_WS,
        DOMAIN as LOVELACE_DOMAIN,
        MODE_STORAGE,
    )
    from homeassistant.const import CONF_ID, CONF_TYPE, CONF_URL

    lovelace = hass.data.get(LOVELACE_DOMAIN)
    if lovelace is None:
        return

    # HA 2024.12-2025.x stores Lovelace data in a dict; newer HA releases use
    # a LovelaceData dataclass and split dashboard mode from resource mode.
    if isinstance(lovelace, dict):
        resource_mode = lovelace.get("resource_mode", lovelace.get("mode"))
        resources = lovelace.get("resources")
    else:
        resource_mode = lovelace.resource_mode
        resources = lovelace.resources
    if resource_mode != MODE_STORAGE or resources is None:
        return

    # ResourceStorageCollection loads lazily.
    await resources.async_get_info()
    existing = next(
        (
            item
            for item in resources.async_items()
            if item.get(CONF_URL, "").partition("?")[0] == CARD_URL
        ),
        None,
    )
    if existing is None:
        await resources.async_create_item(
            {CONF_RESOURCE_TYPE_WS: "module", CONF_URL: card_url}
        )
    elif existing.get(CONF_URL) != card_url or existing.get(CONF_TYPE) != "module":
        await resources.async_update_item(
            existing[CONF_ID],
            {CONF_RESOURCE_TYPE_WS: "module", CONF_URL: card_url},
        )


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Serve and register the card once per HA instance."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_REGISTERED):
        return

    # Both are part of default_config in any normal HA install; skip cleanly if
    # absent (e.g. in the bare test harness) instead of failing setup.
    if "http" not in hass.config.components or "frontend" not in hass.config.components:
        _LOGGER.debug("http/frontend not available; skipping card auto-registration")
        return

    try:
        from homeassistant.components.http import StaticPathConfig

        card_path = os.path.join(os.path.dirname(__file__), "frontend", "pluxee-card.js")
        card_url = await hass.async_add_executor_job(_card_url, card_path)
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, card_path, cache_headers=False)]
        )

        from homeassistant.components.frontend import add_extra_js_url

        # Keep the global module registration for YAML-resource dashboards and
        # pages loaded before Lovelace starts.
        add_extra_js_url(hass, card_url)
        try:
            await _async_register_lovelace_resource(hass, card_url)
        except Exception as err:  # noqa: BLE001 - extra-module fallback remains usable
            _LOGGER.warning("Could not register Pluxee as a Lovelace resource: %s", err)

        domain_data[_REGISTERED] = True
        _LOGGER.debug("Registered Pluxee dashboard card at %s", card_url)
    except Exception as err:  # noqa: BLE001 - never block setup over a UI nicety
        _LOGGER.warning(
            "Could not auto-register the Pluxee dashboard card "
            "(you can still add it manually): %s",
            err,
        )
