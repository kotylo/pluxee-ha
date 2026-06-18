"""Pluxee balance and transaction sensors."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PluxeeConfigEntry
from .api import Card
from .const import DOMAIN
from .coordinator import PluxeeCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PluxeeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Pluxee sensors from a config entry."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for card in coordinator.data.cards:
        entities.append(PluxeeCardBalanceSensor(coordinator, card.unique_card_id))
        entities.append(PluxeeLastTransactionSensor(coordinator, card.unique_card_id))
    async_add_entities(entities)


class PluxeeCardEntity(CoordinatorEntity[PluxeeCoordinator], SensorEntity):
    """Base entity tied to a single Pluxee card (device)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PluxeeCoordinator, card_id: str) -> None:
        super().__init__(coordinator)
        self._card_id = card_id
        card = self._card
        product = card.product_name if card else "Card"
        last4 = card.last4 if card else None
        name = f"{product} ••{last4}" if last4 else product
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, card_id)},
            manufacturer="Pluxee",
            name=name,
            model=card.product_name if card else None,
        )

    @property
    def _card(self) -> Card | None:
        for card in self.coordinator.data.cards:
            if card.unique_card_id == self._card_id:
                return card
        return None

    @property
    def available(self) -> bool:
        return super().available and self._card is not None


class PluxeeCardBalanceSensor(PluxeeCardEntity):
    """Balance of a single Pluxee card."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_translation_key = "balance"

    def __init__(self, coordinator: PluxeeCoordinator, card_id: str) -> None:
        super().__init__(coordinator, card_id)
        self._attr_unique_id = f"{card_id}_balance"

    @property
    def native_value(self) -> float | None:
        card = self._card
        return card.balance if card else None

    @property
    def native_unit_of_measurement(self) -> str:
        card = self._card
        return card.currency if card else "EUR"

    @property
    def extra_state_attributes(self) -> dict:
        card = self._card
        if not card:
            return {}
        return {
            "product_code": card.product_code,
            "product_name": card.product_name,
            "card_status": card.status,
            "masked_pan": card.masked_pan,
            "last4": card.last4,
            "name_on_card": card.name_on_card,
            "expiry_date": card.expiry_date,
            "preferred": card.preferred,
            "wallets": [
                {
                    "type": w.wallet_type,
                    "balance": w.balance,
                    "currency": w.currency,
                    "status": w.status,
                }
                for w in card.wallets
            ],
        }


class PluxeeLastTransactionSensor(PluxeeCardEntity):
    """Most recent transaction of a card, with recent history in attributes."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_translation_key = "last_transaction"
    # The full transaction list would bloat the recorder DB; keep it out of history.
    _unrecorded_attributes = frozenset({"transactions"})

    def __init__(self, coordinator: PluxeeCoordinator, card_id: str) -> None:
        super().__init__(coordinator, card_id)
        self._attr_unique_id = f"{card_id}_last_transaction"

    @property
    def _transactions(self) -> list:
        return self.coordinator.data.transactions.get(self._card_id, [])

    @property
    def native_value(self) -> float | None:
        txs = self._transactions
        return txs[0].amount if txs else None

    @property
    def native_unit_of_measurement(self) -> str:
        txs = self._transactions
        return txs[0].currency if txs else "EUR"

    @property
    def extra_state_attributes(self) -> dict:
        txs = self._transactions
        if not txs:
            return {"transactions": []}
        latest = txs[0]
        return {
            "last_merchant": latest.merchant,
            "last_date": latest.date,
            "last_description": latest.description,
            "transactions": [t.as_dict() for t in txs],
        }
