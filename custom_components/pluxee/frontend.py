"""Auto-register and serve the Pluxee Lovelace card.

Installing the integration makes the custom "Pluxee Card" available on
dashboards with no manual resource setup: we serve the bundled JS via a static
path and load it as a frontend module.
"""
from __future__ import annotations

import logging
import os

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_URL = "/pluxee/pluxee-card.js"
CARD_VERSION = "0.2.3"
_REGISTERED = "_frontend_registered"


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the card and add it as an extra JS module (once per HA instance)."""
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
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, card_path, cache_headers=False)]
        )

        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, f"{CARD_URL}?v={CARD_VERSION}")
        domain_data[_REGISTERED] = True
        _LOGGER.debug("Registered Pluxee dashboard card at %s", CARD_URL)
    except Exception as err:  # noqa: BLE001 - never block setup over a UI nicety
        _LOGGER.warning(
            "Could not auto-register the Pluxee dashboard card "
            "(you can still add it manually): %s",
            err,
        )
