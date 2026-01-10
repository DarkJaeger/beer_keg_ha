from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ATTR_PLAATO_API_V2, ATTR_PLAATO_API_VERSION

_LOGGER = logging.getLogger(__name__)

PLATFORM_EVENT = f"{DOMAIN}_update"

SENSOR_TYPES: Dict[str, Dict[str, Any]] = {
    # --- RAW BASE SENSORS (HA-native, fixed units) ---
    "weight": {
        "unit": "kg",
        "name": "Weight",
        "key": "weight",
        "icon": "mdi:scale",
        "device_class": "weight",
        "state_class": "measurement",
    },
    "temperature": {
        "unit": "°C",
        "name": "Temperature",
        "key": "temperature",
        "icon": "mdi:thermometer",
        "device_class": "temperature",
        "state_class": "measurement",
    },

    # --- NEW: API v2 native liters remaining (L) ---
    "liters_remaining": {
        "unit": "L",
        "name": "Liters Remaining",
        "key": "liters_left",
        "icon": "mdi:cup",
        "device_class": "volume",
        "state_class": "measurement",
    },

    # --- FILL LEVEL ---
    "fill_percent": {
        "unit": "%",
        "name": "Fill Level",
        "key": "fill_percent",
        "icon": "mdi:cup",
        "device_class": None,
        "state_class": "measurement",
    },

    # --- POUR / CONSUMPTION (base = oz) ---
    "last_pour": {
        "unit": "oz",
        "name": "Last Pour (oz)",
        "key": "last_pour",
        "icon": "mdi:cup-water",
        "device_class": None,
        "state_class": "measurement",
    },
    "daily_consumed": {
        "unit": "oz",
        "name": "Daily Consumption (oz)",
        "key": "daily_consumed",
        "icon": "mdi:beer",
        "device_class": None,
        "state_class": "total_increasing",
    },

    # --- FULL WEIGHT / META ---
    "full_weight": {
        "unit": "kg",
        "name": "Full Weight",
        "key": "full_weight",
        "icon": "mdi:weight",
        "device_class": "weight",
        "state_class": "measurement",
    },
    "name": {
        "unit": None,
        "name": "Name",
        "key": "name",
        "icon": "mdi:barcode",
        "device_class": None,
        "state_class": None,
    },
    "id": {
        "unit": None,
        "name": "ID",
        "key": "id",
        "icon": "mdi:identifier",
        "device_class": None,
        "state_class": None,
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up keg sensors for a config entry."""
    state = hass.data[DOMAIN][entry.entry_id]
    created: Set[str] = state.setdefault("created_kegs", set())

    # One debug sensor per config entry
    async_add_entities([BeerKegDebugSensor(hass, entry)], True)

    def create_for(keg_id: str) -> None:
        if keg_id in created:
            return
        ents: List[SensorEntity] = [
            KegSensor(hass, entry, keg_id, sensor_key)
            for sensor_key in SENSOR_TYPES.keys()
        ]
        async_add_entities(ents, True)
        created.add(keg_id)

    # create for any already-known kegs
    for keg_id in list(state.get("data", {}).keys()):
        create_for(keg_id)

    @callback
    def _on_update(event) -> None:
        keg_id = (event.data or {}).get("keg_id")
        if keg_id:
            create_for(keg_id)

    entry.async_on_unload(hass.bus.async_listen(PLATFORM_EVENT, _on_update))


class KegSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, keg_id: str, sensor_type: str) -> None:
        self.hass = hass
        self.entry = entry
        self.keg_id = keg_id
        self.sensor_type = sensor_type
        self._state_ref: Dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
        self._meta = SENSOR_TYPES[sensor_type]

        short_id = keg_id[:4]
        self._attr_name = f"Keg {short_id} {self._meta['name']}"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{keg_id}_{sensor_type}"
        self._attr_icon = self._meta.get("icon")
        self._attr_device_class = self._meta.get("device_class")
        self._attr_state_class = self._meta.get("state_class")
        self._attr_native_unit_of_measurement = self._meta.get("unit")

    @property
    def device_info(self) -> DeviceInfo:
        short_id = self.keg_id[:4]
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_{self.keg_id}")},
            name=f"Beer Keg {short_id}",
            manufacturer="Beer Keg",
            model="WebSocket + REST",
        )

    @property
    def native_value(self) -> Any:
        data: Dict[str, Dict[str, Any]] = self._state_ref.get("data", {})
        raw = data.get(self.keg_id, {}).get(self._meta["key"])
        if raw is None:
            return None

        # liters_remaining should be numeric if present; it will be None on v1 servers
        if self.sensor_type == "liters_remaining":
            try:
                return round(float(raw), 3)
            except Exception:
                return None

        return raw

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.hass.bus.async_listen(PLATFORM_EVENT, self._refresh_if_mine))

    @callback
    def _refresh_if_mine(self, event) -> None:
        if (event.data or {}).get("keg_id") == self.keg_id:
            self.async_write_ha_state()


class BeerKegDebugSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_debug"
        self._attr_name = "Beer Keg Debug"
        self._attr_icon = "mdi:bug"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_settings")},
            name="Beer Keg Settings",
            manufacturer="Beer Keg",
            model="WebSocket + REST",
        )

    @property
    def native_value(self) -> float | None:
        state = self.hass.data[DOMAIN][self.entry.entry_id]
        ts = state.get("last_update_ts")
        if not isinstance(ts, datetime):
            return None
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return round(age, 1)

    @property
    def extra_state_attributes(self) -> dict:
        state = self.hass.data[DOMAIN][self.entry.entry_id]
        return {
            "ws_url": state.get("ws_url"),
            "devices": list(state.get("devices", [])),
            ATTR_PLAATO_API_VERSION: state.get(ATTR_PLAATO_API_VERSION, "v1"),
            ATTR_PLAATO_API_V2: bool(state.get(ATTR_PLAATO_API_V2, False)),
        }
