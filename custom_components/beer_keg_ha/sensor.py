from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Set

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    PLATFORM_EVENT,
    AIRLOCK_EVENT,
    ATTR_LAST_UPDATE,
    ATTR_KEGGED_DATE,
    ATTR_EXPIRATION_DATE,
    ATTR_DAYS_UNTIL_EXPIRATION,
)

_LOGGER = logging.getLogger(__name__)

KG_TO_LB = 2.20462262185
L_TO_GAL = 0.26417205236  # US liquid gallon
OZ_TO_ML = 29.5735295625  # US fl oz -> mL
OZ_TO_L = OZ_TO_ML / 1000  # US fl oz -> liters


def _should_use_us(d: Dict[str, Any]) -> bool:
    """
    Heuristic: if server is operating in US-style volume units, use US display.
    This avoids relying on the numeric 'unit' field mapping (which varies).
    """
    volume_unit = str(d.get("volume_unit") or "").strip().lower()
    beer_left_unit = str(d.get("beer_left_unit") or "").strip().lower()
    temp_unit = str(d.get("temperature_unit") or "").strip().lower()

    if volume_unit in ("gal", "gallon", "gallons", "oz", "ounce", "ounces"):
        return True
    if beer_left_unit in ("gal", "gallon", "gallons", "oz", "ounce", "ounces"):
        return True
    if temp_unit in ("f", "°f"):
        return True

    return False


def _should_use_ml(d: Dict[str, Any]) -> bool:
    """
    Pour stats unit:
      - If server is metric (liters/ml) => display mL
      - If server is US (gallons/oz) => display oz
    """
    # If US mode, never show mL
    if _should_use_us(d):
        return False

    volume_unit = str(d.get("volume_unit") or "").strip().lower()
    beer_left_unit = str(d.get("beer_left_unit") or "").strip().lower()
    last_pour_string = str(d.get("last_pour_string") or "").strip().lower()

    if "ml" in last_pour_string:
        return True
    if "oz" in last_pour_string:
        return False

    if volume_unit in ("l", "liter", "litre", "liters", "litres"):
        return True
    if beer_left_unit in ("l", "liter", "litre", "liters", "litres"):
        return True

    return False


SENSOR_TYPES: Dict[str, Dict[str, Any]] = {
    # -------------------------
    # Computed (derived)
    # -------------------------
    "total_weight_kg": {"name": "Total Weight", "key": "total_weight_kg", "icon": "mdi:scale", "device_class": "weight", "state_class": "measurement", "unit": "kg", "round": 3},
    "beer_remaining_kg": {"name": "Beer Remaining", "key": "beer_remaining_kg", "icon": "mdi:beer", "device_class": "weight", "state_class": "measurement", "unit": "kg", "round": 3},
    "liters_remaining": {"name": "Beer Remaining", "key": "liters_remaining", "icon": "mdi:cup", "device_class": "volume", "state_class": None, "unit": "L", "round": 3},

    "last_pour_oz": {"name": "Last Pour", "key": "last_pour_oz", "icon": "mdi:cup-water", "device_class": None, "state_class": "measurement", "unit": "oz", "round": 1},
    "daily_consumption_oz": {"name": "Daily Consumption", "key": "daily_consumption_oz", "icon": "mdi:beer", "device_class": None, "state_class": "measurement", "unit": "oz", "round": 1},

    "kegged_date": {"name": "Kegged Date", "key": ATTR_KEGGED_DATE, "icon": "mdi:calendar-start", "device_class": None, "state_class": None, "unit": None, "round": None},
    "expiration_date": {"name": "Expiration Date", "key": ATTR_EXPIRATION_DATE, "icon": "mdi:calendar-end", "device_class": None, "state_class": None, "unit": None, "round": None},
    "days_until_expiration": {"name": "Days Until Expiration", "key": ATTR_DAYS_UNTIL_EXPIRATION, "icon": "mdi:timer-sand", "device_class": None, "state_class": "measurement", "unit": "d", "round": None},

    # -------------------------
    # Server-provided "my_*" metadata
    # -------------------------
    "my_beer_style": {"name": "Beer Style", "key": "my_beer_style", "icon": "mdi:tag", "device_class": None, "state_class": None, "unit": None, "round": None},
    "my_keg_date": {"name": "Keg Date", "key": "my_keg_date", "icon": "mdi:calendar", "device_class": None, "state_class": None, "unit": None, "round": None},
    "my_og": {"name": "OG", "key": "my_og", "icon": "mdi:alpha-o-circle", "device_class": None, "state_class": None, "unit": None, "round": 3},
    "my_fg": {"name": "FG", "key": "my_fg", "icon": "mdi:alpha-f-circle", "device_class": None, "state_class": None, "unit": None, "round": 3},
    "my_abv": {"name": "ABV", "key": "my_abv", "icon": "mdi:percent", "device_class": None, "state_class": None, "unit": "%", "round": 2},

    # -------------------------
    # Raw v2 fields
    # -------------------------
    "empty_keg_weight": {"name": "Empty Keg Weight", "key": "empty_keg_weight", "icon": "mdi:weight", "device_class": "weight", "state_class": "measurement", "unit": "kg", "round": 3},
    "amount_left": {"name": "Amount Left (Raw)", "key": "amount_left", "icon": "mdi:cup", "device_class": None, "state_class": None, "unit": None, "round": 3},
    "percent_of_beer_left": {"name": "Percent of Beer Left", "key": "percent_of_beer_left", "icon": "mdi:percent", "device_class": None, "state_class": "measurement", "unit": "%", "round": 1},

    "keg_temperature_c": {"name": "Temperature", "key": "keg_temperature_c", "icon": "mdi:thermometer", "device_class": "temperature", "state_class": "measurement", "unit": "°C", "round": 2},
    "chip_temperature_c": {"name": "Chip Temperature", "key": "chip_temperature_c", "icon": "mdi:chip", "device_class": "temperature", "state_class": "measurement", "unit": "°C", "round": 2},
    "temperature_offset": {"name": "Temperature Offset", "key": "temperature_offset", "icon": "mdi:thermometer-plus", "device_class": "temperature", "state_class": "measurement", "unit": "°C", "round": 3},
    "min_temperature": {"name": "Min Temperature", "key": "min_temperature", "icon": "mdi:thermometer-low", "device_class": "temperature", "state_class": "measurement", "unit": "°C", "round": 3},
    "max_temperature": {"name": "Max Temperature", "key": "max_temperature", "icon": "mdi:thermometer-high", "device_class": "temperature", "state_class": "measurement", "unit": "°C", "round": 3},

    "wifi_signal_strength": {"name": "WiFi Signal", "key": "wifi_signal_strength", "icon": "mdi:wifi", "device_class": None, "state_class": "measurement", "unit": "%", "round": None},

    "leak_detection": {"name": "Leak Detection", "key": "leak_detection", "icon": "mdi:water-alert", "device_class": None, "state_class": None, "unit": None, "round": None},

    "last_pour": {"name": "Last Pour (Raw)", "key": "last_pour", "icon": "mdi:cup-water", "device_class": None, "state_class": "measurement", "unit": None, "round": 3},
    "last_pour_string": {"name": "Last Pour", "key": "last_pour_string", "icon": "mdi:cup-water", "device_class": None, "state_class": None, "unit": None, "round": None},
    "last_pour_time": {"name": "Last Pour Time", "key": "last_pour_string", "icon": "mdi:clock-outline", "device_class": None, "state_class": None, "unit": None, "round": None},

    "max_keg_volume": {"name": "Max Keg Volume", "key": "max_keg_volume", "icon": "mdi:keg", "device_class": "volume", "state_class": None, "unit": "L", "round": 3},

    "measure_unit": {"name": "Measure Unit", "key": "measure_unit", "icon": "mdi:tune", "device_class": None, "state_class": None, "unit": None, "round": None},
    "unit": {"name": "Unit System", "key": "unit", "icon": "mdi:ruler", "device_class": None, "state_class": None, "unit": None, "round": None},

    "volume_unit": {"name": "Volume Unit", "key": "volume_unit", "icon": "mdi:format-letter-case", "device_class": None, "state_class": None, "unit": None, "round": None},
    "beer_left_unit": {"name": "Beer Left Unit", "key": "beer_left_unit", "icon": "mdi:format-letter-case", "device_class": None, "state_class": None, "unit": None, "round": None},
    "temperature_unit": {"name": "Temperature Unit", "key": "temperature_unit", "icon": "mdi:format-letter-case", "device_class": None, "state_class": None, "unit": None, "round": None},

    "firmware_version": {"name": "Firmware Version", "key": "firmware_version", "icon": "mdi:identifier", "device_class": None, "state_class": None, "unit": None, "round": None},
    "keg_temperature_string": {"name": "Temperature (String)", "key": "keg_temperature_string", "icon": "mdi:thermometer", "device_class": None, "state_class": None, "unit": None, "round": None},
    "chip_temperature_string": {"name": "Chip Temp (String)", "key": "chip_temperature_string", "icon": "mdi:chip", "device_class": None, "state_class": None, "unit": None, "round": None},

    "internal_ver": {"name": "Internal Version", "key": "internal_ver", "icon": "mdi:information", "device_class": None, "state_class": None, "unit": None, "round": None},
    "internal_fw": {"name": "Internal FW", "key": "internal_fw", "icon": "mdi:information", "device_class": None, "state_class": None, "unit": None, "round": None},
    "internal_dev": {"name": "Internal Device", "key": "internal_dev", "icon": "mdi:chip", "device_class": None, "state_class": None, "unit": None, "round": None},
    "internal_build": {"name": "Internal Build", "key": "internal_build", "icon": "mdi:calendar", "device_class": None, "state_class": None, "unit": None, "round": None},
    "internal_tmpl": {"name": "Internal Template", "key": "internal_tmpl", "icon": "mdi:tag", "device_class": None, "state_class": None, "unit": None, "round": None},
    "internal_hbeat": {"name": "Internal Heartbeat", "key": "internal_hbeat", "icon": "mdi:heart-pulse", "device_class": None, "state_class": "measurement", "unit": "s", "round": None},
}

AIRLOCK_SENSOR_TYPES: Dict[str, Dict[str, Any]] = {
    "temperature": {"name": "Temperature", "key": "temperature", "icon": "mdi:thermometer", "device_class": "temperature", "state_class": "measurement", "unit": "°C", "round": 1},
    "bubbles_per_min": {"name": "Bubbles Per Minute", "key": "bubbles_per_min", "icon": "mdi:chart-bubble", "device_class": None, "state_class": "measurement", "unit": "BPM", "round": 1},
    "total_bubble_count": {"name": "Total Bubble Count", "key": "total_bubble_count", "icon": "mdi:counter", "device_class": None, "state_class": "total_increasing", "unit": None, "round": None},
    "error": {"name": "Error", "key": "error", "icon": "mdi:alert-circle", "device_class": None, "state_class": None, "unit": None, "round": None},
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    state = hass.data[DOMAIN][entry.entry_id]
    created: Set[str] = state.setdefault("created_kegs", set())

    async_add_entities([BeerKegDebugSensor(hass, entry)], True)

    def create_for(keg_id: str) -> None:
        if keg_id in created:
            return
        ents: List[SensorEntity] = [KegSensor(hass, entry, keg_id, sensor_key) for sensor_key in SENSOR_TYPES.keys()]
        async_add_entities(ents, True)
        created.add(keg_id)

    for keg_id in list(state.get("data", {}).keys()):
        create_for(keg_id)

    @callback
    def _on_update(event) -> None:
        keg_id = (event.data or {}).get("keg_id")
        if keg_id:
            create_for(keg_id)

    entry.async_on_unload(hass.bus.async_listen(PLATFORM_EVENT, _on_update))

    # Airlock sensors
    created_airlocks: Set[str] = state.setdefault("created_airlocks_sensor", set())

    def create_airlock_sensors_for(airlock_id: str) -> None:
        if airlock_id in created_airlocks:
            return
        ents: List[SensorEntity] = [
            AirlockSensor(hass, entry, airlock_id, sensor_key)
            for sensor_key in AIRLOCK_SENSOR_TYPES.keys()
        ]
        async_add_entities(ents, True)
        created_airlocks.add(airlock_id)

    for airlock_id in list(state.get("airlock_data", {}).keys()):
        create_airlock_sensors_for(airlock_id)

    @callback
    def _on_airlock_update(event) -> None:
        airlock_id = (event.data or {}).get("airlock_id")
        if airlock_id:
            create_airlock_sensors_for(airlock_id)

    entry.async_on_unload(hass.bus.async_listen(AIRLOCK_EVENT, _on_airlock_update))


class KegSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, keg_id: str, sensor_type: str) -> None:
        self.hass = hass
        self.entry = entry
        self.keg_id = keg_id
        self.sensor_type = sensor_type
        self._state_ref: Dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
        self._meta = SENSOR_TYPES[sensor_type]

        short_id = keg_id[:4]
        self._attr_name = f"Keg {short_id} {self._meta['name']}"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{keg_id}_{sensor_type}"
        self._attr_icon = self._meta.get("icon")
        self._attr_device_class = self._meta.get("device_class")
        self._attr_state_class = self._meta.get("state_class")
        self._static_unit = self._meta.get("unit")
        # State for last_pour_time tracking
        self._last_pour_string: str | None = None
        self._last_pour_initialized: bool = False
        self._last_pour_timestamp: str | None = None

    @property
    def device_info(self) -> DeviceInfo:
        short_id = self.keg_id[:4]
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_{self.keg_id}")},
            name=f"Beer Keg {short_id}",
            manufacturer="open-plaato-keg",
            model="API v2",
        )

    @property
    def native_unit_of_measurement(self) -> str | None:
        d = self._state_ref.get("data", {}).get(self.keg_id, {})

        if self.sensor_type == "amount_left":
            u = str(d.get("beer_left_unit") or "").lower()
            if u in ("kg", "kilogram", "kilograms"):
                return "kg"
            if u in ("litre", "liter", "liters", "litres", "l"):
                return "L"
            if u in ("gal", "gallon", "gallons"):
                return "gal"
            if u in ("oz", "ounce", "ounces"):
                return "oz"
            return None

        # Dynamic: weight -> lb when server in US
        if self.sensor_type in ("total_weight_kg", "beer_remaining_kg", "empty_keg_weight"):
            return "lb" if _should_use_us(d) else "kg"

        # Dynamic: liters_remaining -> gal when server in US
        if self.sensor_type == "liters_remaining":
            return "gal" if _should_use_us(d) else "L"

        # Dynamic: last pour -> ml in metric, oz in US
        if self.sensor_type == "last_pour_oz":
            return "ml" if _should_use_ml(d) else "oz"

        # Dynamic: daily consumption -> L in metric, oz in US
        if self.sensor_type == "daily_consumption_oz":
            return "L" if _should_use_ml(d) else "oz"

        return self._static_unit

    @property
    def native_value(self) -> Any:
        data: Dict[str, Dict[str, Any]] = self._state_ref.get("data", {})
        d = data.get(self.keg_id, {})

        # last_pour_time: return stored timestamp, not raw data
        if self.sensor_type == "last_pour_time":
            return self._last_pour_timestamp

        raw = d.get(self._meta["key"])
        if raw is None:
            return None

        # Pretty-format date sensors: "10 December 2025 (43 days ago)"
        if self.sensor_type in ("my_keg_date", "kegged_date", "expiration_date"):
            try:
                keg_date = datetime.strptime(raw, "%m/%d/%Y").date()
                today = date.today()
                days = (today - keg_date).days
                pretty = f"{keg_date.day} {keg_date.strftime('%B %Y')}"
                if days >= 0:
                    return f"{pretty} ({days} days ago)"
                else:
                    return f"{pretty} (in {-days} days)"
            except Exception:
                return raw

        # Last pour: stored as oz; convert to ml for metric, oz for US
        if self.sensor_type == "last_pour_oz":
            try:
                oz = float(raw)
            except Exception:
                return None
            if _should_use_ml(d):
                return int(round(oz * OZ_TO_ML, 0))
            return round(oz, 1)

        # Daily consumption: stored as oz; convert to L for metric, oz for US
        if self.sensor_type == "daily_consumption_oz":
            try:
                oz = float(raw)
            except Exception:
                return None
            if _should_use_ml(d):
                return round(oz * OZ_TO_L, 2)
            return round(oz, 1)

        # Weight values are stored as kg; convert to lb if needed
        if self.sensor_type in ("total_weight_kg", "beer_remaining_kg", "empty_keg_weight"):
            try:
                kg = float(raw)
            except Exception:
                return None
            if _should_use_us(d):
                return round(kg * KG_TO_LB, 2)
            r = self._meta.get("round")
            return round(kg, r) if isinstance(r, int) else kg

        # Volume derived is stored as liters; convert to gal if needed
        if self.sensor_type == "liters_remaining":
            try:
                liters = float(raw)
            except Exception:
                return None
            if _should_use_us(d):
                return round(liters * L_TO_GAL, 3)
            r = self._meta.get("round")
            return round(liters, r) if isinstance(r, int) else liters

        r = self._meta.get("round")
        if isinstance(raw, (int, float)) and isinstance(r, int):
            return round(float(raw), r)
        return raw

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.hass.bus.async_listen(PLATFORM_EVENT, self._refresh_if_mine))

    @callback
    def _refresh_if_mine(self, event) -> None:
        if (event.data or {}).get("keg_id") != self.keg_id:
            return
        if self.sensor_type == "last_pour_time":
            d = self._state_ref.get("data", {}).get(self.keg_id, {})
            current = d.get("last_pour_string")
            if not self._last_pour_initialized:
                # First update after (re)start: capture current value without setting timestamp
                self._last_pour_string = current
                self._last_pour_initialized = True
            elif current and current != self._last_pour_string:
                self._last_pour_string = current
                now = datetime.now()
                self._last_pour_timestamp = f"{now.strftime('%a %b')} {now.day} at {now.strftime('%H:%M')}"
        self.async_write_ha_state()


class BeerKegDebugSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_debug"
        self._attr_name = "Beer Keg Debug"
        self._attr_icon = "mdi:bug"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_settings")},
            name="Beer Keg Settings",
            manufacturer="open-plaato-keg",
            model="API v2",
        )

    @property
    def native_value(self) -> float | None:
        st = self.hass.data[DOMAIN][self.entry.entry_id]
        ts = st.get(ATTR_LAST_UPDATE)
        if not isinstance(ts, datetime):
            return None
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return round(age, 1)

    @property
    def extra_state_attributes(self) -> dict:
        st = self.hass.data[DOMAIN][self.entry.entry_id]
        return {
            "ws_url": st.get("ws_url"),
            "devices": list(st.get("devices", [])),
            "kegs": list(st.get("data", {}).keys()),
        }


class AirlockSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, airlock_id: str, sensor_type: str) -> None:
        self.hass = hass
        self.entry = entry
        self.airlock_id = airlock_id
        self.sensor_type = sensor_type
        self._state_ref: Dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
        self._meta = AIRLOCK_SENSOR_TYPES[sensor_type]

        label = str(airlock_id)
        self._attr_name = f"Airlock {label} {self._meta['name']}"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_airlock_{airlock_id}_{sensor_type}"
        self._attr_icon = self._meta.get("icon")
        self._attr_device_class = self._meta.get("device_class")
        self._attr_state_class = self._meta.get("state_class")
        self._attr_native_unit_of_measurement = self._meta.get("unit")

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_airlock_{self.airlock_id}")},
            name=f"Airlock {self.airlock_id}",
            manufacturer="open-plaato-keg",
            model="Plaato Airlock",
        )

    @property
    def native_value(self) -> Any:
        d = self._state_ref.get("airlock_data", {}).get(self.airlock_id, {})
        raw = d.get(self._meta["key"])
        if raw is None:
            return None
        rnd = self._meta.get("round")
        if rnd is not None and isinstance(raw, (int, float)):
            return round(raw, rnd)
        return raw

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.hass.bus.async_listen(AIRLOCK_EVENT, self._refresh_if_mine))

    @callback
    def _refresh_if_mine(self, event) -> None:
        if (event.data or {}).get("airlock_id") != self.airlock_id:
            return
        self.async_write_ha_state()
