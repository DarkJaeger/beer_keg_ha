# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Home Assistant custom integration** (`beer_keg_ha`) for monitoring beer kegs via the Open Plaato Keg server (API v2). It connects over WebSocket (primary) with REST polling fallback, and exposes keg data as HA entities. Distributed via HACS as a custom repository.

**Domain:** `beer_keg_ha`
**HA minimum version:** 2024.6.0

## Development

This is a pure Python Home Assistant integration with no build step, no tests, and no linter configured. To test changes, copy `custom_components/beer_keg_ha/` into a Home Assistant instance's `custom_components/` directory and restart HA.

Version is tracked in `custom_components/beer_keg_ha/manifest.json` (the `version` field). Update it when releasing changes.

## Architecture

### Data Flow

1. **`__init__.py`** is the core — it manages the WebSocket connection, REST polling, and all state. On setup (`async_setup_entry`), it:
   - Normalizes the WS URL (ensures `/ws` path, converts `http` to `ws`)
   - Derives the REST base URL from the WS URL
   - Starts a persistent WebSocket listener (`connect_websocket`) with auto-reconnect
   - Polls `/api/kegs` every 10s and `/api/kegs/devices` every 60s as fallback
   - Stores all state in `hass.data[DOMAIN][entry.entry_id]`

2. **`_normalize_v2()`** transforms raw API payloads into a canonical dict with computed fields (beer remaining in kg, liters, total weight). All internal storage uses metric (kg, liters, Celsius).

3. **`publish_kegs()`** is the single update path — both WS and REST feed through it. It calls `_update_pour_stats()` and `_apply_meta_and_expiration()`, then fires `PLATFORM_EVENT` to notify entities.

4. **Platform entities** (`sensor.py`, `binary_sensor.py`, `select.py`, `text.py`, `date.py`, `number.py`) are event-driven (`_attr_should_poll = False`). They listen for `PLATFORM_EVENT` bus events and call `async_write_ha_state()` to update.

### State Structure (`hass.data[DOMAIN][entry_id]`)

- `data`: `Dict[keg_id, normalized_dict]` — current keg state
- `raw`: `Dict[keg_id, raw_api_dict]` — unprocessed API data
- `devices`: `List[str]` — known device IDs
- `pour`: `Dict[keg_id, pour_tracking_dict]` — pour detection state (last weight, daily totals)
- `meta`: `Dict[keg_id, {kegged_date, ...}]` — manually-set metadata, persisted via `Store`
- `meta_store`: HA `Store` instance for meta persistence

### Key Design Decisions

- **Pour detection is weight-based**, not from the server's `is_pouring` flag. A weight drop > 0.02 kg triggers the pouring binary sensor, which stays ON for 4 seconds after the last drop.
- **REST never overwrites WS pouring state** — when `source="rest"`, the existing `is_pouring` value is preserved to avoid REST's stale "0" overriding a live WS update.
- **Unit conversion happens at the entity level** (`sensor.py`), not in the normalized data. Internal data is always metric. The `_should_use_us()` and `_should_use_ml()` heuristics inspect server-reported unit fields to decide display units dynamically.
- **Entities are created lazily** — when a new `keg_id` appears in data, each platform's `_on_update` callback creates entities for that keg on the fly.
- **Persistence** uses two stores: `meta_store` (for manually-set keg dates) and `prefs_store` (for display units, keg config, tap text/numbers, smoothing settings).

### Platform Modules

- **`sensor.py`**: ~40 sensor types per keg defined in `SENSOR_TYPES` dict. Handles unit conversion (kg/lb, L/gal, oz/ml) based on server unit hints.
- **`binary_sensor.py`**: `KegPouringByWeightBinarySensor` — weight-drop pour detection with timed auto-off.
- **`select.py`**: Device selector for choosing active keg.
- **`text.py`**: Per-keg text fields (name, SG, OG) + tap list text fields (12 taps x 5 fields). Tap notes support up to 4096 chars stored in attributes.
- **`number.py`**: Per-keg calibration numbers + global smoothing controls + tap-level numbers (ABV, IBU).
- **`date.py`**: Per-keg date entities (kegged date, expiration date) backed by `keg_config` in prefs.
- **`config_flow.py`**: Simple single-step flow asking for WS URL. Options flow exposes advanced settings.

### Constants (`const.py`)

- `PLATFORM_EVENT` (`beer_keg_ha_update`): Fired on every keg data update; entities listen for this.
- `DEVICES_UPDATE_EVENT` (`beer_keg_ha_devices_update`): Fired when the device list changes.
- Physical constants: `DEFAULT_BEER_SG = 1.010`, `WATER_DENSITY_KG_PER_L = 0.998`.

### Services

Registered in `__init__.py`. `set_keg_dates` is the main custom service (sets kegged date, auto-computes expiration +6 months). Other services are server command passthroughs (tare, set empty weight, etc.).
