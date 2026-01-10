from __future__ import annotations

import logging
from typing import Any, Dict, List

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORM_EVENT = f"{DOMAIN}_update"
DEVICES_UPDATE_EVENT = f"{DOMAIN}_devices_update"

# -------------------------------------------------------------------
# GLOBAL UNIT SELECTS
# -------------------------------------------------------------------

UNIT_SELECTS: Dict[str, Dict[str, Any]] = {
    "weight": {
        "name": "Keg Weight Unit",
        "options": ["kg", "lb"],
    },
    "temp": {
        "name": "Keg Temperature Unit",
        "options": ["°C", "°F"],
    },
    "pour": {
        "name": "Keg Volume Unit",
        "options": ["oz", "ml"],
    },
}

# (Metadata for the per-keg smoothing select – not strictly required,
#  but kept here for clarity.)
KEG_SELECT_TYPES: Dict[str, Dict[str, Any]] = {
    "smoothing_mode": {
        "key": "disable_smoothing",
        "name": "Smoothing Mode",
        "options": ["Normal", "Off"],
    },
}


# -------------------------------------------------------------------
# SETUP
# -------------------------------------------------------------------

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up device + unit selects + per-keg smoothing selects."""
    state = hass.data[DOMAIN][entry.entry_id]

    entities: List[SelectEntity] = []

    # 1) Device selector (select.keg_device)
    entities.append(BeerKegDeviceSelect(hass, entry, state))

    # 2) Global unit selectors (weight / temp / pour)
    for unit_kind in UNIT_SELECTS.keys():
        entities.append(BeerKegUnitSelect(hass, entry, unit_kind))

    # 3) Per-keg smoothing select (Normal / Off)
    created = state.setdefault("created_select_kegs", set())

    def create_for(keg_id: str) -> None:
        if keg_id in created:
            return
        entities.append(BeerKegSmoothingSelect(hass, entry, keg_id))
        created.add(keg_id)

    # Existing kegs
    for keg_id in list(state.get("data", {}).keys()):
        create_for(keg_id)

    # Add initial entities (device + units + any existing keg smoothing selects)
    async_add_entities(entities, True)

    # When new kegs appear, create smoothing selects for them
    @callback
    def _on_update(event) -> None:
        keg_id = (event.data or {}).get("keg_id")
        if not keg_id or keg_id in created:
            return

        new_ent = BeerKegSmoothingSelect(hass, entry, keg_id)
        created.add(keg_id)
        async_add_entities([new_ent], True)

    entry.async_on_unload(
        hass.bus.async_listen(PLATFORM_EVENT, _on_update)
    )


# -------------------------------------------------------------------
# DEVICE SELECT (select.keg_device)
# -------------------------------------------------------------------

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

        # Cache options so we only update capabilities when the list changes
        self._attr_options: list[str] = []
        self._last_options: list[str] = []

        if "selected_device" not in self._state_ref:
            self._state_ref["selected_device"] = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_settings")},
            name="Beer Keg Settings",
            manufacturer="Beer Keg",
            model="WebSocket + REST",
        )

    def _read_devices(self) -> list[str]:
        devices = self._state_ref.get("devices") or []
        # normalize + stable ordering to prevent needless "changed"
        cleaned = [str(d) for d in devices if d not in (None, "", "unknown", "unavailable")]
        # keep stable order but de-dupe
        seen = set()
        out: list[str] = []
        for d in cleaned:
            if d not in seen:
                seen.add(d)
                out.append(d)
        return out

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
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Refresh when devices list changes, but only if it *actually* changed."""
        # Initialize options once
        new_opts = self._read_devices()
        self._attr_options = new_opts
        self._last_options = list(new_opts)

        # If no selected device yet, pick first
        if self._state_ref.get("selected_device") not in self._attr_options:
            self._state_ref["selected_device"] = self._attr_options[0] if self._attr_options else None

        self.async_write_ha_state()

        @callback
        def _handle_devices_update(event) -> None:
            new = self._read_devices()

            # Only update capabilities if changed
            if new == self._last_options:
                return

            self._attr_options = new
            self._last_options = list(new)

            # Ensure selection stays valid
            if self._state_ref.get("selected_device") not in self._attr_options:
                self._state_ref["selected_device"] = self._attr_options[0] if self._attr_options else None

            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(DEVICES_UPDATE_EVENT, _handle_devices_update)
        )

# -------------------------------------------------------------------
# GLOBAL UNIT SELECTS
# -------------------------------------------------------------------

class BeerKegUnitSelect(SelectEntity):
    """Global select entity to control display units (weight/temp/pour)."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        unit_kind: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._unit_kind = unit_kind  # "weight", "temp", or "pour"

        meta = UNIT_SELECTS[unit_kind]

        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_unit_{unit_kind}"
        self._attr_name = meta["name"]
        self._attr_options = meta["options"]

    @property
    def device_info(self) -> DeviceInfo:
        """Group unit selects under a shared 'Beer Keg Settings' device."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_settings")},
            name="Beer Keg Settings",
            manufacturer="Beer Keg",
            model="WebSocket + REST",
        )

    @property
    def current_option(self) -> str | None:
        """Return the currently selected option based on integration state."""
        domain_state = self.hass.data[DOMAIN][self.entry.entry_id]
        du = domain_state.get("display_units", {})

        if self._unit_kind == "weight":
            val = du.get("weight", "kg")
            return val if val in UNIT_SELECTS["weight"]["options"] else "kg"

        if self._unit_kind == "temp":
            val = du.get("temp", "°C")
            return val if val in UNIT_SELECTS["temp"]["options"] else "°C"

        # pour / volume
        val = du.get("pour", "oz")
        return val if val in UNIT_SELECTS["pour"]["options"] else "oz"

    async def async_select_option(self, option: str) -> None:
        """Handle user selecting a new option."""
        domain_state = self.hass.data[DOMAIN][self.entry.entry_id]

        if option not in self._attr_options:
            _LOGGER.warning(
                "%s: Invalid option '%s' for %s",
                DOMAIN,
                option,
                self._unit_kind,
            )
            return

        du = domain_state.setdefault("display_units", {})

        if self._unit_kind == "weight":
            du["weight"] = option
        elif self._unit_kind == "temp":
            du["temp"] = option
        else:  # pour
            du["pour"] = option

        # Persist preferences if prefs_store exists
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

        # Notify all keg sensors so they recalc units
        for keg_id in list(domain_state.get("data", {}).keys()):
            self.hass.bus.async_fire(
                PLATFORM_EVENT,
                {"keg_id": keg_id},
            )

        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Refresh when integration fires update events."""

        @callback
        def _handle_update(event) -> None:
            # If display units change from somewhere else, refresh our select
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(PLATFORM_EVENT, _handle_update)
        )


# -------------------------------------------------------------------
# PER-KEG SMOOTHING SELECT
# -------------------------------------------------------------------

class BeerKegSmoothingSelect(SelectEntity):
    """Per-keg select to turn smoothing on/off."""

    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, keg_id: str) -> None:
        self.hass = hass
        self.entry = entry
        self.keg_id = keg_id
        self._state_ref = hass.data[DOMAIN][entry.entry_id]

        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{keg_id}_smoothing_mode"
        self._attr_name = f"Keg {keg_id[:4]} Smoothing"
        self._attr_options = ["Normal", "Off"]

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_{self.keg_id}")},
            name=f"Beer Keg {self.keg_id[:4]}",
            manufacturer="Beer Keg",
        )

    @property
    def current_option(self) -> str:
        keg_cfg_all: Dict[str, Dict[str, Any]] = self._state_ref.setdefault(
            "keg_config", {}
        )
        keg_cfg: Dict[str, Any] = keg_cfg_all.get(self.keg_id, {})
        disable = bool(keg_cfg.get("disable_smoothing", False))
        return "Off" if disable else "Normal"

    async def async_select_option(self, option: str) -> None:
        keg_cfg_all: Dict[str, Dict[str, Any]] = self._state_ref.setdefault(
            "keg_config", {}
        )
        keg_cfg: Dict[str, Any] = keg_cfg_all.setdefault(self.keg_id, {})

        # True when Off, False when Normal
        keg_cfg["disable_smoothing"] = (option == "Off")

        # persist along with other prefs
        prefs = self._state_ref.get("prefs_store")
        if prefs:
            await prefs.async_save(
                {
                    "display_units": self._state_ref.get("display_units", {}),
                    "keg_config": self._state_ref.get("keg_config", {}),
                    "tap_text": self._state_ref.get("tap_text", {}),
                    "tap_numbers": self._state_ref.get("tap_numbers", {}),
                    "noise_deadband_kg": self._state_ref.get("noise_deadband_kg"),
                    "smoothing_alpha": self._state_ref.get("smoothing_alpha"),
                }
            )

        # nudge that keg so UI updates with new behavior immediately
        self.hass.bus.async_fire(
            PLATFORM_EVENT,
            {"keg_id": self.keg_id},
        )
        self.async_write_ha_state()
