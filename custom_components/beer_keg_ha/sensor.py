from __future__ import annotations

import logging
from typing import Any, Dict, List, Set

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    PLATFORM_EVENT,
    ATTR_KEGGED_DATE,
    ATTR_EXPIRATION_DATE,
    ATTR_DAYS_UNTIL_EXPIRATION,
)

_LOGGER = logging.getLogger(__name__)


# -------------------------------------------------------------------
# SENSOR DEFINITIONS
# -------------------------------------------------------------------

SENSOR_DEFS = {
    "total_weight_kg": {
        "name": "Total Weight",
        "unit": "kg",
        "device_class": "weight",
    },
    "beer_remaining_kg": {
        "name": "Beer Remaining",
        "unit": "kg",
        "device_class": "weight",
    },
    "liters_remaining": {
        "name": "Liters Remaining",
        "unit": "L",
    },
    "percent_of_beer_left": {
        "name": "Percent Remaining",
        "unit": "%",
    },
    "keg_temperature_c": {
        "name": "Keg Temperature",
        "unit": "°C",
        "device_class": "temperature",
    },
    "chip_temperature_c": {
        "name": "Chip Temperature",
        "unit": "°C",
        "device_class": "temperature",
    },
    "wifi_signal_strength": {
        "name": "WiFi Signal",
        "unit": "dBm",
        "device_class": "signal_strength",
    },
    "last_pour_oz": {
        "name": "Last Pour",
        "unit": "oz",
    },
    "daily_consumption_oz": {
        "name": "Daily Consumption",
        "unit": "oz",
    },
    "my_beer_style": {
        "name": "Beer Style",
    },
    "my_og": {
        "name": "Original Gravity",
    },
    "my_fg": {
        "name": "Final Gravity",
    },
    "my_abv": {
        "name": "ABV",
        "unit": "%",
    },
    ATTR_KEGGED_DATE: {
        "name": "Kegged Date",
    },
    ATTR_EXPIRATION_DATE: {
        "name": "Expiration Date",
    },
    ATTR_DAYS_UNTIL_EXPIRATION: {
        "name": "Days Until Expiration",
        "unit": "days",
    },
}

# NOTE:
# ❌ is_pouring is INTENTIONALLY NOT INCLUDED HERE
# It is now handled ONLY by binary_sensor.py


# -------------------------------------------------------------------
# SETUP
# -------------------------------------------------------------------

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data[DOMAIN][entry.entry_id]
    created: Set[str] = state.setdefault("created_sensor_kegs", set())

    entities: List[SensorEntity] = []

    def create_for(keg_id: str) -> None:
        if keg_id in created:
            return
        for key, meta in SENSOR_DEFS.items():
            entities.append(
                BeerKegSensor(
                    hass=hass,
                    entry=entry,
                    keg_id=keg_id,
                    key=key,
                    meta=meta,
                )
            )
        created.add(keg_id)

    # Existing kegs
    for keg_id in list(state.get("data", {}).keys()):
        create_for(keg_id)

    async_add_entities(entities, True)

    @callback
    def _on_update(event) -> None:
        keg_id = (event.data or {}).get("keg_id")
        if keg_id:
            create_for(keg_id)

    entry.async_on_unload(
        hass.bus.async_listen(PLATFORM_EVENT, _on_update)
    )


# -------------------------------------------------------------------
# SENSOR ENTITY
# -------------------------------------------------------------------

class BeerKegSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        keg_id: str,
        key: str,
        meta: Dict[str, Any],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.keg_id = keg_id
        self.key = key
        self.meta = meta

        short_id = keg_id[:4]

        self._attr_name = f"Keg {short_id} {meta['name']}"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{keg_id}_{key}"

        self._attr_native_unit_of_measurement = meta.get("unit")
        self._attr_device_class = meta.get("device_class")

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
    def native_value(self) -> Any:
        data = self.hass.data[DOMAIN][self.entry.entry_id]["data"]
        return data.get(self.keg_id, {}).get(self.key)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.hass.bus.async_listen(PLATFORM_EVENT, self._handle_update)
        )

    @callback
    def _handle_update(self, event) -> None:
        if (event.data or {}).get("keg_id") == self.keg_id:
            self.async_write_ha_state()
