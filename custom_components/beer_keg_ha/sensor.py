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

SENSOR_TYPES = {
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
    "fill_percent": {
        "unit": "%",
        "name": "Fill Level",
        "key": "fill_percent",
        "icon": "mdi:cup",
        "device_class": None,
        "state_class": "measurement",
    },
    # legacy alias
    "fill_level": {
        "unit": "%",
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

KG_TO_LB = 2.20462


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data[DOMAIN][entry.entry_id]
    created: Set[str] = state.setdefault("created_kegs", set())

    def create_for(keg_id: str) -> None:
        if keg_id in created:
            return
        ents: List[KegSensor] = [
            KegSensor(hass, entry, keg_id, key) for key in SENSOR_TYPES.keys()
        ]
        async_add_entities(ents, True)
        created.add(keg_id)

    # Create sensors for any kegs already known
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
        # Reference to integration state so we can read display_units
        self._state = hass.data[DOMAIN][entry.entry_id]

        meta = SENSOR_TYPES[sensor_type]
        self._attr_name = f"Keg {keg_id} {meta['name']}"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{keg_id}_{sensor_type}"
        self._attr_icon = meta.get("icon")
        self._attr_device_class = meta.get("device_class")
        self._attr_state_class = meta.get("state_class")
        # Do NOT set _attr_native_unit_of_measurement statically here.
        # We'll compute units dynamically based on state["display_units"].

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_{self.keg_id}")},
            name=f"Beer Keg {self.keg_id}",
            manufacturer="Beer Keg",
            model="WebSocket + REST",
        )

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the active unit, honoring display_units for weight/temp."""
        meta = SENSOR_TYPES[self.sensor_type]
        base_unit = meta["unit"]
        units = self._state.get("display_units", {})

        if self.sensor_type in ("weight", "full_weight"):
            return units.get("weight", base_unit)
        if self.sensor_type == "temperature":
            return units.get("temp", base_unit)
        return base_unit

    @property
    def native_value(self) -> Any:
        data: Dict[str, Dict[str, Any]] = self.hass.data[DOMAIN][self.entry.entry_id][
            "data"
        ]
        meta = SENSOR_TYPES[self.sensor_type]
        raw = data.get(self.keg_id, {}).get(meta["key"])

        if raw is None:
            return None

        units = self._state.get("display_units", {})

        # Weight / Full Weight: kg <-> lb
        if self.sensor_type in ("weight", "full_weight"):
            target = units.get("weight", "kg")
            if target == "lb":
                try:
                    return round(float(raw) * KG_TO_LB, 2)
                except Exception:
                    return raw
            return raw

        # Temperature: °C <-> °F
        if self.sensor_type == "temperature":
            target = units.get("temp", "°C")
            if target == "°F":
                try:
                    return round((float(raw) * 9.0 / 5.0) + 32.0, 1)
                except Exception:
                    return raw
            return raw

        # Other sensors (oz, %, id, name, etc.) unchanged
        return raw

    async def async_added_to_hass(self) -> None:
        # Ensure handler runs in the event loop
        self.async_on_remove(
            self.hass.bus.async_listen(PLATFORM_EVENT, self._refresh_if_mine)
        )

    @callback
    def _refresh_if_mine(self, event) -> None:
        if (event.data or {}).get("keg_id") == self.keg_id:
            self.async_write_ha_state()

