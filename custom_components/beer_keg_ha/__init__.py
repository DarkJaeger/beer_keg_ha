from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from urllib.parse import urlparse, urlunparse

import aiohttp
from homeassistant.components.persistent_notification import async_create as pn_create
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval, async_track_time_change
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
)

_LOGGER = logging.getLogger(__name__)

# Platforms we load
PLATFORMS = ["sensor", "select", "number", "text", "date"]

# hassfest requirement
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

KG_TO_OZ = 35.274
REST_POLL_SECONDS = 10
WS_PING_SEC = 30
DATA_STALE_SEC = 45
DEVICES_REFRESH_SEC = 60
LAST_UPDATE_KEY = "last_update_ts"

DEVICES_UPDATE_EVENT = f"{DOMAIN}_devices_update"

# Optional density keys
try:
    from .const import (
        CONF_FULL_VOLUME_L,
        CONF_BEER_SG,
        DEFAULT_BEER_SG,
        DEFAULT_FULL_VOLUME_L,
        WATER_DENSITY_KG_PER_L,
    )

    DENSITY_AWARE = True
except Exception:  # pragma: no cover - optional import
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


def _normalize_keg_dict(keg: dict) -> dict:
    """Normalize keg dict from WS/REST payloads into a common structure."""
    keg_id = str(keg.get("id", "unknown")).lower().replace(" ", "_")
    weight = _coerce_float(keg.get("weight"))
    temp = keg.get("temperature")
    temp = _coerce_float(temp) if temp is not None else None
    full_w = _coerce_float(keg.get("full_weight"), default=0.0)
    name = keg.get("name") or keg_id
    return {
        "keg_id": keg_id,
        "name": name,
        "weight": weight,
        "temperature": temp,
        "full_weight": full_w if full_w > 0 else None,
        "weight_calibrate": _coerce_float(keg.get("weight_calibrate")),
        "temperature_calibrate": _coerce_float(keg.get("temperature_calibrate")),
    }


def _rest_base_from_ws(ws: str) -> str:
    """Build http(s) base URL from ws:// or wss://."""
    u = urlparse(ws)
    scheme = "http" if u.scheme == "ws" else "https" if u.scheme == "wss" else "http"
    return urlunparse((scheme, u.netloc, "", "", "", ""))


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

        # Optional density-aware computed full weight (global default)
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

        # Main runtime state for this config entry
        state: Dict[str, Any] = {
            "ws_url": ws_url,
            "empty_weight": empty_weight,
            "default_full": default_full,
            "pour_threshold": pour_threshold,
            "per_keg_full": per_keg_full,
            "full_volume_l": full_volume_l,
            "beer_sg": beer_sg,
            "computed_full_from_sg": computed_full_from_sg,
            "kegs": {},          # runtime per-keg stats
            "data": {},          # values exposed to entities
            "history": [],
            "devices": [],
            "display_units": {   # may be overridden by prefs below
                "weight": "kg",
                "temp": "°C",
                "pour": "oz",    # last_pour / daily_consumption base unit
            },
            # Global smoothing config (overridable via Number entities)
            "tap_text": {},      # will be overridden by prefs if present
            "tap_numbers": {},   # "
            "noise_deadband_kg": float(opts.get("noise_deadband_kg", 0.0)),
            "smoothing_alpha": float(opts.get("smoothing_alpha", 1.0)),
            # Optional per-keg config that should survive restart
            # (e.g. name override, beer_sg, original_gravity, kegged_date, expiration_date)
            "keg_config": {},
            "history_store": history_store,
            "prefs_store": prefs_store,
            LAST_UPDATE_KEY: None,
        }

        hass.data[DOMAIN][entry.entry_id] = state

        # ---- load history from storage
        loaded_history = await history_store.async_load()
        if isinstance(loaded_history, list):
            state["history"] = loaded_history
            _LOGGER.info("%s: Loaded %d pour records", DOMAIN, len(state["history"]))

        # ---- load prefs (display_units + keg_config + tap_text + tap_numbers + smoothing) from storage
        loaded_prefs = await prefs_store.async_load()
        if isinstance(loaded_prefs, dict):
            du = loaded_prefs.get("display_units")
            if isinstance(du, dict):
                w = du.get("weight")
                t = du.get("temp")
                p = du.get("pour")
                if w in ("kg", "lb"):
                    state["display_units"]["weight"] = w
                if t in ("°C", "°F"):
                    state["display_units"]["temp"] = t
                if p in ("oz", "ml"):
                    state["display_units"]["pour"] = p

            keg_cfg = loaded_prefs.get("keg_config")
            if isinstance(keg_cfg, dict):
                norm_cfg: Dict[str, Dict[str, Any]] = {}
                for k, v in keg_cfg.items():
                    keg_id = str(k).lower().replace(" ", "_")
                    if isinstance(v, dict):
                        norm_cfg[keg_id] = v
                state["keg_config"] = norm_cfg

            # restore tap text / numbers
            tap_text = loaded_prefs.get("tap_text")
            if isinstance(tap_text, dict):
                state["tap_text"] = tap_text

            tap_numbers = loaded_prefs.get("tap_numbers")
            if isinstance(tap_numbers, dict):
                state["tap_numbers"] = tap_numbers

            # restore per-entry smoothing settings
            ndb = loaded_prefs.get("noise_deadband_kg")
            if isinstance(ndb, (int, float)):
                state["noise_deadband_kg"] = float(ndb)

            sa = loaded_prefs.get("smoothing_alpha")
            if isinstance(sa, (int, float)):
                state["smoothing_alpha"] = float(sa)
        # ---- end load prefs

        # ---------- REST helpers

        async def fetch_kegs() -> List[Dict[str, Any]]:
            """GET /api/kegs (list or {'kegs':[...]})"""
            try:
                base = _rest_base_from_ws(ws_url)
                urls = (f"{base}/api/kegs", f"{base}/api/kegs/")
                async with aiohttp.ClientSession() as http_sess:
                    for url in urls:
                        try:
                            async with http_sess.get(url) as resp:
                                if resp.status != 200:
                                    _ = await resp.text()
                                    continue
                                try:
                                    data = await resp.json()
                                except Exception:
                                    continue
                                if isinstance(data, list):
                                    return data
                                if isinstance(data, dict) and isinstance(data.get("kegs"), list):
                                    return data["kegs"]
                        except Exception as e:
                            _LOGGER.warning("%s: REST GET failed %s (%s)", DOMAIN, url, e)
                return []
            except Exception as e:
                _LOGGER.error("%s: fetch_kegs error (outer): %s", DOMAIN, e)
                return []

        async def fetch_devices() -> list[str]:
            """GET /api/kegs/devices -> list[str]."""
            try:
                base = _rest_base_from_ws(ws_url)
                urls = (f"{base}/api/kegs/devices", f"{base}/api/kegs/devices/")
                async with aiohttp.ClientSession() as http_sess:
                    for url in urls:
                        try:
                            async with http_sess.get(url) as resp:
                                if resp.status != 200:
                                    _ = await resp.text()
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
            except Exception as e:
                _LOGGER.error("%s: fetch_devices error: %s", DOMAIN, e)
                return state.setdefault("devices", [])

        # ---------- publisher

        async def _publish_keg(norm: dict) -> None:
            """Normalize and push one keg's data into integration state."""
            keg_id = norm["keg_id"]
            weight_raw = norm["weight"]
            temp = norm["temperature"]

            # ---------- INITIALIZE PER-KEG RUNTIME STATE ----------
            info = state["kegs"].get(keg_id)
            if not info:
                initial_fw = (
                    norm["full_weight"]
                    or state["per_keg_full"].get(keg_id)
                    or state["computed_full_from_sg"]
                    or state["default_full"]
                )
                info = state["kegs"][keg_id] = {
                    "last_weight_raw": weight_raw,
                    "last_weight": weight_raw,
                    "daily_consumed": 0.0,
                    "last_pour": 0.0,
                    "last_pour_time": None,
                    "full_weight": float(initial_fw),
                    "recent_weights": [],
                }

            # ---------- POUR DETECTION ----------
            prev_weight_raw = info.get("last_weight_raw", weight_raw)
            raw_delta = prev_weight_raw - weight_raw
            is_pour = raw_delta > state.get("pour_threshold", DEFAULT_POUR_THRESHOLD)
            info["last_weight_raw"] = weight_raw

            # ---------- CHECK PER-KEG BYPASS FLAG ----------
            keg_cfg = state.get("keg_config", {}).get(keg_id, {}) or {}
            bypass_smoothing = bool(keg_cfg.get("disable_smoothing", False))

            # ---------- SMOOTHING ----------
            display_weight = weight_raw
            if not is_pour and not bypass_smoothing:
                recent = info.setdefault("recent_weights", [])
                recent.append(weight_raw)
                if len(recent) > 5:
                    recent.pop(0)

                median_kg = sorted(recent)[len(recent) // 2]

                alpha = float(state.get("smoothing_alpha", 1.0))
                if not (0 < alpha <= 1):
                    alpha = 1.0
                previous_filtered = info.get("filtered_weight", median_kg)
                filtered = (alpha * median_kg) + ((1 - alpha) * previous_filtered)

                deadband = float(state.get("noise_deadband_kg", 0.0))
                if abs(filtered - previous_filtered) < deadband:
                    filtered = previous_filtered

                display_weight = filtered
                info["filtered_weight"] = filtered
            else:
                # During pours OR when bypass_smoothing is True, use raw weight
                info["filtered_weight"] = weight_raw

            info["last_weight"] = display_weight

            # ---------- POUR STATS ----------
            if is_pour:
                delta_kg = round(raw_delta, 2)
                delta_oz = round(delta_kg * KG_TO_OZ, 1)
                info["last_pour"] = delta_oz
                info["last_pour_time"] = datetime.now(timezone.utc)
                info["daily_consumed"] += delta_oz

                state["history"].append({
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "keg": keg_id,
                    "pour_oz": delta_oz,
                    "weight_before_kg": round(prev_weight_raw, 2),
                    "weight_after_kg": round(weight_raw, 2),
                    "temperature_c": temp,
                })
                if len(state["history"]) > MAX_LOG_ENTRIES:
                    state["history"].pop(0)
                await state["history_store"].async_save(state["history"][-MAX_LOG_ENTRIES:])

            # ---------- APPLY MANUAL OVERRIDES FROM NUMBER ENTITIES ----------
            existing = state["data"].get(keg_id, {})

            # Keep defaults only if no data yet
            if "full_weight" not in existing:
                existing["full_weight"] = 19.0
            if "weight_calibrate" not in existing:
                existing["weight_calibrate"] = 0.15
            if "temperature_calibrate" not in existing:
                existing["temperature_calibrate"] = 0.0

            # Use manual overrides FIRST
            override_fw = existing.get("full_weight")
            override_wc = existing.get("weight_calibrate")
            override_tc = existing.get("temperature_calibrate")

            # ---------- FILL % ----------
            fw = (
                override_fw
                or info.get("full_weight")
                or state["per_keg_full"].get(keg_id)
                or state["computed_full_from_sg"]
                or state["default_full"]
            )

            ew = state["empty_weight"]
            w_val = max(0.0, display_weight)

            if fw and fw > ew:
                fill_pct = ((w_val - ew) / (fw - ew)) * 100.0
            else:
                fill_pct = 0.0
            fill_pct = max(0, min(100, fill_pct))

            # ---------- WRITE STATE BACK ----------
            state["data"][keg_id] = {
                **existing,
                "id": keg_id,
                "name": norm["name"],
                "weight": round(display_weight, 2),
                "temperature": round(temp, 1) if temp is not None else None,
                "full_weight": round(float(fw), 2),
                "weight_calibrate": override_wc,
                "temperature_calibrate": override_tc,
                "daily_consumed": round(info["daily_consumed"], 1),
                "last_pour": round(info["last_pour"], 1),
                "fill_percent": round(fill_pct, 1),
            }

            state[LAST_UPDATE_KEY] = datetime.now(timezone.utc)

            if keg_id not in state["devices"]:
                state["devices"].append(keg_id)
                hass.bus.async_fire(DEVICES_UPDATE_EVENT, {"ids": list(state["devices"])})

            hass.bus.async_fire(f"{DOMAIN}_update", {"keg_id": keg_id})

        # ---------- WebSocket loop

        async def connect_websocket() -> None:
            async with aiohttp.ClientSession() as http_sess:
                while True:
                    try:
                        _LOGGER.info("%s: Connecting WS -> %s", DOMAIN, ws_url)
                        async with http_sess.ws_connect(ws_url) as ws:
                            _LOGGER.info("%s: Connected to WS", DOMAIN)

                            async def _pinger() -> None:
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
                                    norm = _normalize_keg_dict(raw)
                                    await _publish_keg(norm)
                    except Exception as e:
                        _LOGGER.error("%s: WS error: %s", DOMAIN, e)
                        await asyncio.sleep(10)

        # ---------- REST poll & watchdog

        async def rest_poll(_now: datetime | None = None) -> None:
            try:
                new_kegs = await fetch_kegs()
                for raw in new_kegs:
                    norm = _normalize_keg_dict(raw)
                    await _publish_keg(norm)
            except Exception as e:
                _LOGGER.debug("%s: REST poll error: %s", DOMAIN, e)

        async def watchdog(_now: datetime | None = None) -> None:
            ts = state.get(LAST_UPDATE_KEY)
            if ts is None:
                return
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > DATA_STALE_SEC:
                _LOGGER.warning(
                    "%s: no updates for %.0fs, forcing REST poll + republish",
                    DOMAIN,
                    age,
                )
                try:
                    new_kegs = await fetch_kegs()
                    if new_kegs:
                        for raw in new_kegs:
                            norm = _normalize_keg_dict(raw)
                            await _publish_keg(norm)
                    else:
                        for keg_id in list(state.get("data", {}).keys()):
                            hass.bus.async_fire(f"{DOMAIN}_update", {"keg_id": keg_id})
                except Exception as e:
                    _LOGGER.error("%s: watchdog REST poll failed: %s", DOMAIN, e)
                    for keg_id in list(state.get("data", {}).keys()):
                        hass.bus.async_fire(f"{DOMAIN}_update", {"keg_id": keg_id})

        async def _periodic_devices(_now: datetime | None) -> None:
            await fetch_devices()

        async def reset_daily_consumption(_now: datetime | None = None) -> None:
            """Reset daily_consumed for all kegs at local midnight."""
            try:
                for keg_id, info in state.get("kegs", {}).items():
                    # Reset runtime stats
                    info["daily_consumed"] = 0.0

                    # Reset exposed data if present
                    if keg_id in state.get("data", {}):
                        state["data"][keg_id]["daily_consumed"] = 0.0

                    # Nudge HA entities to update
                    hass.bus.async_fire(f"{DOMAIN}_update", {"keg_id": keg_id})

                _LOGGER.info("%s: daily_consumed reset to 0 for %d kegs", DOMAIN, len(state.get("kegs", {})))
            except Exception as e:
                _LOGGER.error("%s: failed resetting daily_consumed: %s", DOMAIN, e)

        # ---------- Services

        async def export_history(call: ServiceCall) -> None:
            path = hass.config.path("www/beer_keg_history.json")

            def _write_export() -> None:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(state["history"][-MAX_LOG_ENTRIES:], f, indent=2)

            await hass.async_add_executor_job(_write_export)
            pn_create(
                hass,
                'Beer Keg history exported.<br><a href="/local/beer_keg_history.json" target="_blank">Open</a>',
                title="Beer Keg Export",
            )

        hass.services.async_register(DOMAIN, "export_history", export_history)

        async def refresh_kegs(call: ServiceCall) -> None:
            new_kegs = await fetch_kegs()
            for raw in new_kegs:
                norm = _normalize_keg_dict(raw)
                await _publish_keg(norm)
            pn_create(hass, f"Refreshed {len(new_kegs)} kegs", title="Beer Keg Refresh")

        hass.services.async_register(DOMAIN, "refresh_kegs", refresh_kegs)

        async def republish_all(call: ServiceCall) -> None:
            data = state.get("data", {})
            for keg_id in data.keys():
                hass.bus.async_fire(f"{DOMAIN}_update", {"keg_id": keg_id})
            pn_create(hass, f"Republished {len(data)} kegs", title="Beer Keg Republish")

        hass.services.async_register(DOMAIN, "republish_all", republish_all)

        async def refresh_devices(call: ServiceCall) -> None:
            ids = await fetch_devices()
            pn_create(hass, f"Found {len(ids)} device(s).", title="Beer Keg Devices")

        hass.services.async_register(DOMAIN, "refresh_devices", refresh_devices)

        async def calibrate_keg(call: ServiceCall) -> None:
            """
            Calibrate a keg.

            Supports three ways of getting data:

            1) Direct values in service call (Dev Tools / automations):

               data:
                 id: "d00dcafe5ded57971fce7ec1b3ff4253"
                 name: "My Keg"
                 full_weight: 19.0
                 weight_calibrate: 0.15
                 temperature_calibrate: 0.0

            2) Entity IDs in service call (from Lovelace button):

               data:
                 id_entity: select.keg_device
                 name_entity: text.keg_d00d_keg_name
                 full_weight_entity: number.keg_d00d_full_weight_kg
                 weight_calibrate_entity: number.keg_d00d_weight_calibrate
                 temperature_calibrate_entity: number.keg_d00d_temp_calibrate_degc_2

            3) Nothing in data: fall back to select.keg_device + state["data"][keg_id].
            """

            base = _rest_base_from_ws(state["ws_url"])
            url = f"{base}/api/kegs/calibrate"

            data_state: Dict[str, Dict[str, Any]] = state.get("data", {})

            # ---------- helpers to read HA entities ----------
            def _get_ent_state(ent_id: str | None) -> str | None:
                if not ent_id:
                    return None
                ent = hass.states.get(ent_id)
                if not ent or ent.state in ("unknown", "unavailable", ""):
                    return None
                return ent.state

            def _get_ent_float(ent_id: str | None, default: float) -> float:
                s = _get_ent_state(ent_id)
                if s is None:
                    return default
                try:
                    return float(s)
                except (TypeError, ValueError):
                    return default

            # ---------- 1) Resolve keg_id ----------
            keg_id = call.data.get("id")

            # If user passed an id_entity (e.g. select.keg_device), use its state
            if not keg_id:
                id_entity = call.data.get("id_entity")
                keg_id = _get_ent_state(id_entity)

            # Last fallback: select.keg_device
            if not keg_id:
                dev_state = hass.states.get("select.keg_device")
                if not dev_state or dev_state.state in ("unknown", "unavailable", ""):
                    pn_create(hass, "No keg selected.", title="Beer Keg Calibration")
                    _LOGGER.error(
                        "%s: calibrate_keg – no keg id (no 'id', no id_entity, no select.keg_device)",
                        DOMAIN,
                    )
                    return
                keg_id = dev_state.state

            keg_id = str(keg_id)
            short_id = keg_id[:4]
            keg_data = data_state.get(keg_id, {})

            # ---------- 2) Resolve name ----------
            # Priority:
            #   call.data.name ->
            #   name_entity ->
            #   text.keg_<short>_keg_name ->
            #   stored keg name ->
            #   keg_id
            name_from_call = call.data.get("name")
            if name_from_call:
                keg_name = str(name_from_call)
            else:
                name_entity = call.data.get("name_entity")
                name_val = _get_ent_state(name_entity)
                if name_val:
                    keg_name = name_val
                else:
                    # legacy per-keg text entity
                    legacy_name_ent = f"text.keg_{short_id}_keg_name"
                    legacy_name_val = _get_ent_state(legacy_name_ent)
                    if legacy_name_val:
                        keg_name = legacy_name_val
                    else:
                        keg_name = keg_data.get("name") or keg_id

            # ---------- 3) Resolve numeric fields (full_weight, weight_cal, temp_cal) ----------

            def _resolve_float(
                direct_key: str,           # e.g. "full_weight"
                entity_key: str,           # e.g. "full_weight_entity"
                data_key: str,             # e.g. "full_weight"
                default: float,
            ) -> float:
                # 1) Direct numeric in service data
                if direct_key in call.data:
                    try:
                        return float(call.data.get(direct_key))
                    except (TypeError, ValueError):
                        pass

                # 2) Entity id in service data (e.g. number.keg_d00d_full_weight_kg)
                ent_id = call.data.get(entity_key)
                if ent_id:
                    return _get_ent_float(ent_id, default)

                # 3) state["data"][keg_id][data_key]
                val = keg_data.get(data_key)
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return default

            full_weight = _resolve_float(
                "full_weight",
                "full_weight_entity",
                "full_weight",
                state.get("default_full", DEFAULT_FULL_WEIGHT),
            )
            weight_cal = _resolve_float(
                "weight_calibrate",
                "weight_calibrate_entity",
                "weight_calibrate",
                0.0,
            )
            temp_cal = _resolve_float(
                "temperature_calibrate",
                "temperature_calibrate_entity",
                "temperature_calibrate",
                0.0,
            )

            payload = {
                "id": keg_id,
                "name": keg_name,
                "full_weight": full_weight,
                "weight_calibrate": weight_cal,
                "temperature_calibrate": temp_cal,
            }

            _LOGGER.info(
                "%s: calibrating keg %s with payload: %s",
                DOMAIN,
                keg_id,
                payload,
            )

            # ---------- 4) POST to Plaato/Open-Plaato server ----------
            try:
                async with aiohttp.ClientSession() as http_sess:
                    async with http_sess.post(url, json=payload) as resp:
                        body = await resp.text()
                        if resp.status not in (200, 201):
                            raise RuntimeError(f"HTTP {resp.status}: {body[:200]}")

                pn_create(hass, "Calibration saved.", title="Beer Keg")

                # Refresh from server so numbers/sensors match new calibration
                await refresh_kegs(ServiceCall(DOMAIN, "refresh_kegs", {}))

            except Exception as e:
                _LOGGER.error("%s: calibrate_keg failed: %s", DOMAIN, e)
                pn_create(hass, f"Calibration failed: {e}", title="Beer Keg")

        hass.services.async_register(DOMAIN, "calibrate_keg", calibrate_keg)

        async def set_display_units(call: ServiceCall) -> None:
            """
            Change weight / temperature / pour units and persist them.

            Can be called with explicit units, e.g.:

              service: beer_keg_ha.set_display_units
              data:
                weight_unit: "lb"
                temp_unit: "°F"
                pour_unit: "ml"

            or without data, in which case we keep existing settings.
            """
            weight_unit = call.data.get("weight_unit") or state["display_units"].get("weight", "kg")
            temp_unit = call.data.get("temp_unit") or state["display_units"].get("temp", "°C")
            pour_unit = call.data.get("pour_unit") or state["display_units"].get("pour", "oz")

            if weight_unit not in ("kg", "lb"):
                weight_unit = "kg"
            if temp_unit not in ("°C", "°F"):
                temp_unit = "°C"
            if pour_unit not in ("oz", "ml"):
                pour_unit = "oz"

            state["display_units"] = {
                "weight": weight_unit,
                "temp": temp_unit,
                "pour": pour_unit,
            }

            # Persist so it survives reboot (along with all other prefs)
            await state["prefs_store"].async_save(
                {
                    "display_units": state["display_units"],
                    "keg_config": state.get("keg_config", {}),
                    "tap_text": state.get("tap_text", {}),
                    "tap_numbers": state.get("tap_numbers", {}),
                    "noise_deadband_kg": state.get("noise_deadband_kg"),
                    "smoothing_alpha": state.get("smoothing_alpha"),
                }
            )

            # Notify all keg sensors so they recalc units
            for keg_id in list(state.get("data", {}).keys()):
                hass.bus.async_fire(f"{DOMAIN}_update", {"keg_id": keg_id})

            pn_create(
                hass,
                f"Display units set to weight={weight_unit}, temp={temp_unit}, pour={pour_unit}",
                title="Beer Keg",
            )

        hass.services.async_register(DOMAIN, "set_display_units", set_display_units)

        async def on_stop(event) -> None:
            """Save both history and prefs on shutdown."""
            try:
                await state["history_store"].async_save(state["history"][-MAX_LOG_ENTRIES:])
                await state["prefs_store"].async_save(
                    {
                        "display_units": state["display_units"],
                        "keg_config": state.get("keg_config", {}),
                        "tap_text": state.get("tap_text", {}),
                        "tap_numbers": state.get("tap_numbers", {}),
                        "noise_deadband_kg": state.get("noise_deadband_kg"),
                        "smoothing_alpha": state.get("smoothing_alpha"),
                    }
                )
            except Exception as e:  # pragma: no cover - best effort
                _LOGGER.warning("%s: failed to persist state on stop: %s", DOMAIN, e)

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_stop)

        # ---------- Start after HA is running

        async def _start_after_started(event: Any | None = None) -> None:
            # create entities for platforms
            await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

            try:
                await fetch_devices()
                initial = await fetch_kegs()
                for raw in initial:
                    norm = _normalize_keg_dict(raw)
                    await _publish_keg(norm)
                _LOGGER.info("%s: Initial REST refresh found %d kegs", DOMAIN, len(initial))
            except Exception as e:
                _LOGGER.warning("%s: Initial refresh failed: %s", DOMAIN, e)

            hass.async_create_task(connect_websocket())
            async_track_time_interval(hass, rest_poll, timedelta(seconds=REST_POLL_SECONDS))
            async_track_time_interval(hass, watchdog, timedelta(seconds=10))
            async_track_time_interval(hass, _periodic_devices, timedelta(seconds=DEVICES_REFRESH_SEC))

            # Reset daily_consumed at local midnight
            async_track_time_change(hass, reset_daily_consumption, hour=0, minute=0, second=0)

            _LOGGER.info("%s: started background tasks", DOMAIN)

        if hass.state == "RUNNING":
            hass.async_create_task(_start_after_started())
        else:
            hass.bus.async_listen_once("homeassistant_started", _start_after_started)

        _LOGGER.info("%s: setup complete (WS+REST+poll+watchdog+devices+services)", DOMAIN)
        return True

    except Exception as e:  # pragma: no cover - top-level safety net
        _LOGGER.exception("%s: setup_entry crashed: %s", DOMAIN, e)
        return False


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    state = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and state is not None:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
