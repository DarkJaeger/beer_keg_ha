from __future__ import annotations

import logging
from typing import Any, Dict, List

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PLATFORM_EVENT, DEVICES_UPDATE_EVENT

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up v2-only selects (single manual keg selector)."""
    state = hass.data[DOMAIN][entry.entry_id]

    # Ensure key exists
    state.setdefault("selected_device", None)

    async_add_entities([BeerKegDeviceSelect(hass, entry, state)], True)


class BeerKegDeviceSelect(SelectEntity):
    """Select entity listing all keg devices discovered by the integration."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        state_ref: Dict[str, Any],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._state_ref = state_ref

        self._attr_name = "Keg Device"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_keg_device"
        self._attr_icon = "mdi:keg"

        self._attr_options: list[str] = []
        self._last_options: list[str] = []

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_settings")},
            name="Beer Keg Settings",
            manufacturer="open-plaato-keg",
            model="API v2",
        )

    def _read_devices(self) -> list[str]:
        devices = self._state_ref.get("devices") or []
        cleaned = [str(d) for d in devices if d not in (None, "", "unknown", "unavailable")]
        # stable order de-dupe
        seen = set()
        out: list[str] = []
        for d in cleaned:
            if d not in seen:
                seen.add(d)
                out.append(d)
        return out

    def _ensure_valid_selection(self) -> None:
        selected = self._state_ref.get("selected_device")
        if selected in self._attr_options:
            return
        self._state_ref["selected_device"] = self._attr_options[0] if self._attr_options else None

    @property
    def current_option(self) -> str | None:
        selected = self._state_ref.get("selected_device")
        if selected in self._attr_options:
            return selected
        if self._attr_options:
            return self._attr_options[0]
        return None

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            _LOGGER.warning("%s: Attempt to select unknown keg device: %s", DOMAIN, option)
            return

        self._state_ref["selected_device"] = option

        # nudge sensors/UI
        self.hass.bus.async_fire(PLATFORM_EVENT, {"keg_id": option})
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        # Initial options
        new_opts = self._read_devices()
        self._attr_options = new_opts
        self._last_options = list(new_opts)
        self._ensure_valid_selection()
        self.async_write_ha_state()

        @callback
        def _handle_devices_update(_event) -> None:
            new = self._read_devices()
            if new == self._last_options:
                return

            self._attr_options = new
            self._last_options = list(new)
            self._ensure_valid_selection()
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(DEVICES_UPDATE_EVENT, _handle_devices_update)
        )
