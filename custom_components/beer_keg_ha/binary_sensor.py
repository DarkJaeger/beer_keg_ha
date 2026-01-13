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

# If total weight drops more than this, we consider it an active pour.
# 0.02 kg ≈ 0.705 oz
POUR_DROP_THRESHOLD_KG = 0.02

# Keep the indicator ON this long after last detected drop.
POUR_HOLD_SECONDS = 4.0


def _coerce_float(v: Any) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


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
        async_add_entities([KegPouringByWeightBinarySensor(hass, entry, keg_id)], True)
        created.add(keg_id)

    for keg_id in list(state.get("data", {}).keys()):
        create_for(keg_id)

    @callback
    def _on_update(event) -> None:
        keg_id = (event.data or {}).get("keg_id")
        if keg_id:
            create_for(keg_id)

    entry.async_on_unload(hass.bus.async_listen(PLATFORM_EVENT, _on_update))


class KegPouringByWeightBinarySensor(BinarySensorEntity):
    """ON when keg appears to be pouring based on weight dropping."""

    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, keg_id: str) -> None:
        self.hass = hass
        self.entry = entry
        self.keg_id = keg_id
        self._state_ref: Dict[str, Any] = hass.data[DOMAIN][entry.entry_id]

        short_id = keg_id[:4]
        self._attr_name = f"Keg {short_id} Pouring"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{keg_id}_pouring"

        self._is_on = False
        self._last_total_kg: float | None = None
        self._off_handle = None  # Cancelable timer handle

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
        return self._is_on

    @property
    def icon(self) -> str:
        # Filled dot when ON, outline when OFF
        return "mdi:circle" if self._is_on else "mdi:circle-outline"

    def _schedule_turn_off(self) -> None:
        if self._off_handle is not None:
            try:
                self._off_handle.cancel()
            except Exception:
                pass

        def _turn_off():
            self._off_handle = None
            if self._is_on:
                self._is_on = False
                self.async_write_ha_state()

        self._off_handle = self.hass.loop.call_later(POUR_HOLD_SECONDS, _turn_off)

    def _process_update(self) -> None:
        keg = self._state_ref.get("data", {}).get(self.keg_id, {})
        total = _coerce_float(keg.get("total_weight_kg"))

        if total is None:
            return

        if self._last_total_kg is None:
            self._last_total_kg = total
            return

        prev = self._last_total_kg
        self._last_total_kg = total

        delta = prev - total  # positive if weight dropped
        if delta >= POUR_DROP_THRESHOLD_KG:
            # Detected a pour-like drop
            if not self._is_on:
                self._is_on = True
                self.async_write_ha_state()
            # Keep ON for a bit after the last drop
            self._schedule_turn_off()

    async def async_added_to_hass(self) -> None:
        # Initialize from current data immediately
        self._process_update()

        self.async_on_remove(self.hass.bus.async_listen(PLATFORM_EVENT, self._refresh_if_mine))

    @callback
    def _refresh_if_mine(self, event) -> None:
        if (event.data or {}).get("keg_id") != self.keg_id:
            return
        self._process_update()
