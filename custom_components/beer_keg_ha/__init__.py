from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
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
    PLATFORM_EVENT,
    DEVICES_UPDATE_EVENT,
    ATTR_LAST_UPDATE,
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

KG_TO_OZ = 35.274

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


def _parse_mmddyyyy(s: Any) -> Optional[datetime]:
    """Parse MM/DD/YYYY -> aware UTC datetime at 00:00:00."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        dt = datetime.strptime(s.strip(), DATE_FORMAT)
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _days_until(exp: Any) -> Optional[int]:
    dt = _parse_mmddyyyy(exp)
    if not dt:
        return None
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((dt - today).days)


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

    if amount_left is not None:
        if beer_left_unit in ("kg", "kilogram", "kilograms"):
            beer_remaining_kg = amount_left
        elif beer_left_unit in ("litre", "liter", "liters", "litres", "l"):
            liters_remaining = amount_left
            beer_remaining_kg = liters_remaining * DEFAULT_BEER_SG * WATER_DENSITY_KG_PER_L

    # If server reports kg, derive liters too (approx)
    if beer_remaining_kg is not None and liters_remaining is None:
        liters_remaining = beer_remaining_kg / (DEFAULT_BEER_SG * WATER_DENSITY_KG_PER_L)

    total_weight_kg: float | None = None
    if empty_keg_weight is not None and beer_remaining_kg is not None:
        total_weight_kg = empty_keg_weight + beer_remaining_kg

    return {
        "id": keg_id,
        "short_id": short_id,

        # raw fields
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

        # internal flattened
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


def _register_service_once(hass: HomeAssistant, name: str, handler, schema=None) -> None:
    if hass.services.has_service(DOMAIN, name):
        return
    hass.services.async_register(DOMAIN, name, handler, schema=schema)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ws_url: str | None = entry.data.get(CONF_WS_URL)
    if not ws_url:
        _LOGGER.error("%s: Missing ws_url", DOMAIN)
        return False

    hass.data.setdefault(DOMAIN, {})
    state: Dict[str, Any] = {
        "ws_url": ws_url,
        "data": {},
        "raw": {},
        "devices": [],
        "selected_device": None,  # used by your single manual select
        ATTR_LAST_UPDATE: None,

        # computed pour stats (per keg)
        "pour": {},

        # manual per-keg meta (persisted)
        "meta": {},
        "meta_store": Store(hass, 1, f"{DOMAIN}_meta_{entry.entry_id}"),
    }
    hass.data[DOMAIN][entry.entry_id] = state

    base = _rest_base_from_ws(ws_url)
    session = async_get_clientsession(hass)

    # ---- load persisted meta
    try:
        loaded = await state["meta_store"].async_load()
        if isinstance(loaded, dict):
            state["meta"] = loaded
    except Exception as e:
        _LOGGER.debug("%s: meta load failed: %s", DOMAIN, e)

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

    def _update_pour_stats(keg_id: str, norm: dict) -> None:
        """Compute last_pour_oz + daily_consumption_oz from drop in total_weight_kg."""
        total_kg = norm.get("total_weight_kg")
        if not isinstance(total_kg, (int, float)):
            norm["last_pour_oz"] = None
            norm["daily_consumption_oz"] = None
            return

        today = datetime.now(timezone.utc).date().isoformat()
        p = state["pour"].get(keg_id)

        if not p:
            p = state["pour"][keg_id] = {
                "last_total_kg": float(total_kg),
                "last_pour_oz": 0.0,
                "daily_oz": 0.0,
                "day": today,
            }
            norm["last_pour_oz"] = 0.0
            norm["daily_consumption_oz"] = 0.0
            return

        if p.get("day") != today:
            p["day"] = today
            p["daily_oz"] = 0.0

        prev = float(p.get("last_total_kg") or total_kg)
        curr = float(total_kg)
        delta_kg = prev - curr  # positive means poured

        if delta_kg > 0.02:  # jitter deadband
            oz = round(delta_kg * KG_TO_OZ, 1)
            p["last_pour_oz"] = oz
            p["daily_oz"] = round(float(p.get("daily_oz") or 0.0) + oz, 1)

        p["last_total_kg"] = curr

        norm["last_pour_oz"] = float(p.get("last_pour_oz") or 0.0)
        norm["daily_consumption_oz"] = float(p.get("daily_oz") or 0.0)

    def _apply_meta_and_expiration(keg_id: str, norm: dict) -> None:
        meta = (state.get("meta") or {}).get(keg_id, {})
        kegged = meta.get(ATTR_KEGGED_DATE)
        exp = meta.get(ATTR_EXPIRATION_DATE)

        norm[ATTR_KEGGED_DATE] = kegged if isinstance(kegged, str) else None
        norm[ATTR_EXPIRATION_DATE] = exp if isinstance(exp, str) else None
        norm[ATTR_DAYS_UNTIL_EXPIRATION] = _days_until(exp) if exp else None

    async def publish_kegs(payload_list: List[dict]) -> None:
        for raw in payload_list:
            if not isinstance(raw, dict):
                continue
            norm = _normalize_v2(raw)
            keg_id = norm["id"]

            _update_pour_stats(keg_id, norm)
            _apply_meta_and_expiration(keg_id, norm)

            state["raw"][keg_id] = raw
            state["data"][keg_id] = norm
            state[ATTR_LAST_UPDATE] = datetime.now(timezone.utc)
            hass.bus.async_fire(PLATFORM_EVENT, {"keg_id": keg_id})

        # keep devices list sane even if devices endpoint fails
        known = list(dict.fromkeys([*(state.get("devices") or []), *state["data"].keys()]))
        state["devices"] = known
        hass.bus.async_fire(DEVICES_UPDATE_EVENT, {"ids": known})

        # keep selected_device valid
        if state.get("selected_device") not in known:
            state["selected_device"] = known[0] if known else None

    async def rest_poll(_now=None) -> None:
        try:
            kegs = await fetch_kegs_rest()
            await publish_kegs(kegs)
        except Exception as e:
            _LOGGER.debug("%s: REST poll failed: %s", DOMAIN, e)

    async def devices_poll(_now=None) -> None:
        await fetch_devices()

    async def connect_websocket() -> None:
        """WS connects to ws_url and expects: [ {keg}, {keg} ]"""
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

    # -------------------------
    # Services helpers
    # -------------------------
    def _resolve_keg_id(call: ServiceCall) -> Optional[str]:
        if call.data.get("id"):
            return str(call.data["id"])
        if state.get("selected_device"):
            return str(state["selected_device"])
        devices = state.get("devices") or []
        return str(devices[0]) if devices else None

    async def _notify(title: str, msg: str) -> None:
        await pn_create(hass, msg, title=title)

    async def _post_cmd(keg_id: str, cmd: str, payload: dict | None = None) -> tuple[int, str]:
        url = f"{base}/api/kegs/{keg_id}/{cmd}"
        try:
            async with session.post(url, json=payload or {}) as resp:
                txt = await resp.text()
                return resp.status, txt
        except Exception as e:
            return 0, str(e)

    async def _get_connected() -> tuple[int, str]:
        url = f"{base}/api/kegs/connected"
        try:
            async with session.get(url) as resp:
                txt = await resp.text()
                return resp.status, txt
        except Exception as e:
            return 0, str(e)

    # -------------------------
    # Service: manual dates (id optional)
    # -------------------------
    _SCHEMA_SET_DATES = vol.Schema(
        {
            vol.Optional("id"): cv.string,
            vol.Optional("kegged_date"): cv.string,      # MM/DD/YYYY
            vol.Optional("expiration_date"): cv.string,  # MM/DD/YYYY
        }
    )

    async def set_keg_dates(call: ServiceCall) -> None:
        keg_id = _resolve_keg_id(call)
        if not keg_id:
            await _notify("Beer Keg Dates", "No keg id available (no devices discovered yet).")
            return

        kegged = call.data.get("kegged_date")
        exp = call.data.get("expiration_date")

        if kegged is not None and _parse_mmddyyyy(kegged) is None:
            await _notify("Beer Keg Dates", f"Invalid kegged_date. Use {DATE_FORMAT} (MM/DD/YYYY).")
            return
        if exp is not None and _parse_mmddyyyy(exp) is None:
            await _notify("Beer Keg Dates", f"Invalid expiration_date. Use {DATE_FORMAT} (MM/DD/YYYY).")
            return

        meta = state.setdefault("meta", {})
        meta.setdefault(keg_id, {})
        if kegged is not None:
            meta[keg_id][ATTR_KEGGED_DATE] = kegged
        if exp is not None:
            meta[keg_id][ATTR_EXPIRATION_DATE] = exp

        await state["meta_store"].async_save(meta)

        if keg_id in state.get("data", {}):
            _apply_meta_and_expiration(keg_id, state["data"][keg_id])
            hass.bus.async_fire(PLATFORM_EVENT, {"keg_id": keg_id})

        await _notify("Beer Keg Dates", f"Dates saved for {keg_id}")

    _register_service_once(hass, "set_keg_dates", set_keg_dates, schema=_SCHEMA_SET_DATES)

    # -------------------------
    # Services: Command API
    # -------------------------
    _SCHEMA_ID = vol.Schema({vol.Optional("id"): cv.string})
    _SCHEMA_FLOAT = vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.Coerce(float)})
    _SCHEMA_INT = vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.Coerce(int)})
    _SCHEMA_STR = vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): cv.string})

    async def cmd_tare(call: ServiceCall) -> None:
        keg_id = _resolve_keg_id(call)
        if not keg_id:
            await _notify("Beer Keg Command", "No keg id available.")
            return
        status, body = await _post_cmd(keg_id, "tare")
        await _notify("Beer Keg Command", f"tare ({keg_id}) -> {status}\n{body[:500]}")

    async def cmd_empty_keg(call: ServiceCall) -> None:
        keg_id = _resolve_keg_id(call)
        if not keg_id:
            await _notify("Beer Keg Command", "No keg id available.")
            return
        status, body = await _post_cmd(keg_id, "empty-keg", {"value": float(call.data["value"])})
        await _notify("Beer Keg Command", f"empty-keg ({keg_id}) -> {status}\n{body[:500]}")

    async def cmd_max_volume(call: ServiceCall) -> None:
        keg_id = _resolve_keg_id(call)
        if not keg_id:
            await _notify("Beer Keg Command", "No keg id available.")
            return
        status, body = await _post_cmd(keg_id, "max-keg-volume", {"value": float(call.data["value"])})
        await _notify("Beer Keg Command", f"max-keg-volume ({keg_id}) -> {status}\n{body[:500]}")

    async def cmd_temp_offset(call: ServiceCall) -> None:
        keg_id = _resolve_keg_id(call)
        if not keg_id:
            await _notify("Beer Keg Command", "No keg id available.")
            return
        status, body = await _post_cmd(keg_id, "temperature-offset", {"value": float(call.data["value"])})
        await _notify("Beer Keg Command", f"temperature-offset ({keg_id}) -> {status}\n{body[:500]}")

    async def cmd_calibrate_known(call: ServiceCall) -> None:
        keg_id = _resolve_keg_id(call)
        if not keg_id:
            await _notify("Beer Keg Command", "No keg id available.")
            return
        status, body = await _post_cmd(keg_id, "calibrate-known-weight", {"value": float(call.data["value"])})
        await _notify("Beer Keg Command", f"calibrate-known-weight ({keg_id}) -> {status}\n{body[:500]}")

    async def cmd_beer_style(call: ServiceCall) -> None:
        keg_id = _resolve_keg_id(call)
        if not keg_id:
            await _notify("Beer Keg Command", "No keg id available.")
            return
        status, body = await _post_cmd(keg_id, "beer-style", {"value": str(call.data["value"])})
        await _notify("Beer Keg Command", f"beer-style ({keg_id}) -> {status}\n{body[:500]}")

    async def cmd_date(call: ServiceCall) -> None:
        keg_id = _resolve_keg_id(call)
        if not keg_id:
            await _notify("Beer Keg Command", "No keg id available.")
            return
        status, body = await _post_cmd(keg_id, "date", {"value": str(call.data["value"])})
        await _notify("Beer Keg Command", f"date ({keg_id}) -> {status}\n{body[:500]}")

    async def cmd_unit(call: ServiceCall) -> None:
        keg_id = _resolve_keg_id(call)
        if not keg_id:
            await _notify("Beer Keg Command", "No keg id available.")
            return
        status, body = await _post_cmd(keg_id, "unit", {"value": str(call.data["value"]).lower()})
        await _notify("Beer Keg Command", f"unit ({keg_id}) -> {status}\n{body[:500]}")

    async def cmd_measure_unit(call: ServiceCall) -> None:
        keg_id = _resolve_keg_id(call)
        if not keg_id:
            await _notify("Beer Keg Command", "No keg id available.")
            return
        status, body = await _post_cmd(keg_id, "measure-unit", {"value": str(call.data["value"]).lower()})
        await _notify("Beer Keg Command", f"measure-unit ({keg_id}) -> {status}\n{body[:500]}")

    async def cmd_keg_mode(call: ServiceCall) -> None:
        keg_id = _resolve_keg_id(call)
        if not keg_id:
            await _notify("Beer Keg Command", "No keg id available.")
            return
        status, body = await _post_cmd(keg_id, "keg-mode", {"value": str(call.data["value"]).lower()})
        await _notify("Beer Keg Command", f"keg-mode ({keg_id}) -> {status}\n{body[:500]}")

    async def cmd_sensitivity(call: ServiceCall) -> None:
        keg_id = _resolve_keg_id(call)
        if not keg_id:
            await _notify("Beer Keg Command", "No keg id available.")
            return
        status, body = await _post_cmd(keg_id, "sensitivity", {"value": int(call.data["value"])})
        await _notify("Beer Keg Command", f"sensitivity ({keg_id}) -> {status}\n{body[:500]}")

    async def get_connected(_call: ServiceCall) -> None:
        status, body = await _get_connected()
        await _notify("Beer Keg Connected", f"connected -> {status}\n{body[:1000]}")

    _register_service_once(hass, "keg_tare", cmd_tare, schema=_SCHEMA_ID)
    _register_service_once(hass, "keg_set_empty_keg_weight", cmd_empty_keg, schema=_SCHEMA_FLOAT)
    _register_service_once(hass, "keg_set_max_keg_volume", cmd_max_volume, schema=_SCHEMA_FLOAT)
    _register_service_once(hass, "keg_set_temperature_offset", cmd_temp_offset, schema=_SCHEMA_FLOAT)
    _register_service_once(hass, "keg_calibrate_known_weight", cmd_calibrate_known, schema=_SCHEMA_FLOAT)
    _register_service_once(hass, "keg_set_beer_style", cmd_beer_style, schema=_SCHEMA_STR)
    _register_service_once(hass, "keg_set_date", cmd_date, schema=_SCHEMA_STR)
    _register_service_once(hass, "keg_set_unit_system", cmd_unit, schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.In(["metric", "us"])}))
    _register_service_once(hass, "keg_set_measure_unit", cmd_measure_unit, schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.In(["weight", "volume"])}))
    _register_service_once(hass, "keg_set_mode", cmd_keg_mode, schema=vol.Schema({vol.Optional("id"): cv.string, vol.Required("value"): vol.In(["beer", "co2"])}))
    _register_service_once(hass, "keg_set_sensitivity", cmd_sensitivity, schema=_SCHEMA_INT)
    _register_service_once(hass, "keg_get_connected", get_connected, schema=vol.Schema({}))

    async def on_stop(_event) -> None:
        try:
            await state["meta_store"].async_save(state.get("meta", {}))
        except Exception:
            pass

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_stop)

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
