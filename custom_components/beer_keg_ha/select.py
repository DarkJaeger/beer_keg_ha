from __future__ import annotations

import logging
from typing import Any, Dict, List

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.components.select import SelectEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Events we fire from __init__.py
DEVICES_UPDATE_EVENT = f"{DOMAIN}_devices_update"
UNITS_CHANGED_EVENT = f"{DOMAIN}_units_changed"

WEIGHT_OPTIONS = ["kg", "lb"]
TEMP_OPTIONS = ["°C", "°F"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select entities for this config entry."""
    state: Dict[str, Any] = hass.data[DOMAIN][entry.entry_id]

    entities: List[SelectEntity] = [
        # Weight unit selector
        KegUnitSelect(
            hass=hass,
            entry=entry,
            kind="weight",
            name="Keg Weight Unit",
            options=WEIGHT_OPTIONS,
        ),
        # Temperature unit selector
        KegUnitSelect(
            hass=hass,
            entry=entry,
            kind="temp",
            name="Keg Temperature Unit",
            options=TEMP_OPTIONS,
        ),
        # Keg device selector (list of available keg IDs)
        KegDeviceSelect(
            hass=hass,
            entry=entry,
            name="Keg Device",
        ),
    ]

    async_add_entities(entities, True)


class KegUnitSelect(SelectEntity):
    """Select entity to choose display units for the Beer Keg integration."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        kind: str,
        name: str,
        options: list[str],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._kind = kind  # "weight" or "temp"
        self._attr_name = name
        self._attr_options = options
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_unit_{kind}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_units")},
            name="Beer Keg Display Units",
            manufacturer="Beer Keg",
            model="Unit Preferences",
        )

    @property
    def current_option(self) -> str | None:
        state: Dict[str, Any] = self.hass.data[DOMAIN][self.entry.entry_id]
        units = state.get("display_units", {})
        val = units.get(self._kind)

        # Fallback to first option if unknown
        if val not in self._attr_options:
            return self._attr_options[0]
        return val

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            _LOGGER.warning(
                "%s: invalid unit option %s for kind %s", DOMAIN, option, self._kind
            )
            return

        state: Dict[str, Any] = self.hass.data[DOMAIN][self.entry.entry_id]
        units = state.setdefault("display_units", {})
        units[self._kind] = option

        _LOGGER.info("%s: set %s unit to %s", DOMAIN, self._kind, option)

        # Broadcast a "units changed" event so templates/cards can react
        self.hass.bus.async_fire(
            UNITS_CHANGED_EVENT,
            {"kind": self._kind, "unit": option},
        )

        # Update our own entity state
        self.async_write_ha_state()


class KegDeviceSelect(SelectEntity):
    """Select entity listing keg IDs from /api/kegs/devices or seen via data."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        name: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_device"
        self._options: list[str] = []
        self._current: str | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_devices")},
            name="Beer Keg Devices",
            manufacturer="Beer Keg",
            model="Keg Scale",
        )

    @property
    def options(self) -> list[str]:
        return self._options

    @property
    def current_option(self) -> str | None:
        return self._current

    async def async_added_to_hass(self) -> None:
        """Subscribe to device updates and initialize from state."""
        # Initial load
        self._refresh_from_state()

        # Listen for /api/kegs/devices updates
        self.async_on_remove(
            self.hass.bus.async_listen(DEVICES_UPDATE_EVENT, self._handle_devices_update)
        )

    @callback
    def _handle_devices_update(self, event) -> None:
        """Handle devices list changes from __init__.py."""
        self._refresh_from_state()
        self.async_write_ha_state()

    def _refresh_from_state(self) -> None:
        """Sync options and current selection from integration state."""
        state: Dict[str, Any] = self.hass.data[DOMAIN][self.entry.entry_id]

        # Devices discovered by fetch_devices()
        devices: list[str] = state.get("devices") or []

        # Also include any keg IDs from data in case /devices is missing
        data = state.get("data") or {}
        for keg_id in data.keys():
            if keg_id not in devices:
                devices.append(keg_id)

        devices = sorted(set(devices))
        self._options = devices

        # Current selection from state (if present)
        selected = state.get("selected_device")
        if selected in devices:
            self._current = selected
        else:
            # Default to first device if list is non-empty
            self._current = devices[0] if devices else None

    async def async_select_option(self, option: str) -> None:
        """Handle user selecting a keg ID."""
        if option not in self._options:
            _LOGGER.warning("%s: invalid device option %s", DOMAIN, option)
            return

        state: Dict[str, Any] = self.hass.data[DOMAIN][self.entry.entry_id]
        state["selected_device"] = option
        self._current = option

        _LOGGER.info("%s: selected keg device %s", DOMAIN, option)

        # Fire a generic event so scripts/automations can react if they want
        self.hass.bus.async_fire(
            f"{DOMAIN}_device_selected",
            {"id": option},
        )

        self.async_write_ha_state()
