Beer Keg Scale – Home Assistant Integration
Overview

Beer Keg Scale (beer_keg_ha) is a custom Home Assistant integration that connects to a Beer Keg server via:

WebSocket (primary real-time updates)

REST API (fallback, polling, device discovery, calibration)

It tracks keg weight, temperature, pour events, fill percentage, and maintains historical pour data.

Domain: beer_keg_ha 

const


Version: 1.2.0 

manifest


IoT Class: local_push 

manifest


Repository: https://github.com/DarkJaeger/beer-keg-ha
 

manifest

Architecture
Core Components
1. Config Flow

User provides:

ws_url (WebSocket endpoint)

Unique ID = ws_url

Options allow:

Empty weight

Default full weight

Pour threshold

Per-keg full overrides

Density-aware volume + specific gravity settings 

config_flow

2. Runtime State (hass.data[DOMAIN][entry_id])

Stores:

ws_url

empty_weight

default_full

pour_threshold

per_keg_full

full_volume_l

beer_sg

computed_full_from_sg

kegs (runtime stats)

data (entity-facing values)

history

devices

display_units

persistent history_store

persistent prefs_store

Data Flow
WebSocket (Primary)

Connects to configured ws_url

Pings every 30s

Parses:

[keg list]

{ "kegs": [...] }

Normalizes payload

Publishes per-keg updates

REST (Fallback & Watchdog)

Every 10s:

GET /api/kegs

Every 60s:

GET /api/kegs/devices

If no updates for 45s:

Watchdog forces REST refresh

Keg Logic
Full Weight Priority

Device-reported full_weight

Per-keg override

Computed from:

full_volume_l * beer_sg * WATER_DENSITY_KG_PER_L


Default full weight 

const

Pour Detection

If:

previous_weight - current_weight > pour_threshold


Then:

Convert kg → oz

Store:

last pour (oz)

timestamp

add to daily consumption

Append to history

Trim to MAX_LOG_ENTRIES = 500 

const

Fill Percentage
fill% = ((weight - empty_weight) / (full_weight - empty_weight)) * 100


Clamped between 0–100%.

Entities
Sensors (sensor.py – implied)

Per keg:

Weight

Temperature

Full weight

Fill %

Daily consumed (oz)

Last pour (oz)

Number Entities (Per Keg)

From number.py 

number

Created dynamically when new kegs appear:

Full Weight (kg)

Weight Calibrate

Temp Calibrate (°C)

These:

Update integration state

Fire update events

Do NOT directly call REST

Select Entities

From select.py 

select

1. Keg Weight Unit

kg

lb

2. Keg Temperature Unit

°C

°F

3. Keg Device Selector

Lists discovered keg IDs

Fires device_selected event

Services

Registered under beer_keg_ha

Data Services

export_history

refresh_kegs

republish_all

refresh_devices

Calibration Service

calibrate_keg

Reads:

select.keg_device

input_text.beer_keg_name

input_number.keg_cfg_full_weight_kg

input_number.keg_cfg_weight_cal

input_number.keg_cfg_temp_cal_c

POSTs:

/api/kegs/calibrate

Display Units Service

set_display_units

Priority:

Service call data

Select helpers

Existing prefs

Persists to storage and refreshes all entities.

Persistent Storage
History Store

File: beer_keg_ha_history

Stores last 500 pour entries 

const

Prefs Store

File: beer_keg_ha_prefs

Stores display unit selections

Both saved on HA shutdown.

Events

Integration fires:

beer_keg_ha_update

beer_keg_ha_devices_update

beer_keg_ha_units_changed

beer_keg_ha_device_selected

Used internally to refresh entities and allow automations.

Constants (const.py)

Key defaults 

const

DEFAULT_EMPTY_WEIGHT = 0.0
DEFAULT_FULL_WEIGHT = 19.0
DEFAULT_POUR_THRESHOLD = 0.15
DEFAULT_FULL_VOLUME_L = 19.0
DEFAULT_BEER_SG = 1.010
WATER_DENSITY_KG_PER_L = 0.998
MAX_LOG_ENTRIES = 500

Platforms Loaded
PLATFORMS = ["sensor", "select", "number"]

Startup Sequence

Config entry loads

Restore history + prefs

Forward platforms

Initial REST fetch

Start:

WebSocket loop

REST polling

Watchdog

Device refresh timer

Services registered

System fully operational

Design Goals

Real-time keg tracking

Accurate pour detection

Density-aware fill %

Per-keg overrides

Persistent unit preferences

Automatic device discovery

REST fallback resilience

HA-native entities & services