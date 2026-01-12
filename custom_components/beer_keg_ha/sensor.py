from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    PLATFORM_EVENT,
    ATTR_LAST_UPDATE,
    ATTR_KEGGED_DATE,
    ATTR_EXPIRATION_DATE,
    ATTR_DAYS_UNTIL_EXPIRATION,
)

_LOGGER = logging.getLogger(__name__)

# v2-only sensors: raw + derived
SENSOR_TYPES: Dict[str, Dict[str, Any]] = {
    # -------------------------
    # Computed (derived)
    # -------------------------
    "total_weight_kg": {
        "name": "Total Weight",
        "key": "total_weight_kg",
        "icon": "mdi:scale",
        "device_class": "weight",
        "state_class": "measurement",
        "unit": "kg",
        "round": 3,
    },
    "beer_remaining_kg": {
        "name": "Beer Remaining",
        "key": "beer_remaining_kg",
        "icon": "mdi:beer",
        "device_class": "weight",
        "state_class": "measurement",
        "unit": "kg",
        "round": 3,
    },
    "liters_remaining": {
        "name": "Beer Remaining",
        "key": "liters_remaining",
        "icon": "mdi:cup",
        "device_class": "volume",
        "state_class": None,  # remaining is a level
        "unit": "L",
        "round": 3,
    },

    # NEW: computed pour stats (from __init__.py)
    "last_pour_oz": {
        "name": "Last Pour",
        "key": "last_pour_oz",
        "icon": "mdi:cup-water",
        "device_class": None,
        "state_class": "measurement",
        "unit": "oz",
        "round": 1,
    },
    "daily_consumption_oz": {
        "name": "Daily Consumption",
        "key": "daily_consumption_oz",
        "icon": "mdi:beer",
        "device_class": None,
        "state_class": "measurement",
        "unit": "oz",
        "round": 1,
    },

    # NEW: manual meta dates (strings) + derived days-until
    "kegged_date": {
        "name": "Kegged Date",
        "key": ATTR_KEGGED_DATE,
        "icon": "mdi:calendar-start",
        "device_class": None,
        "state_class": None,
        "unit": None,
        "round": None,
    },
    "expiration_date": {
        "name": "Expiration Date",
        "key": ATTR_EXPIRATION_DATE,
        "icon": "mdi:calendar-end",
        "device_class": None,
        "state_class": None,
        "unit": None,
        "round": None,
    },
    "days_until_expiration": {
        "name": "Days Until Expiration",
        "key": ATTR_DAYS_UNTIL_EXPIRATION,
        "icon": "mdi:timer-sand",
        "device_class": None,
        "state_class": "measurement",
        "unit": "d",
        "round": None,
    },

    # -------------------------
    # Raw v2 fields (from API)
    # -------------------------
    "empty_keg_weight": {
        "name": "Empty Keg Weight",
        "key": "empty_keg_weight",
        "icon": "mdi:weight",
        "device_class": "weight",
        "state_class": "measurement",
        "unit": "kg",
        "round": 3,
    },

    # amount_left meaning depends on beer_left_unit (kg or L)
    "amount_left": {
        "name": "Amount Left (Raw)",
        "key": "amount_left",
        "icon": "mdi:cup",
        "device_class": None,
        "state_class": None,
        "unit": None,  # dynamic
        "round": 3,
    },

    "percent_of_beer_left": {
        "name": "Percent of Beer Left",
        "key": "percent_of_beer_left",
        "icon": "mdi:percent",
        "device_class": None,
        "state_class": "measurement",
        "unit": "%",
        "round": 1,
    },

    # Temperatures
    "keg_temperature_c": {
        "name": "Temperature",
        "key": "keg_temperature_c",
        "icon": "mdi:thermometer",
        "device_class": "temperature",
        "state_class": "measurement",
        "unit": "°C",
        "round": 2,
    },
    "chip_temperature_c": {
        "name": "Chip Temperature",
        "key": "chip_temperature_c",
        "icon": "mdi:chip",
        "device_class": "temperature",
        "state_class": "measurement",
        "unit": "°C",
        "round": 2,
    },
    "temperature_offset": {
        "name": "Temperature Offset",
        "key": "temperature_offset",
        "icon": "mdi:thermometer-plus",
        "device_class": "temperature",
        "state_class": "measurement",
        "unit": "°C",
        "round": 3,
    },
    "min_temperature": {
        "name": "Min Temperature",
        "key": "min_temperature",
        "icon": "mdi:thermometer-low",
        "device_class": "temperature",
        "state_class": "measurement",
        "unit": "°C",
        "round": 3,
    },
    "max_temperature": {
        "name": "Max Temperature",
        "key": "max_temperature",
        "icon": "mdi:thermometer-high",
        "device_class": "temperature",
        "state_class": "measurement",
        "unit": "°C",
        "round": 3,
    },

    "wifi_signal_strength": {
        "name": "WiFi Signal",
        "key": "wifi_signal_strength",
        "icon": "mdi:wifi",
        "device_class": None,
        "state_class": "measurement",
        "unit": "%",
        "round": None,
    },

    "leak_detection": {
        "name": "Leak Detection",
        "key": "leak_detection",
        "icon": "mdi:water-alert",
        "device_class": None,
        "state_class": None,
        "unit": None,
        "round": None,
    },
    "is_pouring": {
        "name": "Is Pouring",
        "key": "is_pouring",
        "icon": "mdi:cup-water",
        "device_class": None,
        "state_class": None,
        "unit": None,
        "round": None,
    },

    "last_pour": {
        "name": "Last Pour (Raw)",
        "key": "last_pour",
        "icon": "mdi:cup-water",
        "device_class": None,
        "state_class": "measurement",
        "unit": None,
        "round": 3,
    },
    "last_pour_string": {
        "name": "Last Pour",
        "key": "last_pour_string",
        "icon": "mdi:cup-water",
        "device_class": None,
        "state_class": None,
        "unit": None,
        "round": None,
    },

    "max_keg_volume": {
        "name": "Max Keg Volume",
        "key": "max_keg_volume",
        "icon": "mdi:keg",
        "device_class": "volume",
        "state_class": None,
        "unit": "L",
        "round": 3,
    },

    "measure_unit": {"name": "Measure Unit", "key": "measure_unit", "icon": "mdi:tune", "device_class": None, "state_class": None, "unit": None, "round": None},
    "unit": {"name": "Unit System", "key": "unit", "icon": "mdi:ruler", "device_class": None, "state_class": None, "unit": None, "round": None},

    "volume_unit": {"name": "Volume Unit", "key": "volume_unit", "icon": "mdi:format-letter-case", "device_class": None, "state_class": None, "unit": None, "round": None},
    "beer_left_unit": {"name": "Beer Left Unit", "key": "beer_left_unit", "icon": "mdi:format-letter-case", "device_class": None, "state_class": None, "unit": None, "round": None},
    "temperature_unit": {"name": "Temperature Unit", "key": "temperature_unit", "icon": "mdi:format-letter-case", "device_class": None, "state_class": None, "unit": None, "round": None},

    "firmware_version": {"name": "Firmware Version", "key": "firmware_version", "icon": "mdi:identifier", "device_class": None, "state_class": None, "unit": None, "round": None},
    "keg_temperature_string": {"name": "Temperature (String)", "key": "keg_temperature_string", "icon": "mdi:thermometer", "device_class": None, "state_class": None, "unit": None, "round": None},
    "chip_temperature_string": {"name": "Chip Temp (String)", "key": "chip_temperature_string", "icon": "mdi:chip", "device_class": None, "state_class": None, "unit": None, "round": None},

    "og": {"name": "OG", "key": "og", "icon": "mdi:alpha-o-circle", "device_class": None, "state_class": None, "unit": None, "round": None},
    "fg": {"name": "FG", "key": "fg", "icon": "mdi:alpha-f-circle", "device_class": None, "state_class": None, "unit": None, "round": None},

    "internal_ver": {"name": "Internal Version", "key": "internal_ver", "icon": "mdi:information", "device_class": None, "state_class": None, "unit": None, "round": None},
    "internal_fw": {"name": "Internal FW", "key": "internal_fw", "icon": "mdi:information", "device_class": None, "state_class": None, "unit": None, "round": None},
    "internal_dev": {"name": "Internal Device", "key": "internal_dev", "icon": "mdi:chip", "device_class": None, "state_class": None, "unit": None, "round": None},
    "internal_build": {"name": "Internal Build", "key": "internal_build", "icon": "mdi:calendar", "device_class": None, "state_class": None, "unit": None, "round": None},
    "internal_tmpl": {"name": "Internal Template", "key": "internal_tmpl", "icon": "mdi:tag", "device_class": None, "state_class": None, "unit": None, "round": None},
    "internal_hbeat": {"name": "Internal Heartbeat", "key": "internal_hbeat", "icon": "mdi:heart-pulse", "device_class": None, "state_class": "measurement", "unit": "s", "round": None},
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data[DOMAIN][entry.entry_id]
    created: Set[str] = state.setdefault("created_kegs", set())

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
        self._static_unit = self._meta.get("unit")

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
    def native_unit_of_measurement(self) -> str | None:
        # amount_left is dynamic: depends on beer_left_unit
        if self.sensor_type == "amount_left":
            d = self._state_ref.get("data", {}).get(self.keg_id, {})
            u = str(d.get("beer_left_unit") or "").lower()
            if u in ("kg", "kilogram", "kilograms"):
                return "kg"
            if u in ("litre", "liter", "liters", "litres", "l"):
                return "L"
            return None
        return self._static_unit

    @property
    def native_value(self) -> Any:
        data: Dict[str, Dict[str, Any]] = self._state_ref.get("data", {})
        raw = data.get(self.keg_id, {}).get(self._meta["key"])
        if raw is None:
            return None

        r = self._meta.get("round")
        if isinstance(raw, (int, float)) and isinstance(r, int):
            return round(float(raw), r)
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
            manufacturer="open-plaato-keg",
            model="API v2",
        )

    @property
    def native_value(self) -> float | None:
        st = self.hass.data[DOMAIN][self.entry.entry_id]
        ts = st.get(ATTR_LAST_UPDATE)
        if not isinstance(ts, datetime):
            return None
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return round(age, 1)

    @property
    def extra_state_attributes(self) -> dict:
        st = self.hass.data[DOMAIN][self.entry.entry_id]
        return {
            "ws_url": st.get("ws_url"),
            "devices": list(st.get("devices", [])),
            "kegs": list(st.get("data", {}).keys()),
        }
