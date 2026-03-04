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

import aiohttp

from .const import DOMAIN, PLATFORM_EVENT, AIRLOCK_EVENT

_LOGGER = logging.getLogger(__name__)

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
        "default": 0.0,   # match __init__.py default
    },
    "smoothing_alpha": {
        "name": "Beer Keg Smoothing Alpha",
        "state_key": "smoothing_alpha",
        "min": 0.05,
        "max": 1.0,
        "step": 0.05,
        "mode": NumberMode.SLIDER,
        "default": 1.0,   # match __init__.py default (no smoothing)
    },
}

#
# Tap-level numbers (ABV, IBU, etc.) backed by state["tap_numbers"]
#
# These become entities like:
#   number.tap_1_abv
#   number.tap_1_ibu
#
TAP_NUMBER_TYPES: Dict[str, Dict[str, Any]] = {
    "abv": {
        "name": "ABV (%)",
        "min": 0.0,
        "max": 20.0,
        "step": 0.1,
        "mode": NumberMode.BOX,
        "unit": "%",
    },
    "ibu": {
        "name": "IBU",
        "min": 0.0,
        "max": 200.0,
        "step": 1.0,
        "mode": NumberMode.BOX,
        "unit": None,
    },
    # You can add more here later, e.g. "og", "fg", etc.
}

#
# Airlock-level numbers (SG per integration)
#
AIRLOCK_NUMBER_TYPES: Dict[str, Dict[str, Any]] = {
    "grainfather_sg": {
        "name": "Grainfather SG",
        "key": "grainfather_sg",
        "endpoint": "grainfather",
        "server_param": "specific_gravity",
        "min": 0.9,
        "max": 1.2,
        "step": 0.001,
        "mode": NumberMode.BOX,
    },
    "brewfather_sg": {
        "name": "Brewfather SG",
        "key": "brewfather_sg",
        "endpoint": "brewfather",
        "server_param": "specific_gravity",
        "min": 0.9,
        "max": 1.2,
        "step": 0.001,
        "mode": NumberMode.BOX,
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up keg calibration + smoothing numbers + tap numbers from a config entry."""
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
    created_kegs: Set[str] = state.setdefault("created_number_kegs", set())

    def create_for_keg(keg_id: str) -> None:
        """Create all number entities for one keg_id (once)."""
        if keg_id in created_kegs:
            return

        for num_type in NUMBER_TYPES.keys():
            entities.append(BeerKegNumberEntity(hass, entry, keg_id, num_type))

        created_kegs.add(keg_id)

    # Create numbers for any kegs we already know about
    for keg_id in list(state.get("data", {}).keys()):
        create_for_keg(keg_id)

    #
    # 3) Tap-level numbers (ABV/IBU etc.)
    #
    tap_entities: List[NumberEntity] = []

    tap_keys: Set[str] = set()
    tap_text = state.get("tap_text") or {}
    tap_numbers_state = state.get("tap_numbers") or {}

    # Discover tap IDs from existing tap_text / tap_numbers, e.g. "tap_1", "tap_2"
    for key in tap_text.keys():
        if isinstance(key, str) and key.startswith("tap_"):
            tap_keys.add(key)
    for key in tap_numbers_state.keys():
        if isinstance(key, str) and key.startswith("tap_"):
            tap_keys.add(key)

    # If nothing yet, assume tap_1..tap_4 as a friendly default
    if not tap_keys:
        tap_keys = {f"tap_{i}" for i in range(1, 5)}

    for tap_key in sorted(tap_keys):
        for field_key, meta in TAP_NUMBER_TYPES.items():
            tap_entities.append(
                BeerKegTapNumberEntity(hass, entry, tap_key, field_key, meta)
            )

    entities.extend(tap_entities)

    # Add initial batch (global + existing kegs + taps)
    async_add_entities(entities, True)

    @callback
    def _on_update(event) -> None:
        """Create entities for new kegs when they appear."""
        keg_id = (event.data or {}).get("keg_id")
        if not keg_id or keg_id in created_kegs:
            return

        new_entities: List[NumberEntity] = []
        for num_type in NUMBER_TYPES.keys():
            new_entities.append(BeerKegNumberEntity(hass, entry, keg_id, num_type))
        created_kegs.add(keg_id)
        async_add_entities(new_entities, True)

    entry.async_on_unload(
        hass.bus.async_listen(PLATFORM_EVENT, _on_update)
    )

    #
    # 4) Airlock numbers (SG per integration)
    #
    created_airlocks: Set[str] = state.setdefault("created_airlocks_number", set())

    def create_airlock_numbers_for(airlock_id: str) -> None:
        if airlock_id in created_airlocks:
            return
        new_ents: List[NumberEntity] = [
            AirlockNumberEntity(hass, entry, airlock_id, num_type)
            for num_type in AIRLOCK_NUMBER_TYPES.keys()
        ]
        created_airlocks.add(airlock_id)
        async_add_entities(new_ents, True)

    for airlock_id in list(state.get("airlock_data", {}).keys()):
        create_airlock_numbers_for(airlock_id)

    @callback
    def _on_airlock_update(event) -> None:
        airlock_id = (event.data or {}).get("airlock_id")
        if airlock_id:
            create_airlock_numbers_for(airlock_id)

    entry.async_on_unload(
        hass.bus.async_listen(AIRLOCK_EVENT, _on_airlock_update)
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
        """Update the smoothing value in integration state and persist it."""
        domain_state = self.hass.data[DOMAIN][self.entry.entry_id]
        domain_state[self._state_key] = float(value)

        prefs_store = domain_state.get("prefs_store")
        if prefs_store is not None:
            await prefs_store.async_save(
                {
                    "display_units": domain_state.get("display_units", {}),
                    "keg_config": domain_state.get("keg_config", {}),
                    "tap_text": domain_state.get("tap_text", {}),
                    "tap_numbers": domain_state.get("tap_numbers", {}),
                    "noise_deadband_kg": domain_state.get("noise_deadband_kg"),
                    "smoothing_alpha": domain_state.get("smoothing_alpha"),
                }
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


class BeerKegTapNumberEntity(NumberEntity):
    """Tap-level number entity (ABV, IBU, etc.) stored in state['tap_numbers']."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tap_key: str,       # e.g. "tap_1"
        field_key: str,     # e.g. "abv", "ibu"
        meta: Dict[str, Any],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.tap_key = tap_key
        self.field_key = field_key
        self._meta = meta

        # Extract numeric part for display ("Tap 1 ABV (%)")
        try:
            tap_index = tap_key.split("_", 1)[1]
        except Exception:
            tap_index = tap_key

        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{tap_key}_{field_key}"
        self._attr_name = f"Tap {tap_index} {meta['name']}"
        self._attr_mode = meta["mode"]
        self._attr_native_min_value = meta["min"]
        self._attr_native_max_value = meta["max"]
        self._attr_native_step = meta["step"]
        self._attr_native_unit_of_measurement = meta.get("unit")

        # Force specific entity_id so the Lovelace card can reference
        # number.tap_1_abv, number.tap_1_ibu, etc.
        self.entity_id = f"number.{tap_key}_{field_key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Group tap numbers under a 'Beer Keg Settings' device."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_settings")},
            name="Beer Keg Settings",
            manufacturer="Beer Keg",
            model="WebSocket + REST",
        )

    @property
    def native_value(self) -> float | None:
        """Return the current value from tap_numbers state."""
        domain_state = self.hass.data[DOMAIN][self.entry.entry_id]
        tap_numbers: Dict[str, Dict[str, Any]] = domain_state.get("tap_numbers", {})
        per_tap = tap_numbers.get(self.tap_key, {})
        val = per_tap.get(self.field_key)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Update tap_numbers and persist to prefs."""
        domain_state = self.hass.data[DOMAIN][self.entry.entry_id]
        tap_numbers: Dict[str, Dict[str, Any]] = domain_state.setdefault("tap_numbers", {})
        per_tap = tap_numbers.setdefault(self.tap_key, {})
        per_tap[self.field_key] = float(value)

        # Persist along with other prefs (tap_text, smoothing, etc.)
        prefs_store = domain_state.get("prefs_store")
        if prefs_store is not None:
            await prefs_store.async_save(
                {
                    "display_units": domain_state.get("display_units", {}),
                    "keg_config": domain_state.get("keg_config", {}),
                    "tap_text": domain_state.get("tap_text", {}),
                    "tap_numbers": tap_numbers,
                    "noise_deadband_kg": domain_state.get("noise_deadband_kg"),
                    "smoothing_alpha": domain_state.get("smoothing_alpha"),
                }
            )

        # No specific keg_id to nudge; UI (cards) read this entity directly
        self.async_write_ha_state()


class AirlockNumberEntity(NumberEntity):
    """Number entity for airlock-level SG (Grainfather or Brewfather)."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        airlock_id: str,
        num_type: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.airlock_id = airlock_id
        self.num_type = num_type
        self._meta = AIRLOCK_NUMBER_TYPES[num_type]
        self._state_ref: Dict[str, Any] = hass.data[DOMAIN][entry.entry_id]

        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_airlock_{airlock_id}_{num_type}"
        self._attr_name = f"Airlock {airlock_id} {self._meta['name']}"
        self._attr_mode = self._meta["mode"]
        self._attr_native_min_value = self._meta["min"]
        self._attr_native_max_value = self._meta["max"]
        self._attr_native_step = self._meta["step"]

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_airlock_{self.airlock_id}")},
            name=f"Airlock {self.airlock_id}",
            manufacturer="open-plaato-keg",
            model="Plaato Airlock",
        )

    def _airlock(self) -> Dict[str, Any]:
        return self._state_ref.get("airlock_data", {}).get(self.airlock_id, {})

    @property
    def native_value(self) -> float | None:
        val = self._airlock().get(self._meta["key"])
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        a = self._airlock()
        endpoint = self._meta["endpoint"]
        base = self._state_ref.get("rest_base", "")

        if endpoint == "grainfather":
            body = {
                "enabled": a.get("grainfather_enabled", False),
                "unit": a.get("grainfather_unit") or "celsius",
                "specific_gravity": value,
                "url": a.get("grainfather_url") or "",
            }
        else:
            body: Dict[str, Any] = {
                "enabled": a.get("brewfather_enabled", False),
                "unit": a.get("brewfather_temp_unit") or "celsius",
                "specific_gravity": value,
                "url": a.get("brewfather_url") or "",
            }
            if a.get("brewfather_og") is not None:
                body["og"] = a["brewfather_og"]
            if a.get("brewfather_batch_volume") is not None:
                body["batch_volume"] = a["brewfather_batch_volume"]

        url = f"{base}/api/airlocks/{self.airlock_id}/{endpoint}"
        try:
            session = async_get_clientsession(self.hass)
            async with session.post(url, json=body) as resp:
                if resp.status not in range(200, 300):
                    _LOGGER.warning("AirlockNumberEntity POST %s returned %s", url, resp.status)
        except aiohttp.ClientError as err:
            _LOGGER.error("AirlockNumberEntity POST %s failed: %s", url, err)

        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.hass.bus.async_listen(AIRLOCK_EVENT, self._on_event))

    @callback
    def _on_event(self, event) -> None:
        if (event.data or {}).get("airlock_id") == self.airlock_id:
            self.async_write_ha_state()
