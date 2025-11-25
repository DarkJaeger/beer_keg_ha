from __future__ import annotations

import logging
from typing import Any, Dict, List, Set

from homeassistant.components.number import (
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORM_EVENT = f"{DOMAIN}_update"

#
# Per-keg number types
#
# These become entities like:
#   number.keg_d00d_full_weight_kg
#   number.keg_d00d_weight_calibrate
#   number.keg_d00d_temp_calibrate_degc
#
NUMBER_TYPES: Dict[str, Dict[str, Any]] = {
    "full_weight_kg": {
        "name": "Full Weight (kg)",
        "key": "full_weight",
        "min": 0.0,
        "max": 100.0,
        "step": 0.01,
        "mode": NumberMode.BOX,
    },
    "weight_calibrate": {
        "name": "Weight Calibrate",
        "key": "weight_calibrate",
        "min": -10.0,
        "max": 10.0,
        "step": 0.01,
        "mode": NumberMode.BOX,
    },
    "temp_calibrate_degc": {
        "name": "Temp Calibrate (°C)",
        "key": "temperature_calibrate",
        "min": -10.0,
        "max": 10.0,
        "step": 0.1,
        "mode": NumberMode.BOX,
    },
    # Optional: per-keg Beer SG (current gravity)
    "beer_sg": {
        "name": "Beer SG",
        "key": "beer_sg",
        "min": 0.9,
        "max": 1.2,
        "step": 0.001,
        "mode": NumberMode.BOX,
    },
    # Optional: per-keg Original Gravity (OG)
    "original_gravity": {
        "name": "Original Gravity",
        "key": "original_gravity",
        "min": 0.9,
        "max": 1.2,
        "step": 0.001,
        "mode": NumberMode.BOX,
    },
}

#
# Global smoothing controls (one per integration entry, not per keg)
#
GLOBAL_NUMBER_TYPES: Dict[str, Dict[str, Any]] = {
    "noise_deadband_kg": {
        "name": "Beer Keg Noise Deadband (kg)",
        "state_key": "noise_deadband_kg",
        "min": 0.0,
        "max": 0.5,
        "step": 0.01,
        "mode": NumberMode.SLIDER,
        "default": 0.00,
    },
    "smoothing_alpha": {
        "name": "Beer Keg Smoothing Alpha",
        "state_key": "smoothing_alpha",
        "min": 0.05,
        "max": 1.0,
        "step": 0.05,
        "mode": NumberMode.SLIDER,
        "default": 0.1,
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up keg calibration + smoothing numbers from a config entry."""
    state = hass.data[DOMAIN][entry.entry_id]

    entities: List[NumberEntity] = []

    #
    # 1) Global smoothing sliders (once per integration entry)
    #
    state.setdefault(
        "noise_deadband_kg",
        GLOBAL_NUMBER_TYPES["noise_deadband_kg"]["default"],
    )
    state.setdefault(
        "smoothing_alpha",
        GLOBAL_NUMBER_TYPES["smoothing_alpha"]["default"],
    )

    for key, meta in GLOBAL_NUMBER_TYPES.items():
        entities.append(BeerKegGlobalNumberEntity(hass, entry, key, meta))

    #
    # 2) Per-keg calibration numbers
    #
    created: Set[str] = state.setdefault("created_number_kegs", set())

    def create_for(keg_id: str) -> None:
        """Create all number entities for one keg_id (once)."""
        if keg_id in created:
            return

        for num_type in NUMBER_TYPES.keys():
            entities.append(BeerKegNumberEntity(hass, entry, keg_id, num_type))

        created.add(keg_id)

    # Create numbers for any kegs we already know about
    for keg_id in list(state.get("data", {}).keys()):
        create_for(keg_id)

    # Add initial batch (global + existing kegs)
    async_add_entities(entities, True)

    @callback
    def _on_update(event) -> None:
        """Create entities for new kegs when they appear."""
        keg_id = (event.data or {}).get("keg_id")
        if not keg_id or keg_id in created:
            return

        new_entities: List[NumberEntity] = []
        for num_type in NUMBER_TYPES.keys():
            new_entities.append(BeerKegNumberEntity(hass, entry, keg_id, num_type))
        created.add(keg_id)
        async_add_entities(new_entities, True)

    entry.async_on_unload(
        hass.bus.async_listen(PLATFORM_EVENT, _on_update)
    )


class BeerKegGlobalNumberEntity(NumberEntity):
    """Global number entities for smoothing settings."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        key: str,
        meta: Dict[str, Any],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._state_key = meta["state_key"]
        self._default = meta["default"]

        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_global_{key}"
        self._attr_name = meta["name"]
        self._attr_mode = meta["mode"]
        self._attr_native_min_value = meta["min"]
        self._attr_native_max_value = meta["max"]
        self._attr_native_step = meta["step"]

    @property
    def device_info(self) -> DeviceInfo:
        """Group these under a 'Beer Keg Settings' device."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_settings")},
            name="Beer Keg Settings",
            manufacturer="Beer Keg",
            model="WebSocket + REST",
        )

    @property
    def native_value(self) -> float | None:
        domain_state = self.hass.data[DOMAIN][self.entry.entry_id]
        val = domain_state.get(self._state_key, self._default)
        try:
            return float(val)
        except (TypeError, ValueError):
            return self._default

    async def async_set_native_value(self, value: float) -> None:
        domain_state = self.hass.data[DOMAIN][self.entry.entry_id]
        domain_state[self._state_key] = float(value)

        # Nudge all kegs so sensors recalc with new smoothing
        for keg_id in list(domain_state.get("data", {}).keys()):
            self.hass.bus.async_fire(
                PLATFORM_EVENT,
                {"keg_id": keg_id},
            )

        self.async_write_ha_state()


class BeerKegNumberEntity(NumberEntity):
    """Number entity representing calibration/config values per keg."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        keg_id: str,
        num_type: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.keg_id = keg_id
        self.num_type = num_type

        meta = NUMBER_TYPES[num_type]
        self._key = meta["key"]

        short_id = keg_id[:4]  # cosmetic short ID in names

        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{keg_id}_{num_type}"
        self._attr_name = f"Keg {short_id} {meta['name']}"
        self._attr_mode = meta["mode"]
        self._attr_native_min_value = meta["min"]
        self._attr_native_max_value = meta["max"]
        self._attr_native_step = meta["step"]

        # Units:
        #  - full_weight / weight_calibrate: kg
        #  - temperature_calibrate: °C
        #  - beer_sg / original_gravity: unitless
        if self._key in ("full_weight", "weight_calibrate"):
            self._attr_native_unit_of_measurement = "kg"
        elif self._key == "temperature_calibrate":
            self._attr_native_unit_of_measurement = "°C"
        else:
            self._attr_native_unit_of_measurement = None

    @property
    def device_info(self) -> DeviceInfo:
        """Attach these numbers to the keg device."""
        short_id = self.keg_id[:4]
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_{self.keg_id}")},
            name=f"Beer Keg {short_id}",
            manufacturer="Beer Keg",
            model="WebSocket + REST",
        )

    @property
    def native_value(self) -> float | None:
        """Return the current value from integration state."""
        domain_state = self.hass.data[DOMAIN][self.entry.entry_id]
        data: Dict[str, Dict[str, Any]] = domain_state.get("data", {})
        keg = data.get(self.keg_id, {})
        val = keg.get(self._key)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the value in integration state (does NOT call REST directly)."""
        domain_state = self.hass.data[DOMAIN][self.entry.entry_id]
        data: Dict[str, Dict[str, Any]] = domain_state.setdefault("data", {})
        keg = data.setdefault(self.keg_id, {})
        keg[self._key] = float(value)

        # Let any listening sensors/cards update
        self.hass.bus.async_fire(
            PLATFORM_EVENT,
            {"keg_id": self.keg_id},
        )
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Listen for integration updates and refresh."""

        @callback
        def _handle_update(event) -> None:
            if (event.data or {}).get("keg_id") == self.keg_id:
                self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(PLATFORM_EVENT, _handle_update)
        )
