"""Tests for Pluxee frontend registration."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.pluxee.frontend import (
    CARD_URL,
    _async_register_lovelace_resource,
    _card_url,
)


class FakeResources:
    """Minimal Lovelace resource collection."""

    def __init__(self, items=None):
        self.items = items or []
        self.created = None
        self.updated = None

    async def async_get_info(self):
        """Pretend the lazy storage collection is loaded."""
        return {"resources": len(self.items)}

    def async_items(self):
        """Return stored resources."""
        return self.items

    async def async_create_item(self, data):
        """Capture a resource creation."""
        self.created = data

    async def async_update_item(self, item_id, data):
        """Capture a resource update."""
        self.updated = (item_id, data)


def test_card_url_uses_content_hash(tmp_path):
    """The browser cache key follows the actual bundle contents."""
    card = tmp_path / "pluxee-card.js"
    card.write_text("first", encoding="utf-8")
    first = _card_url(str(card))
    card.write_text("second", encoding="utf-8")
    second = _card_url(str(card))

    assert first.startswith(f"{CARD_URL}?v=")
    assert first != second


@pytest.mark.asyncio
async def test_registers_storage_resource():
    """A storage-mode dashboard gets a persistent module resource."""
    resources = FakeResources()
    hass = SimpleNamespace(
        data={"lovelace": {"mode": "storage", "resources": resources}}
    )

    await _async_register_lovelace_resource(hass, f"{CARD_URL}?v=new")

    assert resources.created == {
        "res_type": "module",
        "url": f"{CARD_URL}?v=new",
    }


@pytest.mark.asyncio
async def test_updates_stale_storage_resource():
    """An existing resource receives the new content-hashed URL."""
    resources = FakeResources(
        [{"id": "pluxee", "type": "module", "url": f"{CARD_URL}?v=old"}]
    )
    hass = SimpleNamespace(
        data={
            "lovelace": SimpleNamespace(
                resource_mode="storage",
                resources=resources,
            )
        }
    )

    await _async_register_lovelace_resource(hass, f"{CARD_URL}?v=new")

    assert resources.created is None
    assert resources.updated == (
        "pluxee",
        {"res_type": "module", "url": f"{CARD_URL}?v=new"},
    )


@pytest.mark.asyncio
async def test_skips_yaml_resources():
    """YAML resource mode remains user-owned and uses the module fallback."""
    resources = FakeResources()
    hass = SimpleNamespace(
        data={"lovelace": {"mode": "yaml", "resources": resources}}
    )

    await _async_register_lovelace_resource(hass, f"{CARD_URL}?v=new")

    assert resources.created is None
    assert resources.updated is None
