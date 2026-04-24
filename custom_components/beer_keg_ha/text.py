from __future__ import annotations

import logging
from typing import Any, Dict, List, Set

import aiohttp

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PLATFORM_EVENT, AIRLOCK_EVENT

_LOGGER = logging.getLogger(__name__)

# -------------------------------------------------------------------
# PER-KEG TEXT FIELDS (name / Beer SG / OG)
# -------------------------------------------------------------------

TEXT_TYPES: Dict[str, Dict[str, Any]] = {
    "name": {
        "key": "name",
        "name": "Keg Name",
        "min": 0,
        "max": 64,
    },
    "beer_sg": {
        "key": "beer_sg",
        "name": "Beer SG",
        "min": 0,
        "max": 16,
    },
    "original_gravity": {
        "key": "original_gravity",
        "name": "Original Gravity",
        "min": 0,
        "max": 16,
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
    """Set up per-keg text entities."""
    state = hass.data[DOMAIN][entry.entry_id]
    created: Set[str] = state.setdefault("created_text_kegs", set())

    entities: List[TextEntity] = []

    # ---------- Per-keg text entities ----------
    def create_for_keg(keg_id: str) -> None:
        if keg_id in created:
            return
        for text_type in TEXT_TYPES.keys():
            entities.append(BeerKegTextEntity(hass, entry, keg_id, text_type))
        created.add(keg_id)

    for keg_id in list(state.get("data", {}).keys()):
        create_for_keg(keg_id)

    # Add everything we have so far
    async_add_entities(entities, True)

    # When new kegs appear, create per-keg text entities for them
    @callback
    def _on_update(event) -> None:
        keg_id = (event.data or {}).get("keg_id")
        if keg_id and keg_id not in created:
            new_ents: List[TextEntity] = []
            for text_type in TEXT_TYPES.keys():
                new_ents.append(BeerKegTextEntity(hass, entry, keg_id, text_type))
            created.add(keg_id)
            async_add_entities(new_ents, True)

    entry.async_on_unload(
        hass.bus.async_listen(PLATFORM_EVENT, _on_update)
    )

    # Airlock text entities
    created_airlocks: Set[str] = state.setdefault("created_airlocks_text", set())

    def create_airlock_text_for(airlock_id: str) -> None:
        if airlock_id in created_airlocks:
            return
        new_ents: List[TextEntity] = [
            AirlockTextEntity(hass, entry, airlock_id, field_key)
            for field_key in AIRLOCK_TEXT_TYPES.keys()
        ]
        created_airlocks.add(airlock_id)
        async_add_entities(new_ents, True)

    for airlock_id in list(state.get("airlock_data", {}).keys()):
        create_airlock_text_for(airlock_id)

    @callback
    def _on_airlock_update(event) -> None:
        airlock_id = (event.data or {}).get("airlock_id")
        if airlock_id:
            create_airlock_text_for(airlock_id)

    entry.async_on_unload(
        hass.bus.async_listen(AIRLOCK_EVENT, _on_airlock_update)
    )


# -------------------------------------------------------------------
# PER-KEG TEXT ENTITY
# -------------------------------------------------------------------

class BeerKegTextEntity(TextEntity):
    """Per-keg text entity backed by integration state + prefs_store."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        keg_id: str,
        text_type: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.keg_id = keg_id
        self.text_type = text_type

        self._state_ref: Dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
        meta = TEXT_TYPES[text_type]
        self._key = meta["key"]

        short_id = keg_id[:4]

        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{keg_id}_text_{text_type}"
        self._attr_name = f"Keg {short_id} {meta['name']}"
        self._attr_mode = "text"
        self._attr_min = meta["min"]
        self._attr_max = meta["max"]

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
    def native_value(self) -> str | None:
        """Return current text from keg_config (prefs), falling back to data."""
        domain_state = self._state_ref
        keg_cfg: Dict[str, Dict[str, Any]] = domain_state.setdefault("keg_config", {})
        cfg = keg_cfg.get(self.keg_id, {})

        if self._key == "name":
            # name: prefer config; fall back to live data; else keg_id
            if "name" in cfg and cfg["name"]:
                return str(cfg["name"])
            data = domain_state.get("data", {}).get(self.keg_id, {})
            if data.get("name"):
                return str(data["name"])
            return self.keg_id

        # SG / OG: just whatever is stored in config
        val = cfg.get(self._key)
        if val is None:
            return ""
        return str(val)

    async def async_set_value(self, value: str) -> None:
        """Update config, persist via prefs_store, and nudge sensors."""
        domain_state = self._state_ref
        keg_cfg: Dict[str, Dict[str, Any]] = domain_state.setdefault("keg_config", {})
        cfg = keg_cfg.setdefault(self.keg_id, {})

        # Simple bounds trimming
        meta = TEXT_TYPES[self.text_type]
        if value is None:
            value = ""
        value = str(value)
        if meta["max"] and len(value) > meta["max"]:
            value = value[: meta["max"]]

        cfg[self._key] = value

        # Mirror into data dict for convenience
        data = domain_state.setdefault("data", {})
        keg_data = data.setdefault(self.keg_id, {})
        keg_data[self._key] = value

        # Persist (along with display_units + smoothing settings)
        prefs_store = domain_state.get("prefs_store")
        if prefs_store is not None:
            await prefs_store.async_save(
                {
                    "display_units": domain_state.get("display_units", {}),
                    "keg_config": keg_cfg,
                    "noise_deadband_kg": domain_state.get("noise_deadband_kg"),
                    "smoothing_alpha": domain_state.get("smoothing_alpha"),
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


# -------------------------------------------------------------------
# AIRLOCK TEXT ENTITIES
# -------------------------------------------------------------------

AIRLOCK_TEXT_TYPES: Dict[str, Dict[str, Any]] = {
    "grainfather_url": {
        "name": "Grainfather URL",
        "key": "grainfather_url",
        "max": 512,
        "icon": "mdi:link",
        "endpoint": "grainfather",
    },
    "brewfather_url": {
        "name": "Brewfather URL",
        "key": "brewfather_url",
        "max": 512,
        "icon": "mdi:link",
        "endpoint": "brewfather",
    },
    "brewfather_og": {
        "name": "Brewfather OG",
        "key": "brewfather_og",
        "max": 16,
        "icon": "mdi:alpha-o-circle",
        "endpoint": "brewfather",
    },
    "brewfather_batch_volume": {
        "name": "Brewfather Batch Volume (L)",
        "key": "brewfather_batch_volume",
        "max": 16,
        "icon": "mdi:barrel",
        "endpoint": "brewfather",
    },
}


class AirlockTextEntity(TextEntity):
    _attr_should_poll = False
    _attr_mode = "text"
    _attr_min = 0

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        airlock_id: str,
        field_key: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.airlock_id = airlock_id
        self.field_key = field_key
        self._meta = AIRLOCK_TEXT_TYPES[field_key]
        self._state_ref: Dict[str, Any] = hass.data[DOMAIN][entry.entry_id]

        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_airlock_{airlock_id}_{field_key}"
        self._attr_name = f"Airlock {airlock_id} {self._meta['name']}"
        self._attr_icon = self._meta.get("icon")
        self._attr_max = self._meta["max"]

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_airlock_{self.airlock_id}")},
            name=f"Airlock {self.airlock_id}",
            manufacturer="open-plaato-keg",
            model="Plaato Airlock",
        )

    @property
    def native_value(self) -> str:
        d = self._state_ref.get("airlock_data", {}).get(self.airlock_id, {})
        val = d.get(self._meta["key"])
        return str(val) if val is not None else ""

    async def async_set_value(self, value: str) -> None:
        a = self._state_ref.get("airlock_data", {}).get(self.airlock_id, {})
        endpoint = self._meta["endpoint"]

        if endpoint == "grainfather":
            body = {
                "enabled": a.get("grainfather_enabled", False),
                "unit": a.get("grainfather_unit") or "celsius",
                "specific_gravity": a.get("grainfather_sg") or 1.0,
                "url": a.get("grainfather_url") or "",
            }
            body[self._meta["key"]] = value
            # map field key -> server param name
            if self.field_key == "grainfather_url":
                body["url"] = value
        else:  # brewfather
            body: Dict[str, Any] = {
                "enabled": a.get("brewfather_enabled", False),
                "unit": a.get("brewfather_temp_unit") or "celsius",
                "specific_gravity": a.get("brewfather_sg") or 1.0,
                "url": a.get("brewfather_url") or "",
            }
            if a.get("brewfather_og") is not None:
                body["og"] = a["brewfather_og"]
            if a.get("brewfather_batch_volume") is not None:
                body["batch_volume"] = a["brewfather_batch_volume"]
            # override the specific field being changed
            if self.field_key == "brewfather_url":
                body["url"] = value
            elif self.field_key == "brewfather_og":
                body["og"] = value
            elif self.field_key == "brewfather_batch_volume":
                body["batch_volume"] = value

        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        base = self._state_ref.get("rest_base", "")
        url = f"{base}/api/airlocks/{self.airlock_id}/{endpoint}"
        try:
            session = async_get_clientsession(self.hass)
            async with session.post(url, json=body) as resp:
                if resp.status not in range(200, 300):
                    _LOGGER.warning("AirlockTextEntity POST %s returned %s", url, resp.status)
        except aiohttp.ClientError as err:
            _LOGGER.error("AirlockTextEntity POST %s failed: %s", url, err)

        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.hass.data[DOMAIN][self.entry.entry_id].setdefault("registered_unique_ids", set()).add(self._attr_unique_id)
        self.async_on_remove(self.hass.bus.async_listen(AIRLOCK_EVENT, self._on_event))

    @callback
    def _on_event(self, event) -> None:
        if (event.data or {}).get("airlock_id") == self.airlock_id:
            self.async_write_ha_state()
