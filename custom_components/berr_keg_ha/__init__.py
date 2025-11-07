from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List
from urllib.parse import urlparse, urlunparse

import aiohttp
import voluptuous as vol
from homeassistant.components.persistent_notification import async_create as pn_create
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    # base options
    CONF_WS_URL,
    CONF_EMPTY_WEIGHT,
    CONF_DEFAULT_FULL_WEIGHT,
    CONF_POUR_THRESHOLD,
    CONF_PER_KEG_FULL,
    MAX_LOG_ENTRIES,
    DEFAULT_EMPTY_WEIGHT,
    DEFAULT_FULL_WEIGHT,
    DEFAULT_POUR_THRESHOLD,
    # optional density-aware keys (present if you added them to const.py)
    # they are imported conditionally below
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]

# hassfest requirement since we implement async_setup
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Behavior constants
KG_TO_OZ = 35.274
REST_POLL_SECONDS = 10
WS_PING_SEC = 30
DATA_STALE_SEC = 45
LAST_UPDATE_KEY = "last_update_ts"

# Optional density defaults (guarded in case not present in const.py)
try:
    from .const import (
        CONF_FULL_VOLUME_L,
        CONF_BEER_SG,
        DEFAULT_FULL_VOLUME_L,
        DEFAULT_BEER_SG,
        WATER_DENSITY_KG_PER_L,
    )
    DENSITY_AWARE = True
except Exception:  # const does not have density keys
    DENSITY_AWARE = False
    CONF_FULL_VOLUME_L = "full_volume_liters"
    CONF_BEER_SG = "beer_specific_gravity"
    DEFAULT_FULL_VOLUME_L = 19.0
    DEFAULT_BEER_SG = 1.010
    WATER_DENSITY_KG_PER_L = 0.998


def _coerce_float(val, default=0.0):
    try:
        return float(val)
    except Exception:
        return default


def _normalize_keg_dict(keg: dict) -> dict:
    """Normalize keg data from REST or WebSocket payloads."""
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


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    try:
        ws_url: str = entry.data.get(CONF_WS_URL)
        if not ws_url:
            _LOGGER.error("%s: Missing ws_url", DOMAIN)
            return False

        opts = entry.options or {}
        empty_weight = float(opts.get(CONF_EMPTY_WEIGHT, DEFAULT_EMPTY_WEIGHT))
        default_full = float(opts.get(CONF_DEFAULT_FULL_WEIGHT, DEFAULT_FULL_WEIGHT))
        pour_threshold = float(opts.get(CONF_POUR_THRESHOLD, DEFAULT_POUR_THRESHOLD))

        # Optional density-aware computation for effective full_weight
        if DENSITY_AWARE:
            full_volume_l = float(opts.get(CONF_FULL_VOLUME_L, DEFAULT_FULL_VOLUME_L))
            beer_sg = float(opts.get(CONF_BEER_SG, DEFAULT_BEER_SG))
            computed_full_from_sg = full_volume_l * beer_sg * WATER_DENSITY_KG_PER_L  # kg
        else:
            full_volume_l = DEFAULT_FULL_VOLUME_L
            beer_sg = DEFAULT_BEER_SG
            computed_full_from_sg = None

        # Per-keg full weight overrides (JSON map id->kg)
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
        store: Store = Store(hass, 1, f"{DOMAIN}_history")

        hass.data[DOMAIN][entry.entry_id] = state = {
            "ws_url": ws_url,
            "empty_weight": empty_weight,
            "default_full": default_full,
            "pour_threshold": pour_threshold,
            "per_keg_full": per_keg_full,
            "full_volume_l": full_volume_l,
            "beer_sg": beer_sg,
            "computed_full_from_sg": computed_full_from_sg,  # may be None
            "kegs": {},          # runtime per-keg (last_weight, last_pour, etc.)
            "data": {},          # values exposed to sensor.py
            "history": [],
            "store": store,
            LAST_UPDATE_KEY: None,
        }

        # Load history
        loaded = await store.async_load()
        if isinstance(loaded, list):
            state["history"] = loaded
            _LOGGER.info("%s: Loaded %d pour records", DOMAIN, len(state["history"]))

        # ---------- REST helper
        async def fetch_kegs() -> List[Dict[str, Any]]:
            """Fetch /api/kegs. Accepts list[...] or dict{'kegs':[...]}."""
            try:
                u = urlparse(ws_url)
                scheme = "http" if u.scheme == "ws" else "https" if u.scheme == "wss" else "http"
                base = urlunparse((scheme, u.netloc, "", "", "", ""))
                urls = (f"{base}/api/kegs", f"{base}/api/kegs/")

                async with aiohttp.ClientSession() as http:
                    for url in urls:
                        try:
                            async with http.get(url) as resp:
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

        # ---------- Core publisher
        async def _publish_keg(norm: dict):
            keg_id = norm["keg_id"]
            weight = norm["weight"]              # kg (net if you tared)
            temp = norm["temperature"]

            info = state["kegs"].get(keg_id)
            if not info:
                # Full weight selection priority:
                # 1) device-reported per message
                # 2) per-keg override
                # 3) computed by Volume×SG×WaterDensity (if available)
                # 4) default_full
                initial_fw = (
                    norm["full_weight"]
                    or state["per_keg_full"].get(keg_id)
                    or state["computed_full_from_sg"]
                    or state["default_full"]
                )
                info = state["kegs"][keg_id] = {
                    "last_weight": weight,
                    "daily_consumed": 0.0,   # oz
                    "last_pour": 0.0,        # oz
                    "last_pour_time": None,
                    "full_weight": float(initial_fw) if initial_fw else float(state["default_full"]),
                }

            # If device later reports a full_weight, adopt it
            if norm["full_weight"] and norm["full_weight"] > 0 and norm["full_weight"] != info["full_weight"]:
                info["full_weight"] = float(norm["full_weight"])

            prev_weight = info["last_weight"]
            info["last_weight"] = weight

            # Pour detection (kg) → store in oz
            if prev_weight - weight > state["pour_threshold"]:
                delta_kg = round(prev_weight - weight, 2)
                delta_oz = round(delta_kg * KG_TO_OZ, 1)
                info["last_pour"] = delta_oz
                info["last_pour_time"] = datetime.now(timezone.utc)
                info["daily_consumed"] += delta_oz

                state["history"].append({
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "keg": keg_id,
                    "pour_oz": delta_oz,
                    "weight_before_kg": round(prev_weight, 2),
                    "weight_after_kg": round(weight, 2),
                    "temperature_c": temp,
                })
                if len(state["history"]) > MAX_LOG_ENTRIES:
                    state["history"].pop(0)
                await state["store"].async_save(state["history"][-MAX_LOG_ENTRIES:])

            # Fill %
            fw = (
                info.get("full_weight")
                or state["per_keg_full"].get(keg_id)
                or state["computed_full_from_sg"]
                or state["default_full"]
            )
            ew = state["empty_weight"]  # set to 0 if you tare with empty keg
            w = max(0.0, weight)
            if fw and fw > ew:
                fill_pct = ((w - ew) / (fw - ew)) * 100.0
            else:
                fill_pct = 0.0
            fill_pct = max(0.0, min(100.0, fill_pct))

            state["data"][keg_id] = {
                "id": keg_id,
                "name": norm["name"],
                "weight": round(weight, 2),                   # kg (raw)
                "temperature": round(temp, 1) if temp is not None else None,
                "full_weight": round(float(fw), 2) if fw else None,
                "daily_consumed": round(info["daily_consumed"], 1),  # oz
                "last_pour": round(info["last_pour"], 1),            # oz
                "fill_percent": round(fill_pct, 1),
            }
            state[LAST_UPDATE_KEY] = datetime.now(timezone.utc)
            hass.bus.async_fire(f"{DOMAIN}_update", {"keg_id": keg_id})

        # ---------- WebSocket loop
        async def connect_websocket():
            async with aiohttp.ClientSession() as http:
                while True:
                    try:
                        _LOGGER.info("%s: Connecting WS -> %s", DOMAIN, ws_url)
                        async with http.ws_connect(ws_url) as ws:
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

                                # Accept either {"kegs":[...]} or [...]
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

        # ---------- Polling + Watchdog
        async def rest_poll(now=None):
            try:
                new_kegs = await fetch_kegs()
                for raw in new_kegs:
                    norm = _normalize_keg_dict(raw)
                    await _publish_keg(norm)
            except Exception as e:
                _LOGGER.debug("%s: REST poll error: %s", DOMAIN, e)

        async def watchdog(now=None):
            ts = state.get(LAST_UPDATE_KEY)
            if ts is None:
                return
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > DATA_STALE_SEC:
                _LOGGER.warning("%s: no updates for %.0fs, forcing REST poll", DOMAIN, age)
                try:
                    new_kegs = await fetch_kegs()
                    for raw in new_kegs:
                        norm = _normalize_keg_dict(raw)
                        await _publish_keg(norm)
                except Exception as e:
                    _LOGGER.error("%s: watchdog REST poll failed: %s", DOMAIN, e)

        # ---------- Services
        async def export_history(call: ServiceCall):
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
        hass.services.async_register(DOMAIN, "export_history", export_history)

        async def refresh_kegs(call: ServiceCall):
            new_kegs = await fetch_kegs()
            for raw in new_kegs:
                norm = _normalize_keg_dict(raw)
                await _publish_keg(norm)
            pn_create(hass, f"Refreshed {len(new_kegs)} kegs", title="Beer Keg Refresh")
        hass.services.async_register(DOMAIN, "refresh_kegs", refresh_kegs)

        async def on_stop(event):
            await state["store"].async_save(state["history"][-MAX_LOG_ENTRIES:])
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_stop)

        # ---------- Start after HA is up
        async def _start_after_started(event=None):
            await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
            try:
                initial = await fetch_kegs()
                for raw in initial:
                    norm = _normalize_keg_dict(raw)
                    await _publish_keg(norm)
                _LOGGER.info("%s: Initial REST refresh found %d kegs", DOMAIN, len(initial))
            except Exception as e:
                _LOGGER.warning("%s: Initial REST refresh failed: %s", DOMAIN, e)

            hass.async_create_task(connect_websocket())
            async_track_time_interval(hass, rest_poll, timedelta(seconds=REST_POLL_SECONDS))
            async_track_time_interval(hass, watchdog, timedelta(seconds=10))
            _LOGGER.info("%s: started background tasks", DOMAIN)

        if hass.state == "RUNNING":
            hass.async_create_task(_start_after_started())
        else:
            hass.bus.async_listen_once("homeassistant_started", _start_after_started)

        _LOGGER.info("%s: setup complete (WS+REST+poll+watchdog+oz)", DOMAIN)
        return True

    except Exception as e:
        _LOGGER.exception("%s: setup_entry crashed: %s", DOMAIN, e)
        return False


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    state = hass.data[DOMAIN].get(entry.entry_id)
    if not state:
        return True
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
