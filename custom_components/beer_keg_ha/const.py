DOMAIN = "beer_keg_ha"

# Config keys
CONF_WS_URL = "ws_url"
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

# History
MAX_LOG_ENTRIES = 500

# Runtime flags / diagnostics
ATTR_PLAATO_API_VERSION = "plaato_api_version"  # "v1" | "v2"
ATTR_PLAATO_API_V2 = "plaato_api_v2"            # bool

