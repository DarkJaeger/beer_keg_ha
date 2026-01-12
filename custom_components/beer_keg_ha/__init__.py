from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from urllib.parse import urlparse, urlunparse

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_WS_URL,
    PLATFORM_EVENT,
    DEVICES_UPDATE_EVENT,
    ATTR_LAST_UPDATE,
    # date/meta keys
    ATTR_KEGGED_DATE,
    ATTR_EXPIRATION_DATE,
    ATTR_DAYS_UNTIL_EXPIRATION,
    DATE_FORMAT,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "select"]

REST_POLL_SECONDS = 10
DEVICES_REFRESH_SEC = 60
WS_RECONNECT_DELAY = 5

# Used only when server reports beer_left_unit == liters
DEFAULT_BEER_SG = 1.010
WATER_DENSITY_KG_PER_L = 0.998

KG_TO_OZ = 35.274  # for computed daily consumption / pours


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


def _parse_mmddyyyy(s: str | None) -> datetime | None:
    """Parse MM/DD/YYYY into a datetime (local tz, midnight)."""
    if not s:
        return None
    try:
        d = datetime.strptime(s.strip(), DATE_FORMAT).date()
        return dt_util.start_of_local_day(datetime.combine(d, datetime.min.time()))
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

    # stores manual meta: kegged/expiration dates
    meta_store: Store = Store(hass, 1, f"{DOMAIN}_meta")
    loaded_meta = await meta_store.async_load()
    if not isinstance(loaded_meta, dict):
        loaded_meta = {}

    state: Dict[str, Any] = {
        "ws_url": ws_url,
        "data": {},      # normalized per-keg values used by entities
        "raw": {},       # raw payload per keg (optional)
        "devices": [],
        "connected": [],  # from /api/kegs/connected if available

        "meta": loaded_meta,   # { keg_id: {kegged_date, expiration_date} }
        "meta_store": meta_store,

        # computed daily stats (runtime)
        "stats": {},     # { keg_id: {last_weight, daily_oz, day_key, last_pour_oz} }

        ATTR_LAST_UPDATE: None,
    }
    hass.data[DOMAIN][entry.entry_id] = state

    base = _rest_base_from_ws(ws_url)
    session = async_get_clientsession(hass)

    def _compute_days_until_expiration(exp_str: str | None) -> int | None:
        exp_dt = _parse_mmddyyyy(exp_str)
        if exp_dt is None:
            return None
        today = dt_util.now().date()
        exp_date = exp_dt.date()
        return (exp_date - today).days

    def _apply_meta_and_stats(keg_id: str, norm: dict) -> dict:
        """Merge manual meta + computed stats into the normalized dict."""
        meta = (state.get("meta") or {}).get(keg_id) or {}
        kegged_date = meta.get(ATTR_KEGGED_DATE)
        expiration_date = meta.get(ATTR_EXPIRATION_DATE)

        days = _compute_days_until_expiration(expiration_date)

        stats = state.setdefault("stats", {}).setdefault(
            keg_id,
            {"last_weight": None, "daily_oz": 0.0, "day_key": None, "last_pour_oz": 0.0},
        )

        today_key = dt_util.now().date().isoformat()
        if stats.get("day_key") != today_key:
            stats["day_key"] = today_key
            stats["daily_oz"] = 0.0
            stats["last_pour_oz"] = 0.0

        w = norm.get("total_weight_kg")
        if isinstance(w, (int, float)):
            prev = stats.get("last_weight")
            if isinstance(prev, (int, float)):
                delta_kg = float(prev) - float(w)
                if delta_kg > 0.01:
                    last_pour_oz = round(delta_kg * KG_TO_OZ, 1)
                    stats["last_pour_oz"] = last_pour_oz
                    stats["daily_oz"] = round(float(stats.get("daily_oz", 0.0)) + last_pour_oz, 1)
                else:
                    stats["last_pour_oz"] = 0.0
            stats["last_weight"] = float(w)

        norm[ATTR_KEGGED_DATE] = kegged_date
        norm[ATTR_EXPIRATION_DATE] = expiration_date
        norm[ATTR_DAYS_UNTIL_EXPIRATION] = days
        norm["daily_consumption_oz"] = stats.get("daily_oz", 0.0)
        norm["last_pour_oz"] = stats.get("last_pour_oz", 0.0)

        return norm

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

    async def fetch_connected() -> List[str]:
        """GET /api/kegs/connected -> list of currently connected kegs."""
        url = f"{base}/api/kegs/connected"
        try:
            async with session.get(url) as resp:
                if resp.status == 404:
                    return state.get("connected", [])
                if resp.status != 200:
                    return state.get("connected", [])
                data = await resp.json()
                if isinstance(data, list):
                    ids = [str(x) for x in data]
                    state["connected"] = ids
                    return ids
        except Exception:
            pass
        return state.get("connected", [])

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
            norm = _apply_meta_and_stats(keg_id, norm)

            state["raw"][keg_id] = raw
            state["data"][keg_id] = norm
            state[ATTR_LAST_UPDATE] = datetime.now(timezone.utc)
            hass.bus.async_fire(PLATFORM_EVENT, {"keg_id": keg_id})

        # keep devices list sane even if devices endpoint fails
        known = set(state.get("devices") or [])
        for kid in state["data"].keys():
            known.add(kid)

        # also fold in /connected if present
        try:
            connected = await fetch_connected()
            for kid in connected:
                known.add(kid)
        except Exception:
            connected = state.get("connected", [])

        state["devices"] = list(known)
        hass.bus.async_fire(DEVICES_UPDATE_EVENT, {"ids": list(known), "connected": list(connected)})

    async def rest_poll(_now=None) -> None:
        try:
            kegs = await fetch_kegs_rest()
            await publish_kegs(kegs)
        except Exception as e:
            _LOGGER.debug("%s: REST poll failed: %s", DOMAIN, e)

    async def devices_poll(_now=None) -> None:
        await fetch_devices()
        await fetch_connected()

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

                        if isinstance(data, list):
                            await publish_kegs(data)
                        elif isinstance(data, dict) and isinstance(data.get("kegs"), list):
                            await publish_kegs(data["kegs"])

            except Exception as e:
                _LOGGER.warning("%s: WS disconnected (%s). Reconnecting...", DOMAIN, e)
                await asyncio.sleep(WS_RECONNECT_DELAY)

    # ----------------------------
    # Manual date services
    # ----------------------------
    def _resolve_keg_id(call: ServiceCall) -> str | None:
        kid = call.data.get("id")
        if kid:
            return str(kid)
        keys = list((state.get("data") or {}).keys())
        if len(keys) == 1:
            return keys[0]
        return None

    async def set_keg_dates(call: ServiceCall) -> None:
        keg_id = _resolve_keg_id(call)
        if not keg_id:
            _LOGGER.warning("%s: set_keg_dates: missing id (and not exactly one keg present)", DOMAIN)
            return

        kegged = call.data.get("kegged_date")
        expires = call.data.get("expiration_date")

        if kegged is not None and _parse_mmddyyyy(str(kegged)) is None:
            raise vol.Invalid(f"kegged_date must be MM/DD/YYYY (got '{kegged}')")
        if expires is not None and _parse_mmddyyyy(str(expires)) is None:
            raise vol.Invalid(f"expiration_date must be MM/DD/YYYY (got '{expires}')")

        meta = state.setdefault("meta", {})
        cur = meta.get(keg_id) or {}
        if kegged is not None:
            cur[ATTR_KEGGED_DATE] = str(kegged)
        if expires is not None:
            cur[ATTR_EXPIRATION_DATE] = str(expires)
        meta[keg_id] = cur

        await state["meta_store"].async_save(meta)

        if keg_id in state.get("data", {}):
            state["data"][keg_id] = _apply_meta_and_stats(keg_id, state["data"][keg_id])
            hass.bus.async_fire(PLATFORM_EVENT, {"keg_id": keg_id})

    async def clear_keg_dates(call: ServiceCall) -> None:
        keg_id = _resolve_keg_id(call)
        if not keg_id:
            _LOGGER.warning("%s: clear_keg_dates: missing id (and not exactly one keg present)", DOMAIN)
            return
        meta = state.setdefault("meta", {})
        if keg_id in meta:
            meta.pop(keg_id, None)
            await state["meta_store"].async_save(meta)
        if keg_id in state.get("data", {}):
            state["data"][keg_id][ATTR_KEGGED_DATE] = None
            state["data"][keg_id][ATTR_EXPIRATION_DATE] = None
            state["data"][keg_id][ATTR_DAYS_UNTIL_EXPIRATION] = None
            hass.bus.async_fire(PLATFORM_EVENT, {"keg_id": keg_id})

    hass.services.async_register(
        DOMAIN,
        "set_keg_dates",
        set_keg_dates,
        schema=vol.Schema(
            {
                vol.Optional("id"): cv.string,
                vol.Optional("kegged_date"): cv.string,
                vol.Optional("expiration_date"): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "clear_keg_dates",
        clear_keg_dates,
        schema=vol.Schema({vol.Optional("id"): cv.string}),
    )

    # ----------------------------
    # Keg Command API services
    # ----------------------------
    async def _post_cmd(keg_id: str, cmd: str, payload: dict | None = None) -> tuple[int, str]:
        url = f"{base}/api/kegs/{keg_id}/{cmd}"
        async with session.post(url, json=payload or {}) as resp:
            txt = await resp.text()
            return resp.status, txt

    async def _cmd(call: ServiceCall, cmd: str, payload: dict | None = None) -> None:
        keg_id = _resolve_keg_id(call)
        if not keg_id:
            _LOGGER.warning("%s: command %s: missing id", DOMAIN, cmd)
            return
        status, body = await _post_cmd(keg_id, cmd, payload)
        if status == 404:
            _LOGGER.warning("%s: command API not supported by server (404) for %s", DOMAIN, cmd)
            return
        if status >= 300:
            _LOGGER.warning("%s: command %s failed for %s -> HTTP %s: %s", DOMAIN, cmd, keg_id, status, body[:200])
            return

        # If command likely changes readings, refresh once
        await rest_poll()

    async def keg_tare(call: ServiceCall) -> None:
        await _cmd(call, "tare")

    async def keg_empty_keg(call: ServiceCall) -> None:
        await _cmd(call, "empty-keg", {"value": float(call.data["value"])})

    async def keg_max_keg_volume(call: ServiceCall) -> None:
        await _cmd(call, "max-keg-volume", {"value": float(call.data["value"])})

    async def keg_temperature_offset(call: ServiceCall) -> None:
        await _cmd(call, "temperature-offset", {"value": float(call.data["value"])})

    async def keg_calibrate_known_weight(call: ServiceCall) -> None:
        await _cmd(call, "calibrate-known-weight", {"value": float(call.data["value"])})

    async def keg_beer_style(call: ServiceCall) -> None:
        await _cmd(call, "beer-style", {"value": str(call.data["value"])})

    async def keg_date(call: ServiceCall) -> None:
        # server decides format; we pass through string
        await _cmd(call, "date", {"value": str(call.data["value"])})

    async def keg_unit(call: ServiceCall) -> None:
        await _cmd(call, "unit", {"value": str(call.data["value"]).lower()})

    async def keg_measure_unit(call: ServiceCall) -> None:
        await _cmd(call, "measure-unit", {"value": str(call.data["value"]).lower()})

    async def keg_keg_mode(call: ServiceCall) -> None:
        await _cmd(call, "keg-mode", {"value": str(call.data["value"]).lower()})

    async def keg_sensitivity(call: ServiceCall) -> None:
        await _cmd(call, "sensitivity", {"value": int(call.data["value"])})

    hass.services.async_register(DOMAIN, "keg_tare", keg_tare, schema=vol.Schema({vol.Optional("id"): cv.string}))
    hass.services.async_register(
        DOMAIN,
        "keg_set_empty_keg_weight",
        keg_empty_keg,
        schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.Coerce(float)}),
    )
    hass.services.async_register(
        DOMAIN,
        "keg_set_max_keg_volume",
        keg_max_keg_volume,
        schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.Coerce(float)}),
    )
    hass.services.async_register(
        DOMAIN,
        "keg_set_temperature_offset",
        keg_temperature_offset,
        schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.Coerce(float)}),
    )
    hass.services.async_register(
        DOMAIN,
        "keg_calibrate_known_weight",
        keg_calibrate_known_weight,
        schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.Coerce(float)}),
    )
    hass.services.async_register(
        DOMAIN,
        "keg_set_beer_style",
        keg_beer_style,
        schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        "keg_set_date",
        keg_date,
        schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        "keg_set_unit_system",
        keg_unit,
        schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.In(["metric", "us"])}),
    )
    hass.services.async_register(
        DOMAIN,
        "keg_set_measure_unit",
        keg_measure_unit,
        schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.In(["weight", "volume"])}),
    )
    hass.services.async_register(
        DOMAIN,
        "keg_set_mode",
        keg_keg_mode,
        schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.In(["beer", "co2"])}),
    )
    hass.services.async_register(
        DOMAIN,
        "keg_set_sensitivity",
        keg_sensitivity,
        schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.Coerce(int)}),
    )

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
