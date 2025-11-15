from __future__ import annotations

import logging
from typing import List, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.select import SelectEntity
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DEVICES_UPDATE_EVENT = f"{DOMAIN}_devices_update"

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    state = hass.data[DOMAIN][entry.entry_id]
    entity = BeerKegDevicesSelect(hass, entry)
    async_add_entities([entity], True)

class BeerKegDevicesSelect(SelectEntity):
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_devices"
        self._attr_name = "Beer Keg Devices"
        self._options: List[str] = list(self._get_ids())
        self._current_option: Optional[str] = self._options[0] if self._options else None

    def _get_ids(self) -> List[str]:
        return list(self.hass.data[DOMAIN][self.entry.entry_id].get("devices", []))

    @property
    def options(self) -> List[str]:
        return self._options

    @property
    def current_option(self) -> Optional[str]:
        return self._current_option

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_device_selector")},
            name="Beer Keg Device Selector",
            manufacturer="Beer Keg",
            model="Device list",
        )

    async def async_added_to_hass(self) -> None:
        # subscribe to device list updates
        self.async_on_remove(self.hass.bus.async_listen(DEVICES_UPDATE_EVENT, self._on_devices_update))

    @callback
    def _on_devices_update(self, event) -> None:
        ids = event.data.get("ids") or self._get_ids()
        self._options = list(ids)
        if not self._options:
            self._current_option = None
        elif self._current_option not in self._options:
            self._current_option = self._options[0]
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        if option in self._options:
            self._current_option = option
            self.async_write_ha_state()
        else:
            _LOGGER.debug("%s: tried to select unknown option %s", self._attr_name, option)
