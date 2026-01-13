from __future__ import annotations

import logging
from typing import Any, Dict, Set

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PLATFORM_EVENT

_LOGGER = logging.getLogger(__name__)


def _is_truthy(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "on", "yes", "y")
    return False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data[DOMAIN][entry.entry_id]
    created: Set[str] = state.setdefault("created_binary_kegs", set())

    def create_for(keg_id: str) -> None:
        if keg_id in created:
            return
        async_add_entities([KegPouringBinarySensor(entry, keg_id, state)], True)
        created.add(keg_id)

    for keg_id in list(state.get("data", {}).keys()):
        create_for(keg_id)

    @callback
    def _on_update(event) -> None:
        keg_id = (event.data or {}).get("keg_id")
        if keg_id:
            create_for(keg_id)

    entry.async_on_unload(hass.bus.async_listen(PLATFORM_EVENT, _on_update))


class KegPouringBinarySensor(BinarySensorEntity):
    """ON while keg is actively pouring (live WS field is_pouring)."""

    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, entry: ConfigEntry, keg_id: str, state_ref: Dict[str, Any]) -> None:
        self.entry = entry
        self.keg_id = keg_id
        self._state_ref = state_ref

        short_id = keg_id[:4]
        self._attr_name = f"Keg {short_id} Pouring"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{keg_id}_pouring"

    @property
    def device_info(self) -> DeviceInfo:
        short_id = self.keg_id[:4]
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_{self.keg_id}")},
            name=f"Beer Keg {short_id}",
            manufacturer="open-plaato-keg",
            model="API v2",
        )

    @property
    def is_on(self) -> bool:
        data: Dict[str, Dict[str, Any]] = self._state_ref.get("data", {})
        v = data.get(self.keg_id, {}).get("is_pouring")
        return _is_truthy(v)

    @property
    def icon(self) -> str:
        # Filled dot when ON, outline when OFF
        return "mdi:circle" if self.is_on else "mdi:circle-outline"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.hass.bus.async_listen(PLATFORM_EVENT, self._refresh_if_mine)
        )

    @callback
    def _refresh_if_mine(self, event) -> None:
        if (event.data or {}).get("keg_id") == self.keg_id:
            self.async_write_ha_state()
