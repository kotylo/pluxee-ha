"""Pytest config: make custom_components importable and enable custom integrations.

On Windows the asyncio event-loop self-pipe needs an AF_INET socket, which
pytest-homeassistant-custom-component blocks (it only allows AF_UNIX). All real
HTTP is intercepted by aioclient_mock, so we re-enable sockets after phcc's
setup hook runs. We also force the selector event loop policy.
"""
import asyncio
import os
import sys

import pytest
import pytest_socket

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Warm up the pycares (aiodns) shutdown daemon thread before any test captures
# its "threads before" snapshot, so phcc's cleanup check does not flag it.
try:  # pragma: no cover - environment dependent
    import pycares

    pycares._shutdown_manager.start()
except Exception:  # noqa: BLE001
    pass


@pytest.hookimpl(trylast=True)
def pytest_runtest_setup():
    """Undo phcc's socket blocking on Windows (Linux self-pipe uses AF_UNIX)."""
    if sys.platform == "win32":
        pytest_socket.enable_socket()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    yield


@pytest.fixture(autouse=True)
async def _unload_entries(hass):
    """Unload any config entries before phcc's lingering-timer check runs."""
    yield
    from homeassistant.config_entries import ConfigEntryState

    from custom_components.pluxee.const import DOMAIN

    await hass.async_block_till_done()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
