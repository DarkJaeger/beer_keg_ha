from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from urllib.parse import urlparse, urlunparse

import aiohttp
import voluptuous as vol
from homeassistant.components.persistent_notification import async_create as pn_create
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    CONF_WS_URL,
    CONF_EMPTY_WEIGHT,
    CONF_DEFAULT_FULL_WEIGHT,
    CONF_POUR_THRESHOLD,
    CONF_PER_KEG_FULL,
    MAX_LOG_ENTRIES,
    DEFAULT_EMPTY_WEIGHT,
    DEFAULT_FULL_WEIGHT,
    DEFAULT_POUR_THRESHOLD,
    ATTR_PLAATO_API_VERSION,
    ATTR_PLAATO_API_V2,
)

_LOGGER = logging.getLogger(__name__)

# Platforms we load
PLATFORMS = ["sensor", "select", "number"]

# hassfest requirement
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Helper entities for unit selection
WEIGHT_UNIT_ENTITIES = [
    "select.keg_weight_unit",
    "input_select.keg_weight_unit",
]

TEMP_UNIT_ENTITIES = [
    "select.keg_temperature_unit",
    "select.keg_temp_unit",
    "input_select.keg_temp_unit",
]

KG_TO_OZ = 35.274
REST_POLL_SECONDS = 10
WS_PING_SEC = 30
DATA_STALE_SEC = 45
DEVICES_REFRESH_SEC = 60
LAST_UPDATE_KEY = "last_update_ts"

DEVICES_UPDATE_EVENT = f"{DOMAIN}_devices_update"
PLATFORM_EVENT = f"{DOMAIN}_update"

# Optional density keys (const.py provides them in your repo)
try:
    from .const import (
        CONF_FULL_VOLUME_L,
        CONF_BEER_SG,
        DEFAULT_BEER_SG,
        DEFAULT_FULL_VOLUME_L,
        WATER_DENSITY_KG_PER_L,
    )

    DENSITY_AWARE = True
except Exception:
    DENSITY_AWARE = False
    CONF_FULL_VOLUME_L = "full_volume_liters"
    CONF_BEER_SG = "beer_specific_gravity"
    DEFAULT_FULL_VOLUME_L = 19.0
    DEFAULT_BEER_SG = 1.010
    WATER_DENSITY_KG_PER_L = 0.998


def _coerce_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except Exception:
        return default


def _rest_base_from_ws(ws: str) -> str:
    """Build http(s) base URL from ws:// or wss://."""
    u = urlparse(ws)
    scheme = "http" if u.scheme == "ws" else "https" if u.scheme == "wss" else "http"
    return urlunparse((scheme, u.netloc, "", "", "", ""))


def _register_service_once(hass: HomeAssistant, name: str, handler, schema=None) -> None:
    """Register a service only once per HA instance (safe across reloads)."""
    if hass.services.has_service(DOMAIN, name):
        return
    hass.services.async_register(DOMAIN, name, handler, schema=schema)


def _normalize_keg_dict(keg: dict, state: dict) -> dict:
    """Normalize keg dict from WS/REST payloads.

    Backwards compatible:
      - API v1 (legacy): weight, temperature, full_weight, temperature_calibrate
      - API v2 (new): amount_left, percent_of_beer_left, keg_temperature, empty_keg_weight,
                      max_keg_volume, temperature_offset
    """
    keg_id = str(keg.get("id", "unknown")).lower().replace(" ", "_")
    name = keg.get("name") or keg_id

    is_v2 = ("amount_left" in keg) or ("percent_of_beer_left" in keg) or ("keg_temperature" in keg)
    api_version = "v2" if is_v2 else "v1"

    # Temperature
    temp_val = keg.get("keg_temperature") if is_v2 else keg.get("temperature")
    temp = _coerce_float(temp_val) if temp_val is not None else None

    # Empty keg weight from device (v2)
    device_empty_kg = _coerce_float(keg.get("empty_keg_weight"), default=0.0) if is_v2 else None

    # v2 liters remaining (can be negative; clamp)
    liters_left = _coerce_float(keg.get("amount_left"), default=0.0) if is_v2 else None
    if liters_left is not None:
        liters_left = max(0.0, float(liters_left))

    # v2 percent remaining (clamp)
    percent_left = _coerce_float(keg.get("percent_of_beer_left"), default=0.0) if is_v2 else None
    if percent_left is not None:
        percent_left = max(0.0, min(100.0, float(percent_left)))

    # Weight:
    # - v1: use device weight
    # - v2: compute from liters_left + SG + water density, and add device empty weight if present
    if is_v2:
        beer_sg = float(state.get("beer_sg", DEFAULT_BEER_SG))
        liquid_kg = float(liters_left or 0.0) * beer_sg * WATER_DENSITY_KG_PER_L
        weight = float(device_empty_kg or 0.0) + liquid_kg
    else:
        weight = _coerce_float(keg.get("weight"))

    # Full weight:
    # - v1: use device full_weight if present
    # - v2: compute from max_keg_volume + SG + water density (+ empty)
    full_w = _coerce_float(keg.get("full_weight"), default=0.0)
    if is_v2:
        max_vol_l = _coerce_float(keg.get("max_keg_volume"), default=0.0)
        if max_vol_l > 0:
            beer_sg = float(state.get("beer_sg", DEFAULT_BEER_SG))
            full_liquid_kg = max_vol_l * beer_sg * WATER_DENSITY_KG_PER_L
            full_w = float(device_empty_kg or 0.0) + full_liquid_kg

    # Calibration offset key changed name in v2
    temp_cal = keg.get("temperature_offset") if is_v2 else keg.get("temperature_calibrate")

    return {
        "keg_id": keg_id,
        "name": name,
        "weight": weight,
        "temperature": temp,
        "full_weight": full_w if full_w > 0 else None,
        "weight_calibrate": _coerce_float(keg.get("weight_calibrate")),
        "temperature_calibrate": _coerce_float(temp_cal),
        # v2 extras
        "liters_left": liters_left,
        "percent_left": percent_left,
        "device_empty_kg": device_empty_kg,
        ATTR_PLAATO_API_VERSION: api_version,
    }


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    try:
        ws_url: str | None = entry.data.get(CONF_WS_URL)
        if not ws_url:
            _LOGGER.error("%s: Missing ws_url", DOMAIN)
            return False

        opts = entry.options or {}
        empty_weight = float(opts.get(CONF_EMPTY_WEIGHT, DEFAULT_EMPTY_WEIGHT))
        default_full = float(opts.get(CONF_DEFAULT_FULL_WEIGHT, DEFAULT_FULL_WEIGHT))
        pour_threshold = float(opts.get(CONF_POUR_THRESHOLD, DEFAULT_POUR_THRESHOLD))

        # Optional density-aware computed full weight
        if DENSITY_AWARE:
            full_volume_l = float(opts.get(CONF_FULL_VOLUME_L, DEFAULT_FULL_VOLUME_L))
            beer_sg = float(opts.get(CONF_BEER_SG, DEFAULT_BEER_SG))
            computed_full_from_sg = full_volume_l * beer_sg * WATER_DENSITY_KG_PER_L  # kg
        else:
            full_volume_l = DEFAULT_FULL_VOLUME_L
            beer_sg = DEFAULT_BEER_SG
            computed_full_from_sg = None

        # Per-keg full weight overrides (JSON of id->kg)
        per_keg_full: Dict[str, float] = {}
        raw_mapping = opts.get(CONF_PER_KEG_FULL)
        if raw_mapping:
            try:
                per_keg_full = {
                    str(k).lower().replace(" ", "_"): float(v)
                    for k, v in json.loads(raw_mapping).items()
                }
            except Exception as e:
                _LOGGER.warning("%s: Invalid per_keg_full mapping: %s", DOMAIN, e)

        hass.data.setdefault(DOMAIN, {})
        history_store: Store = Store(hass, 1, f"{DOMAIN}_history")
        prefs_store: Store = Store(hass, 1, f"{DOMAIN}_prefs")

        state: Dict[str, Any] = {
            "ws_url": ws_url,
            "empty_weight": empty_weight,
            "default_full": default_full,
            "pour_threshold": pour_threshold,
            "per_keg_full": per_keg_full,
            "full_volume_l": full_volume_l,
            "beer_sg": beer_sg,
            "computed_full_from_sg": computed_full_from_sg,
            "kegs": {},  # runtime per-keg stats
            "data": {},  # values exposed to entities
            "history": [],
            "devices": [],
            "display_units": {  # default; may be overridden by prefs below
                "weight": "kg",
                "temp": "°C",
            },
            "history_store": history_store,
            "prefs_store": prefs_store,
            # Capability flags (auto-detected)
            ATTR_PLAATO_API_V2: False,
            ATTR_PLAATO_API_VERSION: "v1",
            LAST_UPDATE_KEY: None,
            # For platform setup bookkeeping
            "created_kegs": set(),
            "created_select_kegs": set(),
            "selected_device": None,
        }

        hass.data[DOMAIN][entry.entry_id] = state

        # ---- load history from storage
        loaded = await history_store.async_load()
        if isinstance(loaded, list):
            state["history"] = loaded
            _LOGGER.info("%s: Loaded %d pour records", DOMAIN, len(state["history"]))

        # ---- load display_units from prefs (if any)
        prefs = await prefs_store.async_load()
        if isinstance(prefs, dict):
            du = prefs.get("display_units")
            if isinstance(du, dict):
                w = du.get("weight")
                t = du.get("temp")
                if w in ("kg", "lb"):
                    state["display_units"]["weight"] = w
                if t in ("°C", "°F"):
                    state["display_units"]["temp"] = t

            # Keep other prefs if present (avoid breaking select/number/text platforms)
            if isinstance(prefs.get("keg_config"), dict):
                state["keg_config"] = prefs["keg_config"]
            if isinstance(prefs.get("tap_text"), dict):
                state["tap_text"] = prefs["tap_text"]
            if isinstance(prefs.get("tap_numbers"), dict):
                state["tap_numbers"] = prefs["tap_numbers"]
            if "noise_deadband_kg" in prefs:
                state["noise_deadband_kg"] = prefs.get("noise_deadband_kg")
            if "smoothing_alpha" in prefs:
                state["smoothing_alpha"] = prefs.get("smoothing_alpha")

        # ---------- REST helpers (use HA shared session)

        async def fetch_kegs() -> List[Dict[str, Any]]:
            """GET /api/kegs (list or {'kegs':[...]})"""
            base = _rest_base_from_ws(ws_url)
            urls = (f"{base}/api/kegs", f"{base}/api/kegs/")
            http_sess = async_get_clientsession(hass)

            for url in urls:
                try:
                    async with http_sess.get(url) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            _LOGGER.warning(
                                "%s: REST %s -> HTTP %s: %s",
                                DOMAIN,
                                url,
                                resp.status,
                                body[:200],
                            )
                            continue
                        try:
                            data = await resp.json()
                        except Exception as e:
                            body = await resp.text()
                            _LOGGER.warning(
                                "%s: REST %s invalid JSON (%s): %s",
                                DOMAIN,
                                url,
                                e,
                                body[:200],
                            )
                            continue

                        if isinstance(data, list):
                            return data
                        if isinstance(data, dict) and isinstance(data.get("kegs"), list):
                            return data["kegs"]
                except Exception as e:
                    _LOGGER.warning("%s: REST GET failed %s (%s)", DOMAIN, url, e)

            return []

        async def fetch_devices() -> list[str]:
            """GET /api/kegs/devices -> list[str]."""
            base = _rest_base_from_ws(ws_url)
            urls = (f"{base}/api/kegs/devices", f"{base}/api/kegs/devices/")
            http_sess = async_get_clientsession(hass)

            for url in urls:
                try:
                    async with http_sess.get(url) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            _LOGGER.warning(
                                "%s: REST %s -> HTTP %s: %s",
                                DOMAIN,
                                url,
                                resp.status,
                                body[:200],
                            )
                            continue
                        data = await resp.json()
                        if isinstance(data, list):
                            ids = [str(x) for x in data]
                            state["devices"] = ids
                            hass.bus.async_fire(DEVICES_UPDATE_EVENT, {"ids": ids})
                            return ids
                except Exception as e:
                    _LOGGER.debug("%s: REST devices GET failed %s (%s)", DOMAIN, url, e)

            return state.setdefault("devices", [])

        # ---------- publisher

        async def _publish_keg(norm: dict) -> None:
            keg_id = norm["keg_id"]
            weight = float(norm["weight"])
            temp = norm["temperature"]

            # Capability flags
            if norm.get(ATTR_PLAATO_API_VERSION) == "v2":
                state[ATTR_PLAATO_API_V2] = True
                state[ATTR_PLAATO_API_VERSION] = "v2"

            info = state["kegs"].get(keg_id)
            if not info:
                initial_fw = (
                    norm["full_weight"]
                    or state["per_keg_full"].get(keg_id)
                    or state["computed_full_from_sg"]
                    or state["default_full"]
                )
                info = state["kegs"][keg_id] = {
                    "last_weight": weight,
                    "daily_consumed": 0.0,  # oz
                    "last_pour": 0.0,  # oz
                    "last_pour_time": None,
                    "full_weight": float(initial_fw) if initial_fw else float(state["default_full"]),
                }

            # If device later reports a full_weight, adopt it
            if norm["full_weight"] and norm["full_weight"] > 0 and norm["full_weight"] != info["full_weight"]:
                info["full_weight"] = float(norm["full_weight"])

            prev_weight = float(info["last_weight"])
            info["last_weight"] = weight

            # Pour detection (kg -> store oz)
            if prev_weight - weight > float(state["pour_threshold"]):
                delta_kg = round(prev_weight - weight, 2)
                delta_oz = round(delta_kg * KG_TO_OZ, 1)
                info["last_pour"] = delta_oz
                info["last_pour_time"] = datetime.now(timezone.utc)
                info["daily_consumed"] += delta_oz

                state["history"].append(
                    {
                        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z"),
                        "keg": keg_id,
                        "pour_oz": delta_oz,
                        "weight_before_kg": round(prev_weight, 2),
                        "weight_after_kg": round(weight, 2),
                        "temperature_c": temp,
                    }
                )
                if len(state["history"]) > MAX_LOG_ENTRIES:
                    state["history"].pop(0)
                await state["history_store"].async_save(state["history"][-MAX_LOG_ENTRIES:])

            fw = (
                info.get("full_weight")
                or state["per_keg_full"].get(keg_id)
                or state["computed_full_from_sg"]
                or state["default_full"]
            )

            device_ew = norm.get("device_empty_kg")
            ew = float(device_ew) if isinstance(device_ew, (int, float)) and device_ew > 0 else float(state["empty_weight"])

            # liquid-only weight (matches typical server "beer weight" style)
            beer_weight_kg = max(0.0, float(weight) - float(ew))

            w = max(0.0, weight)
            if fw and float(fw) > ew:
                fill_pct = ((w - ew) / (float(fw) - ew)) * 100.0
            else:
                fill_pct = 0.0
            fill_pct = max(0.0, min(100.0, fill_pct))

            state["data"][keg_id] = {
                "id": keg_id,
                "name": norm["name"],
                "weight": round(weight, 2),               # total scale weight
                "beer_weight": round(beer_weight_kg, 2),  # liquid-only weight
                "temperature": round(float(temp), 1) if temp is not None else None,
                "full_weight": round(float(fw), 2) if fw else None,
                "weight_calibrate": norm.get("weight_calibrate"),
                "temperature_calibrate": norm.get("temperature_calibrate"),
                "daily_consumed": round(float(info["daily_consumed"]), 1),
                "last_pour": round(float(info["last_pour"]), 1),
                "fill_percent": round(float(fill_pct), 1),
                # v2 fields
                "liters_left": norm.get("liters_left"),
                "percent_left": norm.get("percent_left"),
                # diagnostics
                ATTR_PLAATO_API_VERSION: state.get(ATTR_PLAATO_API_VERSION, "v1"),
                ATTR_PLAATO_API_V2: bool(state.get(ATTR_PLAATO_API_V2, False)),
            }

            state[LAST_UPDATE_KEY] = datetime.now(timezone.utc)
            _LOGGER.debug("%s: Published %s (api=%s)", DOMAIN, keg_id, state.get(ATTR_PLAATO_API_VERSION))

            # Fallback devices list includes this ID
            if keg_id not in state["devices"]:
                state["devices"].append(keg_id)
                hass.bus.async_fire(DEVICES_UPDATE_EVENT, {"ids": list(state["devices"])})

            hass.bus.async_fire(PLATFORM_EVENT, {"keg_id": keg_id})

        # ---------- WebSocket loop

        async def connect_websocket() -> None:
            http_sess = async_get_clientsession(hass)

            while True:
                try:
                    _LOGGER.info("%s: Connecting WS -> %s", DOMAIN, ws_url)
                    async with http_sess.ws_connect(ws_url) as ws:
                        _LOGGER.info("%s: Connected to WS", DOMAIN)

                        async def _pinger():
                            while True:
                                try:
                                    await ws.ping()
                                except Exception:
                                    break
                                await asyncio.sleep(WS_PING_SEC)

                        hass.async_create_task(_pinger())

                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            try:
                                data = json.loads(msg.data)
                            except json.JSONDecodeError:
                                continue

                            if isinstance(data, list):
                                source = data
                            else:
                                kegs = data.get("kegs")
                                source = kegs if isinstance(kegs, list) else None

                            if not source:
                                continue

                            for raw in source:
                                norm = _normalize_keg_dict(raw, state)
                                await _publish_keg(norm)

                except Exception as e:
                    _LOGGER.error("%s: WS error: %s", DOMAIN, e)
                    await asyncio.sleep(10)

        # ---------- REST poll & watchdog

        async def rest_poll(_now=None) -> None:
            try:
                new_kegs = await fetch_kegs()
                for raw in new_kegs:
                    norm = _normalize_keg_dict(raw, state)
                    await _publish_keg(norm)
            except Exception as e:
                _LOGGER.debug("%s: REST poll error: %s", DOMAIN, e)

        async def watchdog(_now=None) -> None:
            ts = state.get(LAST_UPDATE_KEY)
            if ts is None:
                return
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > DATA_STALE_SEC:
                _LOGGER.warning("%s: no updates for %.0fs, forcing REST poll + republish", DOMAIN, age)
                try:
                    new_kegs = await fetch_kegs()
                    if new_kegs:
                        for raw in new_kegs:
                            norm = _normalize_keg_dict(raw, state)
                            await _publish_keg(norm)
                    else:
                        for keg_id in list(state.get("data", {}).keys()):
                            hass.bus.async_fire(PLATFORM_EVENT, {"keg_id": keg_id})
                except Exception as e:
                    _LOGGER.error("%s: watchdog REST poll failed: %s", DOMAIN, e)
                    for keg_id in list(state.get("data", {}).keys()):
                        hass.bus.async_fire(PLATFORM_EVENT, {"keg_id": keg_id})

        async def _periodic_devices(_now=None) -> None:
            await fetch_devices()

        # ---------- Services

        async def export_history(call: ServiceCall) -> None:
            path = hass.config.path("www/beer_keg_history.json")

            def _write_export():
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(state["history"][-MAX_LOG_ENTRIES:], f, indent=2)

            await hass.async_add_executor_job(_write_export)
            pn_create(
                hass,
                'Beer Keg history exported.<br><a href="/local/beer_keg_history.json" target="_blank">Open</a>',
                title="Beer Keg Export",
            )

        _register_service_once(hass, "export_history", export_history)

        async def refresh_kegs(call: ServiceCall) -> None:
            new_kegs = await fetch_kegs()
            for raw in new_kegs:
                norm = _normalize_keg_dict(raw, state)
                await _publish_keg(norm)
            pn_create(hass, f"Refreshed {len(new_kegs)} kegs", title="Beer Keg Refresh")

        _register_service_once(hass, "refresh_kegs", refresh_kegs)

        async def republish_all(call: ServiceCall) -> None:
            data = state.get("data", {})
            for keg_id in data.keys():
                hass.bus.async_fire(PLATFORM_EVENT, {"keg_id": keg_id})
            pn_create(hass, f"Republished {len(data)} kegs", title="Beer Keg Republish")

        _register_service_once(hass, "republish_all", republish_all)

        async def refresh_devices(call: ServiceCall) -> None:
            ids = await fetch_devices()
            pn_create(hass, f"Found {len(ids)} device(s).", title="Beer Keg Devices")

        _register_service_once(hass, "refresh_devices", refresh_devices)

        async def calibrate_keg(call: ServiceCall) -> None:
            """POST /api/kegs/calibrate with values read from HA entities."""
            base = _rest_base_from_ws(state["ws_url"])
            url = f"{base}/api/kegs/calibrate"

            dev_state = hass.states.get("select.keg_device")
            if not dev_state or dev_state.state in ("unknown", "unavailable", ""):
                pn_create(hass, "No keg selected.", title="Beer Keg Calibration")
                return

            keg_id = dev_state.state

            name_state = hass.states.get("input_text.beer_keg_name")
            if name_state and name_state.state not in ("unknown", "unavailable", ""):
                keg_name = name_state.state
            else:
                keg_name = keg_id

            def _get_float(ent_id: str, default: float = 0.0) -> float:
                ent = hass.states.get(ent_id)
                if not ent:
                    return default
                try:
                    return float(ent.state)
                except (TypeError, ValueError):
                    return default

            full_weight = _get_float("input_number.keg_cfg_full_weight_kg")
            weight_cal = _get_float("input_number.keg_cfg_weight_cal")
            temp_cal = _get_float("input_number.keg_cfg_temp_cal_c")

            payload = {
                "id": keg_id,
                "name": keg_name,
                "full_weight": full_weight,
                "weight_calibrate": weight_cal,
                "temperature_calibrate": temp_cal,
            }

            try:
                http_sess = async_get_clientsession(hass)
                async with http_sess.post(url, json=payload) as resp:
                    body = await resp.text()
                    if resp.status not in (200, 201):
                        raise RuntimeError(f"HTTP {resp.status}: {body[:200]}")

                pn_create(hass, "Calibration saved.", title="Beer Keg")
                await refresh_kegs(ServiceCall(DOMAIN, "refresh_kegs", {}))
            except Exception as e:
                _LOGGER.error("%s: calibrate_keg failed: %s", DOMAIN, e)
                pn_create(hass, f"Calibration failed: {e}", title="Beer Keg")

        _register_service_once(hass, "calibrate_keg", calibrate_keg)

        async def set_display_units(call: ServiceCall) -> None:
            weight_unit = call.data.get("weight_unit")
            temp_unit = call.data.get("temp_unit")

            if weight_unit is None:
                for ent_id in WEIGHT_UNIT_ENTITIES:
                    ent = hass.states.get(ent_id)
                    if ent and ent.state in ("kg", "lb"):
                        weight_unit = ent.state
                        break

            if temp_unit is None:
                for ent_id in TEMP_UNIT_ENTITIES:
                    ent = hass.states.get(ent_id)
                    if ent and ent.state in ("°C", "°F"):
                        temp_unit = ent.state
                        break

            if weight_unit not in ("kg", "lb"):
                weight_unit = state["display_units"].get("weight", "kg")
            if temp_unit not in ("°C", "°F"):
                temp_unit = state["display_units"].get("temp", "°C")

            state["display_units"] = {"weight": weight_unit, "temp": temp_unit}
            await state["prefs_store"].async_save(
                {
                    "display_units": state.get("display_units", {}),
                    "keg_config": state.get("keg_config", {}),
                    "tap_text": state.get("tap_text", {}),
                    "tap_numbers": state.get("tap_numbers", {}),
                    "noise_deadband_kg": state.get("noise_deadband_kg"),
                    "smoothing_alpha": state.get("smoothing_alpha"),
                }
            )

            for keg_id in list(state.get("data", {}).keys()):
                hass.bus.async_fire(PLATFORM_EVENT, {"keg_id": keg_id})

            pn_create(hass, f"Display units set to {weight_unit}, {temp_unit}", title="Beer Keg")

        _register_service_once(hass, "set_display_units", set_display_units)

        # ---------- Command API Services (new server only; graceful 404 on old server)

        def _resolve_cmd_keg_id(call: ServiceCall) -> str | None:
            keg_id = call.data.get("id")
            if keg_id:
                return str(keg_id)
            dev_state = hass.states.get("select.keg_device")
            if dev_state and dev_state.state not in ("unknown", "unavailable", ""):
                return dev_state.state
            return None

        async def _post_keg_cmd(keg_id: str, cmd: str, payload: dict | None = None) -> tuple[int, str]:
            base = _rest_base_from_ws(state["ws_url"])
            url = f"{base}/api/kegs/{keg_id}/{cmd}"
            session = async_get_clientsession(hass)
            async with session.post(url, json=payload or {}) as resp:
                txt = await resp.text()
                return resp.status, txt

        async def _cmd_result(keg_id: str, cmd: str, status: int, body: str) -> None:
            if status == 404:
                pn_create(
                    hass,
                    f"Command API not supported by this server (404) for '{cmd}'.",
                    title="Beer Keg Command",
                )
                return
            if status >= 300:
                pn_create(
                    hass,
                    f"Command '{cmd}' failed for {keg_id}: HTTP {status}\n{body}",
                    title="Beer Keg Command",
                )
                return
            pn_create(hass, f"Command '{cmd}' sent to {keg_id}", title="Beer Keg Command")

        _SCHEMA_ID_ONLY = vol.Schema({vol.Optional("id"): cv.string})
        _SCHEMA_FLOAT_VALUE = vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.Coerce(float)})
        _SCHEMA_INT_VALUE = vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.Coerce(int)})
        _SCHEMA_STR_VALUE = vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): cv.string})

        async def keg_tare(call: ServiceCall) -> None:
            keg_id = _resolve_cmd_keg_id(call)
            if not keg_id:
                pn_create(hass, "No keg selected.", title="Beer Keg Command")
                return
            status, body = await _post_keg_cmd(keg_id, "tare")
            await _cmd_result(keg_id, "tare", status, body)

        async def keg_set_empty_keg(call: ServiceCall) -> None:
            keg_id = _resolve_cmd_keg_id(call)
            if not keg_id:
                pn_create(hass, "No keg selected.", title="Beer Keg Command")
                return
            status, body = await _post_keg_cmd(keg_id, "empty-keg", {"value": float(call.data["value"])})
            await _cmd_result(keg_id, "empty-keg", status, body)

        async def keg_set_max_volume(call: ServiceCall) -> None:
            keg_id = _resolve_cmd_keg_id(call)
            if not keg_id:
                pn_create(hass, "No keg selected.", title="Beer Keg Command")
                return
            status, body = await _post_keg_cmd(keg_id, "max-keg-volume", {"value": float(call.data["value"])})
            await _cmd_result(keg_id, "max-keg-volume", status, body)

        async def keg_set_temp_offset(call: ServiceCall) -> None:
            keg_id = _resolve_cmd_keg_id(call)
            if not keg_id:
                pn_create(hass, "No keg selected.", title="Beer Keg Command")
                return
            status, body = await _post_keg_cmd(keg_id, "temperature-offset", {"value": float(call.data["value"])})
            await _cmd_result(keg_id, "temperature-offset", status, body)

        async def keg_calibrate_known(call: ServiceCall) -> None:
            keg_id = _resolve_cmd_keg_id(call)
            if not keg_id:
                pn_create(hass, "No keg selected.", title="Beer Keg Command")
                return
            status, body = await _post_keg_cmd(keg_id, "calibrate-known-weight", {"value": float(call.data["value"])})
            await _cmd_result(keg_id, "calibrate-known-weight", status, body)

        async def keg_set_beer_style(call: ServiceCall) -> None:
            keg_id = _resolve_cmd_keg_id(call)
            if not keg_id:
                pn_create(hass, "No keg selected.", title="Beer Keg Command")
                return
            status, body = await _post_keg_cmd(keg_id, "beer-style", {"value": str(call.data["value"])})
            await _cmd_result(keg_id, "beer-style", status, body)

        async def keg_set_date(call: ServiceCall) -> None:
            keg_id = _resolve_cmd_keg_id(call)
            if not keg_id:
                pn_create(hass, "No keg selected.", title="Beer Keg Command")
                return
            status, body = await _post_keg_cmd(keg_id, "date", {"value": str(call.data["value"])})
            await _cmd_result(keg_id, "date", status, body)

        async def keg_set_unit(call: ServiceCall) -> None:
            keg_id = _resolve_cmd_keg_id(call)
            if not keg_id:
                pn_create(hass, "No keg selected.", title="Beer Keg Command")
                return
            status, body = await _post_keg_cmd(keg_id, "unit", {"value": str(call.data["value"]).lower()})
            await _cmd_result(keg_id, "unit", status, body)

        async def keg_set_measure_unit(call: ServiceCall) -> None:
            keg_id = _resolve_cmd_keg_id(call)
            if not keg_id:
                pn_create(hass, "No keg selected.", title="Beer Keg Command")
                return
            status, body = await _post_keg_cmd(keg_id, "measure-unit", {"value": str(call.data["value"]).lower()})
            await _cmd_result(keg_id, "measure-unit", status, body)

        async def keg_set_mode(call: ServiceCall) -> None:
            keg_id = _resolve_cmd_keg_id(call)
            if not keg_id:
                pn_create(hass, "No keg selected.", title="Beer Keg Command")
                return
            status, body = await _post_keg_cmd(keg_id, "keg-mode", {"value": str(call.data["value"]).lower()})
            await _cmd_result(keg_id, "keg-mode", status, body)

        async def keg_set_sensitivity(call: ServiceCall) -> None:
            keg_id = _resolve_cmd_keg_id(call)
            if not keg_id:
                pn_create(hass, "No keg selected.", title="Beer Keg Command")
                return
            status, body = await _post_keg_cmd(keg_id, "sensitivity", {"value": int(call.data["value"])})
            await _cmd_result(keg_id, "sensitivity", status, body)

        _register_service_once(hass, "keg_tare", keg_tare, schema=_SCHEMA_ID_ONLY)
        _register_service_once(hass, "keg_set_empty_keg_weight", keg_set_empty_keg, schema=_SCHEMA_FLOAT_VALUE)
        _register_service_once(hass, "keg_set_max_keg_volume", keg_set_max_volume, schema=_SCHEMA_FLOAT_VALUE)
        _register_service_once(hass, "keg_set_temperature_offset", keg_set_temp_offset, schema=_SCHEMA_FLOAT_VALUE)
        _register_service_once(hass, "keg_calibrate_known_weight", keg_calibrate_known, schema=_SCHEMA_FLOAT_VALUE)
        _register_service_once(hass, "keg_set_beer_style", keg_set_beer_style, schema=_SCHEMA_STR_VALUE)
        _register_service_once(hass, "keg_set_date", keg_set_date, schema=_SCHEMA_STR_VALUE)

        _register_service_once(
            hass,
            "keg_set_unit_system",
            keg_set_unit,
            schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.In(["metric", "us"])}),
        )
        _register_service_once(
            hass,
            "keg_set_measure_unit",
            keg_set_measure_unit,
            schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.In(["weight", "volume"])}),
        )
        _register_service_once(
            hass,
            "keg_set_mode",
            keg_set_mode,
            schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.In(["beer", "co2"])}),
        )

        _register_service_once(hass, "keg_set_sensitivity", keg_set_sensitivity, schema=_SCHEMA_INT_VALUE)

        async def on_stop(event) -> None:
            await state["history_store"].async_save(state["history"][-MAX_LOG_ENTRIES:])
            await state["prefs_store"].async_save(
                {
                    "display_units": state.get("display_units", {}),
                    "keg_config": state.get("keg_config", {}),
                    "tap_text": state.get("tap_text", {}),
                    "tap_numbers": state.get("tap_numbers", {}),
                    "noise_deadband_kg": state.get("noise_deadband_kg"),
                    "smoothing_alpha": state.get("smoothing_alpha"),
                }
            )

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_stop)

        # ---------- Start after HA is running

        async def _start_after_started(event=None) -> None:
            await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

            try:
                await fetch_devices()
                initial = await fetch_kegs()
                for raw in initial:
                    norm = _normalize_keg_dict(raw, state)
                    await _publish_keg(norm)
                _LOGGER.info("%s: Initial REST refresh found %d kegs", DOMAIN, len(initial))
            except Exception as e:
                _LOGGER.warning("%s: Initial refresh failed: %s", DOMAIN, e)

            hass.async_create_task(connect_websocket())
            async_track_time_interval(hass, rest_poll, timedelta(seconds=REST_POLL_SECONDS))
            async_track_time_interval(hass, watchdog, timedelta(seconds=10))
            async_track_time_interval(hass, _periodic_devices, timedelta(seconds=DEVICES_REFRESH_SEC))
            _LOGGER.info("%s: started background tasks", DOMAIN)

        if hass.state == "RUNNING":
            hass.async_create_task(_start_after_started())
        else:
            hass.bus.async_listen_once("homeassistant_started", _start_after_started)

        _LOGGER.info("%s: setup complete (WS+REST+poll+watchdog+devices+services)", DOMAIN)
        return True

    except Exception as e:
        _LOGGER.exception("%s: setup_entry crashed: %s", DOMAIN, e)
        return False


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    state = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and state is not None:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
