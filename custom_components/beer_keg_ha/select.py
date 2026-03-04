from __future__ import annotations

import logging
from typing import Any, Dict, List, Set

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

import aiohttp

from .const import DOMAIN, PLATFORM_EVENT, DEVICES_UPDATE_EVENT, AIRLOCK_EVENT

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

    # Airlock temp unit selects
    created: Set[str] = state.setdefault("created_airlocks_select", set())

    def create_airlock_selects_for(airlock_id: str) -> None:
        if airlock_id in created:
            return
        async_add_entities([
            AirlockTempUnitSelect(hass, entry, airlock_id, "grainfather"),
            AirlockTempUnitSelect(hass, entry, airlock_id, "brewfather"),
        ], True)
        created.add(airlock_id)

    for airlock_id in list(state.get("airlock_data", {}).keys()):
        create_airlock_selects_for(airlock_id)

    @callback
    def _on_airlock_update(event) -> None:
        airlock_id = (event.data or {}).get("airlock_id")
        if airlock_id:
            create_airlock_selects_for(airlock_id)

    entry.async_on_unload(hass.bus.async_listen(AIRLOCK_EVENT, _on_airlock_update))


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


class AirlockTempUnitSelect(SelectEntity):
    """Select entity for Grainfather or Brewfather temperature unit per airlock."""

    _attr_should_poll = False
    _attr_options = ["celsius", "fahrenheit"]

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        airlock_id: str,
        integration: str,  # "grainfather" or "brewfather"
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.airlock_id = airlock_id
        self.integration = integration
        self._state_ref: Dict[str, Any] = hass.data[DOMAIN][entry.entry_id]

        self._attr_name = f"Airlock {airlock_id} {integration.title()} Temp Unit"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_airlock_{airlock_id}_{integration}_temp_unit"
        self._attr_icon = "mdi:thermometer"

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
    def current_option(self) -> str:
        a = self._airlock()
        if self.integration == "grainfather":
            return a.get("grainfather_unit") or "celsius"
        return a.get("brewfather_temp_unit") or "celsius"

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            return
        a = self._airlock()
        base = self._state_ref.get("rest_base", "")

        if self.integration == "grainfather":
            body = {
                "enabled": a.get("grainfather_enabled", False),
                "unit": option,
                "specific_gravity": a.get("grainfather_sg") or 1.0,
                "url": a.get("grainfather_url") or "",
            }
            endpoint = "grainfather"
        else:
            body: Dict[str, Any] = {
                "enabled": a.get("brewfather_enabled", False),
                "unit": option,
                "specific_gravity": a.get("brewfather_sg") or 1.0,
                "url": a.get("brewfather_url") or "",
            }
            if a.get("brewfather_og") is not None:
                body["og"] = a["brewfather_og"]
            if a.get("brewfather_batch_volume") is not None:
                body["batch_volume"] = a["brewfather_batch_volume"]
            endpoint = "brewfather"

        url = f"{base}/api/airlocks/{self.airlock_id}/{endpoint}"
        try:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession
            session = async_get_clientsession(self.hass)
            async with session.post(url, json=body) as resp:
                if resp.status not in range(200, 300):
                    _LOGGER.warning("AirlockTempUnitSelect POST %s returned %s", url, resp.status)
        except aiohttp.ClientError as err:
            _LOGGER.error("AirlockTempUnitSelect POST %s failed: %s", url, err)

        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.hass.bus.async_listen(AIRLOCK_EVENT, self._on_event))

    @callback
    def _on_event(self, event) -> None:
        if (event.data or {}).get("airlock_id") == self.airlock_id:
            self.async_write_ha_state()
