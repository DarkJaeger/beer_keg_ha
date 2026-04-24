from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Set

from homeassistant.components.date import DateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORM_EVENT = f"{DOMAIN}_update"

# Per-keg date fields:
#  - kegged_date
#  - expiration_date
DATE_TYPES: Dict[str, Dict[str, Any]] = {
    "kegged_date": {
        "key": "kegged_date",
        "name": "Kegged Date",
    },
    "expiration_date": {
        "key": "expiration_date",
        "name": "Expiration Date",
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up keg date entities."""
    state = hass.data[DOMAIN][entry.entry_id]
    created: Set[str] = state.setdefault("created_date_kegs", set())

    entities: List[DateEntity] = []

    def create_for(keg_id: str) -> None:
        if keg_id in created:
            return
        for date_type in DATE_TYPES.keys():
            entities.append(BeerKegDateEntity(hass, entry, keg_id, date_type))
        created.add(keg_id)

    for keg_id in list(state.get("data", {}).keys()):
        create_for(keg_id)

    async_add_entities(entities, True)

    @callback
    def _on_update(event) -> None:
        keg_id = (event.data or {}).get("keg_id")
        if keg_id and keg_id not in created:
            new_ents: List[DateEntity] = []
            for date_type in DATE_TYPES.keys():
                new_ents.append(BeerKegDateEntity(hass, entry, keg_id, date_type))
            created.add(keg_id)
            async_add_entities(new_ents, True)

    entry.async_on_unload(
        hass.bus.async_listen(PLATFORM_EVENT, _on_update)
    )


class BeerKegDateEntity(DateEntity):
    """Per-keg date entity backed by keg_config + prefs_store."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        keg_id: str,
        date_type: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.keg_id = keg_id
        self.date_type = date_type

        self._state_ref: Dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
        meta = DATE_TYPES[date_type]
        self._key = meta["key"]

        short_id = keg_id[:4]

        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{keg_id}_date_{date_type}"
        self._attr_name = f"Keg {short_id} {meta['name']}"

    @property
    def device_info(self) -> DeviceInfo:
        short_id = self.keg_id[:4]
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_{self.keg_id}")},
            name=f"Beer Keg {short_id}",
            manufacturer="Beer Keg",
            model="WebSocket + REST",
        )

    @property
    def native_value(self) -> date | None:
        """Return a Python date from stored ISO string."""
        domain_state = self._state_ref
        keg_cfg: Dict[str, Dict[str, Any]] = domain_state.setdefault("keg_config", {})
        cfg = keg_cfg.get(self.keg_id, {})
        raw = cfg.get(self._key)
        if not raw:
            return None
        try:
            return date.fromisoformat(str(raw))
        except Exception:
            return None

    async def async_set_value(self, value: date | None) -> None:
        """Store ISO date string in keg_config and persist."""
        domain_state = self._state_ref
        keg_cfg: Dict[str, Dict[str, Any]] = domain_state.setdefault("keg_config", {})
        cfg = keg_cfg.setdefault(self.keg_id, {})

        if value is None:
            cfg[self._key] = None
        else:
            cfg[self._key] = value.isoformat()

        # Mirror into data dict
        data = domain_state.setdefault("data", {})
        keg_data = data.setdefault(self.keg_id, {})
        keg_data[self._key] = cfg[self._key]

        # Persist with prefs_store
        prefs_store = domain_state.get("prefs_store")
        if prefs_store is not None:
            await prefs_store.async_save(
                {
                    "display_units": domain_state.get("display_units", {}),
                    "keg_config": keg_cfg,
                }
            )

        # Nudge sensors/cards
        self.hass.bus.async_fire(
            PLATFORM_EVENT,
            {"keg_id": self.keg_id},
        )
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Refresh when this keg is updated elsewhere."""
        self.hass.data[DOMAIN][self.entry.entry_id].setdefault("registered_unique_ids", set()).add(self._attr_unique_id)

        @callback
        def _handle_update(event) -> None:
            if (event.data or {}).get("keg_id") == self.keg_id:
                self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(PLATFORM_EVENT, _handle_update)
        )
