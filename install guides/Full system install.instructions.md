<div align="center"> 
🍺 Beer Keg Monitoring System
Real-time keg weight, temperature, pours, and fill-percentage tracking using ESP32 scales, open-plaato-keg server, and Home Assistant.
<br> 
ESP32 Scales → open-plaato-keg Server → Beer-Keg-HA → Dashboards & Automations
<img src="https://img.shields.io/badge/Platform-Home%20Assistant-blue" /> <img src="https://img.shields.io/badge/Hardware-ESP32-green" /> <img src="https://img.shields.io/badge/Backend-Docker-orange" /> <img src="https://img.shields.io/badge/Pours-Tracked-success" /> </div> 
________________________________________
📘 Overview
This system allows you to monitor multiple beer kegs using inexpensive ESP32 load-cell scales with Plaato-style firmware. Keg data is streamed to an open-plaato-keg server running in Docker, then consumed by the Beer-Keg-HA integration in Home Assistant.
________________________________________
📡 System Architecture
        ┌──────────────────────┐
        │   ESP32 Keg Scale    │
        │ (Plaato-compatible)  │
        └──────────┬───────────┘
                   │ WiFi 2.4GHz
                   ▼
        ┌───────────────────────────┐
        │ open-plaato-keg SERVER   │
        │      (Docker)            │
        │  • WebSocket API         │
        │  • REST API /api/kegs    │
        └──────────┬──────────────┘
                   │  WS + REST
                   ▼
        ┌──────────────────────────┐
        │ Home Assistant           │
        │  Beer-Keg-HA Integration │
        │  • Sensors               │
        │  • Calibration Tools     │
        │  • Unit Controls         │
        │  • Keg Selector          │
        └──────────┬──────────────┘
                   ▼
        ┌──────────────────────────┐
        │ Dashboards & Automations │
        └──────────────────────────┘
________________________________________
🚀 Features
✔ Multi-keg support
✔ ESP32 Plaato-compatible firmware
✔ Live WebSocket streaming
✔ REST fallback with polling
✔ Automatic pour detection
✔ Fill-percentage
✔ Daily consumption tracking
✔ Per-keg full-weight configuration
✔ Calibration offsets (weight + temp)
✔ Unit control (kg/lb & °C/°F)
✔ Persistent history + export
✔ Device selection dropdown
________________________________________
📦 Components Needed
•	ESP32 boards flashed with Plaato-compatible firmware
•	Load cells + HX711 modules
•	Docker host for open-plaato-keg server
•	Home Assistant OS or Home Assistant Container
•	This integration: Beer-Keg-HA
________________________________________
🛠️ 1. Install Home Assistant
You may use either:
________________________________________
Option A — Home Assistant OS (Recommended for Beginners)
1.	Visit https://www.home-assistant.io/installation/
2.	Flash HA OS to a Raspberry Pi / NUC / ODROID
3.	Boot and open:
http://homeassistant.local:8123
________________________________________
Option B — Home Assistant Container (Docker)
docker run -d \
  --name homeassistant \
  --restart unless-stopped \
  -e TZ=America/New_York \
  -v /srv/homeassistant:/config \
  --network=host \
  ghcr.io/home-assistant/home-assistant:stable
________________________________________
📝 2. Install open-plaato-keg Server (Docker)
mkdir -p /srv/open-plaato-keg
docker run -d \
  --name open-plaato-keg \
  --restart unless-stopped \
  -p 6080:6080 \
  -e KEG_LISTENER_PORT=6080 \
  -v /srv/open-plaato-keg:/data \
  sklopivo/open-plaato-keg:latest
Verify:
http://<docker-host>:6080/api/kegs
Should return:
[]
________________________________________
📶 3. Configure Each ESP32 Keg Scale
When powered, each scale broadcasts WiFi:
PLAATO-XXXXX
Step 1 — Connect to PLAATO-XXXXX
Step 2 — Visit configuration portal
http://192.168.4.1
Step 3 — Fill in configuration
Field	Value
WiFi SSID	Your 2.4GHz WiFi
Password	Your WiFi password
Auth Token	Unique 32-char hex (keg ID)
Host	IP of open-plaato-keg server
Port	6080 (default)
Example:
SSID: Home24
Password: mywifi123
Auth Token: 00af1234b00cdeadbeef1234aa55cc99
Host: 192.168.1.50
Port: 6080
Optional: Configure via HTTP GET
http://192.168.4.1/config?ssid=My+Wifi&pass=my_password&blynk=00000000000000000000000000000001&host=192.168.0.123&port=6080

  4. Install Hacs community store in Home Assistant (if not already done)
To install HACS, follow the official instructions:
https://hacs.xyz/docs/installation/manual
To add the HACS add-on repository to your Home Assistant, select this my link.
https://my.home-assistant.io/redirect/supervisor_addon/?addon=cb646a50_get&repository_url=https%3A%2F%2Fgithub.com%2Fhacs%2Faddons
When prompted to confirm if you want to open the page in Home Assistant, check if the URL is correct. Then, select Open link.
In the Missing add-on repository dialog, select Add.
You have now added the repository that allows you to download HACS to Home Assistant.
In the Get HACS add-on, select Install.
Start the add-on.
Navigate to the add-on logs and follow the instructions given there.
Finalizing steps
Restart Home Assistant.
Follow the steps on setting up the HACS integration.
________________________________________
🔌 5. Install Beer-Keg-HA Integration
1. Download the repository:
https://github.com/DarkJaeger/beer_keg_ha
2. Copy to Home Assistant:
/config/custom_components/beer_keg_ha/
3. Restart Home Assistant
________________________________________
🔧 6. Add Integration in Home Assistant
Go to:
Settings → Devices & Services → Add Integration → Beer Keg Scale
You will be asked for:
WebSocket URL
Format:
ws://<docker-host>:6080/ws
Example:
ws://192.168.1.50:6080/ws
After adding:
✔ Sensors appear
✔ Keg devices populate automatically
✔ Calibration values initialized
________________________________________
⚙️ 7. Unit Controls (lb/kg & °F/°C)
Two select entities are created:
•	select.keg_weight_unit
•	select.keg_temperature_unit
Changing these immediately updates all keg sensors.
________________________________________
🧰 8. Calibration Tools
Each keg has three calibration values:
•	Full Weight (kg)
•	Weight Offset
•	Temperature Offset
These are Number entities under the keg device.
Calibration is sent back to the API via the service:
beer_keg_ha.calibrate_keg
________________________________________
📊 9. Pour Tracking & Keg History
The integration automatically:
•	Detects pours (weight drop > threshold)
•	Converts kg → oz
•	Records timestamp, before/after, temp
•	Stores up to 500 records
You can export history:
beer_keg_ha.export_history
Saved as:
/www/beer_keg_history.json
________________________________________
📺 10. Sample Lovelace Card
type: entities
title: Keg Status
entities:
  - sensor.keg_<id>_weight
  - sensor.keg_<id>_temperature
  - sensor.keg_<id>_fill_percent
  - sensor.keg_<id>_daily_consumed
  - select.keg_weight_unit
  - select.keg_temperature_unit
________________________________________
🛠️ 11. Useful Services
Service	Description
beer_keg_ha.refresh_kegs	Force update from REST API
beer_keg_ha.republish_all	Re-broadcast all sensor data
beer_keg_ha.export_history	Save pour history to JSON
beer_keg_ha.calibrate_keg	Submit calibration values
beer_keg_ha.refresh_devices	Rescan keg IDs
beer_keg_ha.set_display_units	Persist units (lb/kg & °F/°C)
________________________________________
❓ Troubleshooting
No kegs appear?
Check:
http://<host>:6080/api/kegs
If empty:
•	Scale not connected to WiFi
•	Wrong host/port
•	Bad 32-char Auth Token
WebSocket not connecting?
Confirm:
ws://<host>:6080/ws
Units won’t update?
Run:
beer_keg_ha.set_display_units
________________________________________
❤️ Support This Project
If you enjoy this integration, please ⭐ the repository.
________________________________________
📄 License
MIT License
