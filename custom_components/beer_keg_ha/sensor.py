from __future__ import annotations

import logging
from typing import Any, Dict, List, Set

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORM_EVENT = f"{DOMAIN}_update"

# Base/meta definition – "unit" here is the *raw* unit our integration stores
# in hass.data[DOMAIN][entry_id]["data"], not necessarily what we show on cards.
SENSOR_TYPES: Dict[str, Dict[str, Any]] = {
    # ---- RAW BASE SENSORS (ALWAYS KG / °C / ETC.) ----
    "weight": {
        "unit": "kg",           # raw
        "name": "Weight",
        "key": "weight",
        "icon": "mdi:scale",
        "device_class": "weight",
        "state_class": "measurement",
    },
    "temperature": {
        "unit": "°C",           # raw
        "name": "Temperature",
        "key": "temperature",
        "icon": "mdi:thermometer",
        "device_class": "temperature",
        "state_class": "measurement",
    },
    "fill_percent": {
        "unit": "%",            # raw
        "name": "Fill Level",
        "key": "fill_percent",
        "icon": "mdi:cup",
        "device_class": None,
        "state_class": "measurement",
    },
    # legacy alias
    "fill_level": {
        "unit": "%",            # raw
        "name": "Fill Level",
        "key": "fill_percent",
        "icon": "mdi:cup",
        "device_class": None,
        "state_class": "measurement",
    },
    "last_pour": {
        "unit": "oz",
        "name": "Last Pour",
        "key": "last_pour",
        "icon": "mdi:cup-water",
        "device_class": None,
        "state_class": "measurement",
    },
    "daily_consumed": {
        "unit": "oz",
        "name": "Daily Consumption",
        "key": "daily_consumed",
        "icon": "mdi:beer",
        "device_class": None,
        "state_class": "total_increasing",
    },
    "full_weight": {
        "unit": "kg",           # raw
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
        "last_pour": {
        "unit": "oz",
        "name": "Last Pour",
        "key": "last_pour",
        "icon": "mdi:cup-water",
        "device_class": None,
        "state_class": "measurement",
    },
        "last_pour_ml": {
        "unit": "ml",
        "name": "Last Pour (ml)",
        "key": "last_pour_ml",
        "icon": "mdi:cup-water",
        "device_class": None,
        "state_class": "measurement",
    },

    # ---- NEW: DISPLAY SENSORS (FOLLOW display_units) ----
    # These are what you’ll point your Lovelace cards at.
    "weight_display": {
        "unit": "kg",  # base (we override dynamically)
        "name": "Weight Display",
        "key": "weight",  # still reading raw kg from state["data"]
        "icon": "mdi:scale",
        "device_class": None,           # keep this free so HA doesn't fight us
        "state_class": "measurement",
    },
    "temperature_display": {
        "unit": "°C",  # base
        "name": "Temperature Display",
        "key": "temperature",  # raw °C from state["data"]
        "icon": "mdi:thermometer",
        "device_class": None,           # no enforced units
        "state_class": "measurement",
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

    def create_for(keg_id: str) -> None:
        """Create all sensors for one keg_id (once)."""
        if keg_id in created:
            return

        ents: List[KegSensor] = [
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
        """Handle keg update events from the integration."""
        keg_id = (event.data or {}).get("keg_id")
        if keg_id:
            create_for(keg_id)
            # existing sensors for this keg will also be nudged by their own listeners

    entry.async_on_unload(hass.bus.async_listen(PLATFORM_EVENT, _on_update))


class KegSensor(SensorEntity):
    """One logical sensor (weight/temp/etc.) for a specific keg."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        keg_id: str,
        sensor_type: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.keg_id = keg_id
        self.sensor_type = sensor_type

        self._state_ref: Dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
        self._meta = SENSOR_TYPES[sensor_type]

        self._attr_name = f"Keg {keg_id} {self._meta['name']}"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{keg_id}_{sensor_type}"
        self._attr_icon = self._meta.get("icon")
        self._attr_device_class = self._meta.get("device_class")
        self._attr_state_class = self._meta.get("state_class")
        self._attr_native_unit_of_measurement = self._meta.get("unit")

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_{self.keg_id}")},
            name=f"Beer Keg {self.keg_id}",
            manufacturer="Beer Keg",
            model="WebSocket + REST",
        )

    # ---- helpers ----

    def _get_display_units(self) -> Dict[str, str]:
        """Return normalized display units dict from integration state."""
        du = self._state_ref.get("display_units") or {}
        weight_u = du.get("weight", "kg")
        temp_u = du.get("temp", "°C")
        volume_u = du.get("volume", "oz")

        if weight_u not in ("kg", "lb"):
            weight_u = "kg"
        if temp_u not in ("°C", "°F"):
            temp_u = "°C"
        if volume_u not in ("oz", "ml"):
            volume_u = "oz"

        return {"weight": weight_u, "temp": temp_u, "volume": volume_u}


    # ---- core value ----

    @property
        @property
    def native_value(self) -> Any:
        """Return value, converted according to Beer Keg display units."""
        data: Dict[str, Dict[str, Any]] = self._state_ref.get("data", {})
        raw = data.get(self.keg_id, {}).get(self._meta["key"])

        if raw is None:
            return None

        units = self._get_display_units()

        # ---- weight & full_weight (stored as kg) ----
        if self.sensor_type in ("weight", "full_weight"):
            try:
                raw_kg = float(raw)
            except (TypeError, ValueError):
                return None

            if units["weight"] == "lb":
                # convert kg -> lb
                self._attr_native_unit_of_measurement = "lb"
                return round(raw_kg * 2.20462, 2)
            else:
                # stay in kg
                self._attr_native_unit_of_measurement = "kg"
                return round(raw_kg, 2)

        # ---- temperature (stored as °C) ----
        if self.sensor_type == "temperature":
            try:
                raw_c = float(raw)
            except (TypeError, ValueError):
                return None

            if units["temp"] == "°F":
                self._attr_native_unit_of_measurement = "°F"
                return round((raw_c * 9.0 / 5.0) + 32.0, 1)
            else:
                self._attr_native_unit_of_measurement = "°C"
                return round(raw_c, 1)

        # ---- volume (stored as oz) ----
        if self.sensor_type in ("last_pour", "daily_consumed"):
            try:
                raw_oz = float(raw)
            except (TypeError, ValueError):
                return None

            if units["volume"] == "ml":
                # 1 US fl oz = 29.5735 ml
                self._attr_native_unit_of_measurement = "ml"
                return int(round(raw_oz * 29.5735))
            else:
                self._attr_native_unit_of_measurement = "oz"
                return round(raw_oz, 1)

        # ---- everything else: no conversion ----
        self._attr_native_unit_of_measurement = self._meta.get("unit")
        return raw

        # ---- DISPLAY SENSORS: follow display_units ----

        if self.sensor_type == "weight_display":
            units = self._get_display_units()
            try:
                raw_kg = float(raw)
            except (TypeError, ValueError):
                return None

            if units["weight"] == "lb":
                self._attr_native_unit_of_measurement = "lb"
                return round(raw_kg * 2.20462, 2)
            else:
                self._attr_native_unit_of_measurement = "kg"
                return round(raw_kg, 2)

        if self.sensor_type == "temperature_display":
            units = self._get_display_units()
            try:
                raw_c = float(raw)
            except (TypeError, ValueError):
                return None

            if units["temp"] == "°F":
                self._attr_native_unit_of_measurement = "°F"
                return round((raw_c * 9.0 / 5.0) + 32.0, 1)
            else:
                self._attr_native_unit_of_measurement = "°C"
                return round(raw_c, 1)

        # ---- everything else: no conversion ----
        self._attr_native_unit_of_measurement = self._meta.get("unit")
        return raw

    async def async_added_to_hass(self) -> None:
        """Subscribe to integration update events for this keg."""
        self.async_on_remove(
            self.hass.bus.async_listen(PLATFORM_EVENT, self._refresh_if_mine)
        )

    @callback
    def _refresh_if_mine(self, event) -> None:
        """Refresh state when our keg_id gets an update event."""
        if (event.data or {}).get("keg_id") == self.keg_id:
            self.async_write_ha_state()

