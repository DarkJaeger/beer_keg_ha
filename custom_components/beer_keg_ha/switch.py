from __future__ import annotations

import logging
from typing import Any, Dict, List, Set

import aiohttp

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, AIRLOCK_EVENT

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data[DOMAIN][entry.entry_id]
    created: Set[str] = state.setdefault("created_airlocks_switch", set())

    def create_for(airlock_id: str) -> None:
        if airlock_id in created:
            return
        async_add_entities([
            AirlockGrainfatherSwitch(hass, entry, airlock_id),
            AirlockBrewfatherSwitch(hass, entry, airlock_id),
        ], True)
        created.add(airlock_id)

    for airlock_id in list(state.get("airlock_data", {}).keys()):
        create_for(airlock_id)

    @callback
    def _on_airlock_update(event) -> None:
        airlock_id = (event.data or {}).get("airlock_id")
        if airlock_id:
            create_for(airlock_id)

    entry.async_on_unload(hass.bus.async_listen(AIRLOCK_EVENT, _on_airlock_update))


class _AirlockSwitchBase(SwitchEntity):
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, airlock_id: str) -> None:
        self.hass = hass
        self.entry = entry
        self.airlock_id = airlock_id
        self._state_ref: Dict[str, Any] = hass.data[DOMAIN][entry.entry_id]

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

    async def _post(self, path: str, body: Dict[str, Any]) -> None:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        base = self._state_ref.get("rest_base", "")
        url = f"{base}{path}"
        try:
            session = async_get_clientsession(self.hass)
            async with session.post(url, json=body) as resp:
                if resp.status not in range(200, 300):
                    _LOGGER.warning("Airlock switch POST %s returned %s", url, resp.status)
        except aiohttp.ClientError as err:
            _LOGGER.error("Airlock switch POST %s failed: %s", url, err)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.hass.bus.async_listen(AIRLOCK_EVENT, self._on_event))

    @callback
    def _on_event(self, event) -> None:
        if (event.data or {}).get("airlock_id") == self.airlock_id:
            self.async_write_ha_state()


class AirlockGrainfatherSwitch(_AirlockSwitchBase):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, airlock_id: str) -> None:
        super().__init__(hass, entry, airlock_id)
        self._attr_name = f"Airlock {airlock_id} Grainfather Enabled"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_airlock_{airlock_id}_grainfather_enabled"
        self._attr_icon = "mdi:beer"

    @property
    def is_on(self) -> bool:
        return bool(self._airlock().get("grainfather_enabled", False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._post_grainfather(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._post_grainfather(enabled=False)

    async def _post_grainfather(self, enabled: bool) -> None:
        a = self._airlock()
        body = {
            "enabled": enabled,
            "unit": a.get("grainfather_unit") or "celsius",
            "specific_gravity": a.get("grainfather_sg") or 1.0,
            "url": a.get("grainfather_url") or "",
        }
        await self._post(f"/api/airlocks/{self.airlock_id}/grainfather", body)


class AirlockBrewfatherSwitch(_AirlockSwitchBase):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, airlock_id: str) -> None:
        super().__init__(hass, entry, airlock_id)
        self._attr_name = f"Airlock {airlock_id} Brewfather Enabled"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_airlock_{airlock_id}_brewfather_enabled"
        self._attr_icon = "mdi:cup"

    @property
    def is_on(self) -> bool:
        return bool(self._airlock().get("brewfather_enabled", False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._post_brewfather(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._post_brewfather(enabled=False)

    async def _post_brewfather(self, enabled: bool) -> None:
        a = self._airlock()
        body: Dict[str, Any] = {
            "enabled": enabled,
            "unit": a.get("brewfather_temp_unit") or "celsius",
            "specific_gravity": a.get("brewfather_sg") or 1.0,
            "url": a.get("brewfather_url") or "",
        }
        if a.get("brewfather_og") is not None:
            body["og"] = a["brewfather_og"]
        if a.get("brewfather_batch_volume") is not None:
            body["batch_volume"] = a["brewfather_batch_volume"]
        await self._post(f"/api/airlocks/{self.airlock_id}/brewfather", body)
