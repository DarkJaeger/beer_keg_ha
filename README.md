<a href="https://www.buymeacoffee.com/LocutusOFB"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="41" width="174"></a>
🍺 Beer Keg Scale (Home Assistant)

Live keg monitoring for Home Assistant using WebSocket + REST fallback.

Tracks weight, volume, temperature, pours, daily consumption, beer metadata, and expiration with real-time updates from the Open Plaato Keg server (API v2).

✨ Key Features

⚡ Real-time WebSocket updates (REST polling fallback)

⚖️ Live keg weight & remaining volume

📊 Daily consumption & last pour (oz)

🟢 Live “Pouring Now” indicator (based on weight change)

🌡️ Keg & chip temperature

🍺 Beer metadata from server

Beer style

Keg date

OG / FG

ABV

📅 Automatic expiration date

Calculated as keg date + 6 months

🧮 Density-aware calculations (SG × volume)

🧠 Robust against restarts & temporary disconnects

📦 Installation (HACS – Custom Repository)

Open HACS → Integrations

Click ⋯ → Custom repositories

Add:

https://github.com/DarkJaeger/beer_keg_ha


Type: Integration

Search for Beer Keg Scale

Install → Restart Home Assistant

Go to Settings → Devices & Services → Add Integration

Select Beer Keg Scale

⚙️ Configuration
Required

WebSocket URL

ws://<host>:8085/ws

Optional / Advanced

Empty keg weight (kg)

Max keg volume (L)

Unit system (metric / us)

Measure unit (weight / volume)

ℹ️ Most configuration is now handled by the server and reflected automatically in HA.

🧠 How Pour Detection Works

Instead of relying on unreliable is_pouring flags, Home Assistant detects pouring by:

Monitoring live weight changes

If total weight drops by ~0.02 kg (≈ 0.7 oz):

Pouring indicator turns ON

Indicator stays ON for a few seconds after the last detected drop

This matches actual beer flow, not just scale state.

📊 Entities
Computed / Derived

sensor.keg_<id>_total_weight_kg

sensor.keg_<id>_beer_remaining_kg

sensor.keg_<id>_liters_remaining

sensor.keg_<id>_percent_of_beer_left

sensor.keg_<id>_last_pour_oz

sensor.keg_<id>_daily_consumption_oz

binary_sensor.keg_<id>_pouring 🟢

Beer Metadata (from server)

sensor.keg_<id>_my_beer_style

sensor.keg_<id>_my_keg_date

sensor.keg_<id>_my_og

sensor.keg_<id>_my_fg

sensor.keg_<id>_my_abv

Dates

sensor.keg_<id>_kegged_date (manual override)

sensor.keg_<id>_expiration_date (auto-calculated)

sensor.keg_<id>_days_until_expiration

Raw / Diagnostic

Temperatures

Wi-Fi strength

Firmware / internal fields

Leak detection

Heartbeat

🔧 Services
Manual Keg Date (Expiration auto +6 months)
service: beer_keg_ha.set_keg_dates
data:
  id: "<keg_id>"
  kegged_date: "MM/DD/YYYY"

Server Command Passthrough

beer_keg_ha.keg_tare

beer_keg_ha.keg_set_empty_keg_weight

beer_keg_ha.keg_set_max_keg_volume

beer_keg_ha.keg_set_temperature_offset

beer_keg_ha.keg_set_beer_style

beer_keg_ha.keg_set_date

beer_keg_ha.keg_set_og

beer_keg_ha.keg_set_fg

beer_keg_ha.keg_calc_abv

beer_keg_ha.keg_set_unit_system

beer_keg_ha.keg_set_measure_unit

beer_keg_ha.keg_set_mode

beer_keg_ha.keg_set_sensitivity

📺 Lovelace Examples

Example cards (entities, gauges, history graphs, pouring indicator dots) are available in:

/cards


The pouring indicator works best with a Glance card and state_color: true.

🛠️ Troubleshooting
Clear HACS Cache

HACS → ⋯ → Clear downloads

HACS → ⋯ → Reload data

Restart Home Assistant

Reinstall integration if needed

Docker Users

File Editor addon is not required

All configuration is handled via UI & integration services

📘 Full System Install Guide

👉
https://github.com/DarkJaeger/beer_keg_ha/blob/main/Full%20system%20install.instructions.md

ℹ️ Notes

WebSocket is primary; REST polling ensures resilience

Works with Open Plaato Keg API v2

Designed for always-on wall displays & bar dashboards

📄 License

MIT License
