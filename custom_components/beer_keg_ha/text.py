from __future__ import annotations

import logging
from typing import Any, Dict, List, Set

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORM_EVENT = f"{DOMAIN}_update"

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
# TAP LIST TEXT FIELDS (Tap 1–12: name/style/OG/FG/notes)
# -------------------------------------------------------------------

TAP_COUNT = 12

TAP_TEXT_FIELDS: Dict[str, Dict[str, Any]] = {
    "name": {
        "label": "Tap Name",
        "max": 64,
        "icon": "mdi:beer",
    },
    "style": {
        "label": "Style",
        "max": 64,
        "icon": "mdi:beer-outline",
    },
    "og": {
        "label": "OG",
        "max": 16,
        "icon": "mdi:alpha-o-circle",
    },
    "fg": {
        "label": "FG",
        "max": 16,
        "icon": "mdi:alpha-f-circle",
    },
    "notes": {
        "label": "Notes",
        # Full text stored here (we’ll keep state <= 255 chars)
        "max": 4096,
        "icon": "mdi:note-text",
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
    """Set up per-keg text + tap-list text entities."""
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

    # ---------- Tap-list text entities ----------
    await async_setup_tap_text(hass, entry, async_add_entities, entities)

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

        # Persist (along with display_units + tap config + smoothing settings)
        prefs_store = domain_state.get("prefs_store")
        if prefs_store is not None:
            await prefs_store.async_save(
                {
                    "display_units": domain_state.get("display_units", {}),
                    "keg_config": keg_cfg,
                    "tap_text": domain_state.get("tap_text", {}),
                    "tap_numbers": domain_state.get("tap_numbers", {}),
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

        @callback
        def _handle_update(event) -> None:
            if (event.data or {}).get("keg_id") == self.keg_id:
                self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(PLATFORM_EVENT, _handle_update)
        )


# -------------------------------------------------------------------
# TAP LIST TEXT ENTITIES
# -------------------------------------------------------------------

async def async_setup_tap_text(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    entities: List[TextEntity],
) -> None:
    """Create text entities for taps (1..TAP_COUNT)."""
    state = hass.data[DOMAIN][entry.entry_id]
    tapstore = state.setdefault("tap_text", {})

    for tap in range(1, TAP_COUNT + 1):
        tap_id = f"tap_{tap}"  # e.g. "tap_1", "tap_2" ...
        tapstore.setdefault(tap_id, {})

        for key, meta in TAP_TEXT_FIELDS.items():
            entities.append(BeerTapTextEntity(hass, entry, tap_id, key, meta))


class BeerTapTextEntity(TextEntity):
    """Text entity for tap list fields: name/style/OG/FG/long notes."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tap_id: str,
        field_key: str,
        meta: Dict[str, Any],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.tap_id = tap_id            # "tap_1"
        self.field_key = field_key      # "name", "style", "og", "fg", "notes"
        self.meta = meta

        # tap_text structure is maintained in __init__.py + here
        self.state_ref: Dict[str, Dict[str, Any]] = hass.data[DOMAIN][entry.entry_id]["tap_text"]

        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{tap_id}_{field_key}"
        self._attr_name = f"{tap_id.upper()} {meta['label']}"
        self._attr_icon = meta.get("icon")
        self._attr_mode = "text"
        self._attr_min = 0
        # HA core only supports state up to 255 chars; we keep full text separately.
        self._attr_max = min(meta.get("max", 255), 255)

    @property
    def device_info(self) -> DeviceInfo:
        """Group tap text under a single 'Beer Tap List' device."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_taplist")},
            name="Beer Tap List",
            manufacturer="Beer Keg",
        )

    # ---- Core value: short preview for Home Assistant state ----

    @property
    def native_value(self) -> str:
        """Return a <=255 char preview; full text is in attributes.full_value."""
        tap_cfg = self.state_ref.get(self.tap_id, {})
        full_val = str(tap_cfg.get(self.field_key, "") or "")

        # state preview (respect 255-char core limit)
        return full_val[:255]

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Expose full_value so Lovelace cards can render the entire text."""
        tap_cfg = self.state_ref.get(self.tap_id, {})
        full_val = str(tap_cfg.get(self.field_key, "") or "")
        return {
            "full_value": full_val,
        }

    async def async_set_value(self, value: str) -> None:
        """
        Store full text (up to meta['max']) in tap_text, but keep
        the entity state to a 255-char preview to avoid HA truncation
        warnings.
        """
        domain_state = self.hass.data[DOMAIN][self.entry.entry_id]
        tap_cfg = self.state_ref.setdefault(self.tap_id, {})

        if value is None:
            value = ""
        value = str(value)

        max_len = self.meta.get("max", 4096)
        if max_len and len(value) > max_len:
            value = value[:max_len]

        tap_cfg[self.field_key] = value

        # Persist everything (same bundle used by numbers/select/text)
        prefs = domain_state.get("prefs_store")
        if prefs:
            await prefs.async_save(
                {
                    "display_units": domain_state.get("display_units", {}),
                    "keg_config": domain_state.get("keg_config", {}),
                    "tap_text": domain_state.get("tap_text", {}),
                    "tap_numbers": domain_state.get("tap_numbers", {}),
                    "noise_deadband_kg": domain_state.get("noise_deadband_kg"),
                    "smoothing_alpha": domain_state.get("smoothing_alpha"),
                }
            )

        # Nudge UI; we reuse PLATFORM_EVENT for simplicity
        self.hass.bus.async_fire(
            PLATFORM_EVENT,
            {"tap": self.tap_id},
        )
        self.async_write_ha_state()
