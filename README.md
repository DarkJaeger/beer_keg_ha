<a href="https://www.buymeacoffee.com/LocutusOFB"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="41" width="174"></a>

# Beer Keg Scale (Home Assistant)

Live keg and fermentation monitoring for Home Assistant using WebSocket + REST fallback.

Tracks weight, volume, temperature, pours, daily consumption, beer metadata, and expiration with real-time updates from the [Open Plaato Keg](https://github.com/DarkJaeger/open-plaato-keg) server (API v2). Also supports **Plaato Airlock** fermentation devices with Grainfather and Brewfather integration.

---

## ✨ Key Features

- ⚡ **Real-time WebSocket updates** (REST polling fallback every 10s)
- ⚖️ **Live keg weight & remaining volume**
- 📊 **Daily consumption & last pour tracking**
- 🟢 **Live "Pouring Now" indicator** (based on live weight change)
- 🌡️ **Keg & chip temperature**
- 🍺 **Beer metadata** — style, keg date, OG / FG / ABV
- 📅 **Automatic expiration date** — keg date + 6 months
- 🧮 **Density-aware calculations** (SG × volume)
- 🫧 **Plaato Airlock support** — temperature, BPM, bubble count, Grainfather & Brewfather forwarding
- 🔧 **Keg command services** — tare, calibrate, set unit system, sensitivity and more
- 🧹 **Auto entity cleanup** — stale entities from old versions removed on startup
- 🧠 **Robust against restarts & temporary disconnects**

---

## 📦 Installation (HACS – Custom Repository)

1. Open **HACS → Integrations**
2. Click **⋯ → Custom repositories**
3. Add: `https://github.com/DarkJaeger/beer_keg_ha` — Type: **Integration**
4. Search for **Beer Keg Scale** → Install → Restart Home Assistant
5. Go to **Settings → Devices & Services → Add Integration**
6. Select **Beer Keg Scale** and enter your WebSocket URL

---

## ⚙️ Configuration

| Field | Example | Notes |
|---|---|---|
| WebSocket URL | `ws://192.168.1.x:8085/ws` | Required |

Most configuration is handled by the server and reflected automatically in HA.

---

## 📊 Entities

### Per Keg Scale

| Entity | Description |
|---|---|
| `sensor.keg_<id>_amount_left` | Beer remaining (server unit) |
| `sensor.keg_<id>_total_weight_kg` | Total scale weight |
| `sensor.keg_<id>_beer_remaining_kg` | Beer weight only |
| `sensor.keg_<id>_liters_remaining` | Liters / gallons remaining |
| `sensor.keg_<id>_percent_of_beer_left` | Fill percentage |
| `sensor.keg_<id>_last_pour_oz` | Last pour size |
| `sensor.keg_<id>_last_pour_time` | Timestamp of last pour |
| `sensor.keg_<id>_daily_consumption_oz` | Today's consumption |
| `binary_sensor.keg_<id>_pouring` | Live pouring indicator |
| `sensor.keg_<id>_keg_temperature` | Keg temperature |
| `sensor.keg_<id>_my_beer_style` | Beer style (from server) |
| `sensor.keg_<id>_my_keg_date` | Keg date (from server) |
| `sensor.keg_<id>_my_og` / `_my_fg` / `_my_abv` | Gravity & ABV |
| `sensor.keg_<id>_kegged_date` | Manual kegged date |
| `sensor.keg_<id>_expiration_date` | Auto-calculated expiry (+6 months) |
| `sensor.keg_<id>_days_until_expiration` | Days remaining |

Diagnostic sensors (temperatures, Wi-Fi, firmware, heartbeat, leak) are also created.

### Per Plaato Airlock

| Entity | Description |
|---|---|
| `sensor.airlock_<id>_temperature` | Fermentation temperature |
| `sensor.airlock_<id>_bubbles_per_min` | BPM (bubbles per minute) |
| `sensor.airlock_<id>_total_bubble_count` | Lifetime bubble count |
| `sensor.airlock_<id>_error` | Error code from device |
| `switch.airlock_<id>_grainfather_enabled` | Enable Grainfather forwarding |
| `switch.airlock_<id>_brewfather_enabled` | Enable Brewfather forwarding |
| `select.airlock_<id>_grainfather_temp_unit` | Grainfather temp unit |
| `select.airlock_<id>_brewfather_temp_unit` | Brewfather temp unit |
| `number.airlock_<id>_grainfather_sg` | Grainfather specific gravity |
| `number.airlock_<id>_brewfather_sg` | Brewfather specific gravity |
| `text.airlock_<id>_grainfather_url` | Grainfather stream URL |
| `text.airlock_<id>_brewfather_url` | Brewfather custom stream URL |
| `text.airlock_<id>_brewfather_og` | Brewfather OG |
| `text.airlock_<id>_brewfather_batch_volume` | Brewfather batch volume (L) |

---

## 🔧 Services

### Keg Date

```yaml
service: beer_keg_ha.set_keg_dates
data:
  id: "<keg_id>"
  kegged_date: "MM/DD/YYYY"
```

### Keg Commands

All accept an optional `id` field — if omitted, uses the currently selected keg device.

| Service | Description |
|---|---|
| `beer_keg_ha.keg_tare` | Tare the scale |
| `beer_keg_ha.keg_set_empty_keg_weight` | Set empty keg weight (kg) |
| `beer_keg_ha.keg_set_max_keg_volume` | Set max keg volume (L) |
| `beer_keg_ha.keg_set_temperature_offset` | Set temperature calibration offset (°C) |
| `beer_keg_ha.keg_calibrate_known_weight` | Calibrate with a known weight |
| `beer_keg_ha.keg_set_beer_style` | Set beer style label |
| `beer_keg_ha.keg_set_date` | Set keg date |
| `beer_keg_ha.keg_set_unit_system` | Switch between `metric` / `us` |
| `beer_keg_ha.keg_set_measure_unit` | Switch between `weight` / `volume` display |
| `beer_keg_ha.keg_set_mode` | Switch between `beer` / `co2` mode |
| `beer_keg_ha.keg_set_sensitivity` | Set pour sensitivity (0–10) |

### Airlock Services

| Service | Description |
|---|---|
| `beer_keg_ha.set_airlock_label` | Set a human-readable label for an airlock |
| `beer_keg_ha.configure_grainfather` | Enable/configure Grainfather forwarding |
| `beer_keg_ha.configure_brewfather` | Enable/configure Brewfather forwarding |

### Maintenance

| Service | Description |
|---|---|
| `beer_keg_ha.cleanup_entities` | Remove stale entities from previous versions |
| `beer_keg_ha.refresh_kegs` | Force a REST poll for keg data |
| `beer_keg_ha.republish_all` | Re-fire update events for all known kegs |
| `beer_keg_ha.refresh_devices` | Refresh the keg device selector list |
| `beer_keg_ha.export_history` | Export pour history to `/config/www/beer_keg_history.json` |
| `beer_keg_ha.set_display_units` | Update dashboard display units |

---

## 🧠 How Pour Detection Works

Instead of relying on unreliable `is_pouring` flags, pour detection monitors live weight changes:

- If total weight drops by ~0.02 kg (≈ 0.7 oz) → pouring indicator turns ON
- Indicator stays ON for a few seconds after the last detected drop
- This matches actual beer flow, not just scale state

---

## 🧹 Stale Entity Cleanup

When upgrading, old entities from previous versions are automatically removed 15 seconds after startup. You can also trigger this manually:

```yaml
service: beer_keg_ha.cleanup_entities
```

A persistent notification will confirm how many entities were removed.

---

## 📺 Lovelace Examples

Example cards (entities, gauges, history graphs, pouring indicator dots) are available in `/cards`.

The pouring indicator works best with a **Glance card** and `state_color: true`.

---

## 🛠️ Troubleshooting

**HACS not updating?**
- HACS → ⋯ → Clear downloads → Reload data → Restart HA

**Entities missing after upgrade?**
- Run `beer_keg_ha.cleanup_entities` to remove stale entities, then reload the integration

**WebSocket disconnects?**
- Check the server is running: `http://<host>:8085/api/alive`
- REST polling (every 10s) will keep data fresh while WS reconnects

**Airlock shows as keg briefly?**
- Requires Open Plaato Keg server v0.1.42 or later which fixes phantom keg entries

---

## 📘 Full System Install Guide

[Full system install instructions](https://github.com/DarkJaeger/beer_keg_ha/blob/main/install%20guides/Full%20system%20install.instructions.md)

---

## ℹ️ Notes

- WebSocket is primary; REST polling ensures resilience
- Works with Open Plaato Keg API v2
- Designed for always-on wall displays & bar dashboards
- Airlock data is polled every 30s and updated live via WebSocket

---

## 📄 License

MIT License
