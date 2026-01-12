from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from urllib.parse import urlparse, urlunparse

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, CONF_WS_URL, PLATFORM_EVENT, DEVICES_UPDATE_EVENT, ATTR_LAST_UPDATE

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "select"]

REST_POLL_SECONDS = 10
DEVICES_REFRESH_SEC = 60
WS_RECONNECT_DELAY = 5

# Used only when server reports beer_left_unit == liters
DEFAULT_BEER_SG = 1.010
WATER_DENSITY_KG_PER_L = 0.998


def _rest_base_from_ws(ws: str) -> str:
    u = urlparse(ws)
    scheme = "http" if u.scheme == "ws" else "https" if u.scheme == "wss" else "http"
    return urlunparse((scheme, u.netloc, "", "", "", ""))


def _coerce_float(v: Any) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


def _coerce_int(v: Any) -> int | None:
    try:
        return int(float(v))
    except Exception:
        return None


def _parse_temp_from_string(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace("°C", "").replace("°F", "").strip())
    except Exception:
        return None

def _normalize_v2(keg: dict) -> dict:
    """Normalize ONE keg payload from WS/REST (v2 only)."""
    keg_id = str(keg.get("id", "unknown"))
    short_id = keg_id[:4]

    beer_left_unit = str(keg.get("beer_left_unit") or "").strip().lower()
    temp_unit = str(keg.get("temperature_unit") or "").strip()
    volume_unit = str(keg.get("volume_unit") or "").strip().lower()

    empty_keg_weight = _coerce_float(keg.get("empty_keg_weight"))
    if empty_keg_weight is not None:
        empty_keg_weight = max(0.0, empty_keg_weight)

    amount_left = _coerce_float(keg.get("amount_left"))
    if amount_left is not None:
        amount_left = max(0.0, amount_left)  # server can send negatives

    percent_left = _coerce_float(keg.get("percent_of_beer_left"))
    if percent_left is not None:
        percent_left = max(0.0, min(100.0, percent_left))

    # temps
    keg_temp = _coerce_float(keg.get("keg_temperature"))
    chip_temp = _parse_temp_from_string(str(keg.get("chip_temperature_string") or ""))

    wifi = _coerce_int(keg.get("wifi_signal_strength"))

    # -------- internal flatten --------
    internal = keg.get("internal") if isinstance(keg.get("internal"), dict) else {}
    internal_ver = internal.get("ver")
    internal_fw = internal.get("fw")
    internal_dev = internal.get("dev")
    internal_build = internal.get("build")
    internal_tmpl = internal.get("tmpl")
    internal_hbeat = _coerce_int(internal.get("h-beat"))

    # -------- derived units --------
    liters_remaining: float | None = None
    beer_remaining_kg: float | None = None

    # amount_left meaning depends on beer_left_unit
    if amount_left is not None:
        if beer_left_unit in ("kg", "kilogram", "kilograms"):
            beer_remaining_kg = amount_left
        elif beer_left_unit in ("litre", "liter", "liters", "litres", "l"):
            liters_remaining = amount_left
            beer_remaining_kg = liters_remaining * DEFAULT_BEER_SG * WATER_DENSITY_KG_PER_L

    # If server reports kg, derive liters too (approx) using SG*density
    if beer_remaining_kg is not None and liters_remaining is None:
        liters_remaining = beer_remaining_kg / (DEFAULT_BEER_SG * WATER_DENSITY_KG_PER_L)

    total_weight_kg: float | None = None
    if empty_keg_weight is not None and beer_remaining_kg is not None:
        total_weight_kg = empty_keg_weight + beer_remaining_kg

    return {
        "id": keg_id,
        "short_id": short_id,

        # raw fields (as sensors)
        "firmware_version": keg.get("firmware_version"),
        "chip_temperature_string": keg.get("chip_temperature_string"),
        "max_temperature": _coerce_float(keg.get("max_temperature")),
        "min_temperature": _coerce_float(keg.get("min_temperature")),
        "leak_detection": str(keg.get("leak_detection") or "0"),
        "volume_unit": volume_unit,
        "wifi_signal_strength": wifi,
        "temperature_unit": temp_unit,
        "beer_left_unit": beer_left_unit,
        "keg_temperature_string": keg.get("keg_temperature_string"),
        "fg": _coerce_int(keg.get("fg")),
        "og": _coerce_int(keg.get("og")),
        "last_pour": _coerce_float(keg.get("last_pour")),
        "last_pour_string": keg.get("last_pour_string"),
        "is_pouring": str(keg.get("is_pouring") or "0"),
        "percent_of_beer_left": percent_left,
        "temperature_offset": _coerce_float(keg.get("temperature_offset")),
        "measure_unit": _coerce_int(keg.get("measure_unit")),
        "max_keg_volume": _coerce_float(keg.get("max_keg_volume")),
        "empty_keg_weight": empty_keg_weight,
        "amount_left": amount_left,
        "unit": _coerce_int(keg.get("unit")),

        # flattened internal fields (for sensors)
        "internal_ver": internal_ver,
        "internal_fw": internal_fw,
        "internal_dev": internal_dev,
        "internal_build": internal_build,
        "internal_tmpl": internal_tmpl,
        "internal_hbeat": internal_hbeat,

        # numeric temps
        "keg_temperature_c": keg_temp,
        "chip_temperature_c": chip_temp,

        # derived
        "liters_remaining": round(liters_remaining, 3) if isinstance(liters_remaining, float) else None,
        "beer_remaining_kg": round(beer_remaining_kg, 3) if isinstance(beer_remaining_kg, float) else None,
        "total_weight_kg": round(total_weight_kg, 3) if isinstance(total_weight_kg, float) else None,
    }
   
async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ws_url: str | None = entry.data.get(CONF_WS_URL)
    if not ws_url:
        _LOGGER.error("%s: Missing ws_url", DOMAIN)
        return False

    hass.data.setdefault(DOMAIN, {})
    state: Dict[str, Any] = {
        "ws_url": ws_url,
        "data": {},     # normalized per-keg values used by entities
        "raw": {},      # raw payload per keg (optional)
        "devices": [],
        ATTR_LAST_UPDATE: None,
    }
    hass.data[DOMAIN][entry.entry_id] = state

    base = _rest_base_from_ws(ws_url)
    session = async_get_clientsession(hass)

    async def fetch_devices() -> List[str]:
        url = f"{base}/api/kegs/devices"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return state.get("devices", [])
                data = await resp.json()
                if isinstance(data, list):
                    ids = [str(x) for x in data]
                    state["devices"] = ids
                    hass.bus.async_fire(DEVICES_UPDATE_EVENT, {"ids": ids})
                    return ids
        except Exception:
            pass
        return state.get("devices", [])

    async def fetch_kegs_rest() -> List[dict]:
        url = f"{base}/api/kegs"
        async with session.get(url) as resp:
            if resp.status != 200:
                txt = await resp.text()
                raise RuntimeError(f"GET {url} -> {resp.status}: {txt[:200]}")
            data = await resp.json()
            if not isinstance(data, list):
                raise RuntimeError("Expected list from /api/kegs")
            return data

    async def publish_kegs(payload_list: List[dict]) -> None:
        for raw in payload_list:
            if not isinstance(raw, dict):
                continue
            norm = _normalize_v2(raw)
            keg_id = norm["id"]
            state["raw"][keg_id] = raw
            state["data"][keg_id] = norm
            state[ATTR_LAST_UPDATE] = datetime.now(timezone.utc)
            hass.bus.async_fire(PLATFORM_EVENT, {"keg_id": keg_id})

        # keep devices list sane even if devices endpoint fails
        known = set(state.get("devices") or [])
        for kid in state["data"].keys():
            known.add(kid)
        state["devices"] = list(known)
        hass.bus.async_fire(DEVICES_UPDATE_EVENT, {"ids": list(known)})

    async def rest_poll(_now=None) -> None:
        try:
            kegs = await fetch_kegs_rest()
            await publish_kegs(kegs)
        except Exception as e:
            _LOGGER.debug("%s: REST poll failed: %s", DOMAIN, e)

    async def devices_poll(_now=None) -> None:
        await fetch_devices()

    async def connect_websocket() -> None:
        """WS connects to ws_url and expects messages like: [ {keg}, {keg} ]"""
        while True:
            try:
                _LOGGER.info("%s: Connecting WS -> %s", DOMAIN, ws_url)
                async with session.ws_connect(ws_url, heartbeat=20) as ws:
                    _LOGGER.info("%s: WS connected", DOMAIN)

                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            data = json.loads(msg.data)
                        except Exception:
                            continue

                        # Your server sends a list of kegs
                        if isinstance(data, list):
                            await publish_kegs(data)
                        # (optional) support { "kegs": [...] }
                        elif isinstance(data, dict) and isinstance(data.get("kegs"), list):
                            await publish_kegs(data["kegs"])

            except Exception as e:
                _LOGGER.warning("%s: WS disconnected (%s). Reconnecting...", DOMAIN, e)
                await asyncio.sleep(WS_RECONNECT_DELAY)

    async def _start(_event=None) -> None:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await devices_poll()
        await rest_poll()

        hass.async_create_task(connect_websocket())
        async_track_time_interval(hass, rest_poll, timedelta(seconds=REST_POLL_SECONDS))
        async_track_time_interval(hass, devices_poll, timedelta(seconds=DEVICES_REFRESH_SEC))

        _LOGGER.info("%s: v2-only WS+REST started", DOMAIN)

    if hass.state == "RUNNING":
        hass.async_create_task(_start())
    else:
        hass.bus.async_listen_once("homeassistant_started", _start)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded
