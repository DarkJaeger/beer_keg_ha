DOMAIN = "beer_keg_ha"

# Config keys
CONF_WS_URL = "ws_url"

# (Optional legacy keys — safe to keep even if v2-only; remove later if you want)
CONF_EMPTY_WEIGHT = "empty_weight"
CONF_DEFAULT_FULL_WEIGHT = "default_full_weight"
CONF_POUR_THRESHOLD = "pour_threshold"
CONF_PER_KEG_FULL = "per_keg_full"

# Density-aware fill% / conversions
CONF_FULL_VOLUME_L = "full_volume_liters"
CONF_BEER_SG = "beer_specific_gravity"

# Defaults
DEFAULT_EMPTY_WEIGHT = 0.0
DEFAULT_FULL_WEIGHT = 19.0
DEFAULT_POUR_THRESHOLD = 0.15

DEFAULT_FULL_VOLUME_L = 19.0
DEFAULT_BEER_SG = 1.010
WATER_DENSITY_KG_PER_L = 0.998

# History (not used in the v2-only minimal build, but harmless)
MAX_LOG_ENTRIES = 500

# Integration version (optional; HA uses manifest.json version)
VERSION = "2.0.0"

# --- Runtime/events (needed by v2-only __init__.py + sensor/select) ---
PLATFORM_EVENT = f"{DOMAIN}_update"
DEVICES_UPDATE_EVENT = f"{DOMAIN}_devices_update"

# --- Runtime state keys ---
ATTR_LAST_UPDATE = "last_update_ts"
