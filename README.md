\# Beer Keg Scale (Home Assistant)



Live keg weight, temperature, fill %, pours, and history — with WebSocket + REST fallback. Supports density-aware fill% using Full Volume (L) and Beer SG.



\## Install (HACS - Custom repo)

1\. HACS → Integrations → 3-dot menu → \*\*Custom repositories\*\*

2\. Add: `https://github.com/DarkJaeger/beer-keg-ha` as type \*\*Integration\*\*

3\. Search “Beer Keg Scale” → Install → \*\*Restart Home Assistant\*\*

4\. Settings → Devices \& Services → \*\*Add Integration\*\* → Beer Keg Scale



\## Configure

\- \*\*WebSocket URL\*\*: `ws://<host>:8085/ws`

\- Options:

&nbsp; - Empty keg weight (kg): `0` if you tare with empty keg

&nbsp; - Full volume (L): e.g. `19.0`

&nbsp; - Beer SG: e.g. `1.010` (defaults to typical finished beer)

&nbsp; - Default full weight (kg): fallback if needed

&nbsp; - Per-keg full weights JSON: `{ "keg\_id": 19.0 }`



\## Entities

\- `sensor.keg\_<id>\_weight` (kg)

\- `sensor.keg\_<id>\_temperature` (°C)

\- `sensor.keg\_<id>\_fill\_percent` (and legacy alias `\_fill\_level`)

\- `sensor.keg\_<id>\_last\_pour` (oz)

\- `sensor.keg\_<id>\_daily\_consumed` (oz)

\- `sensor.keg\_<id>\_full\_weight` (kg)

\- `sensor.keg\_<id>\_name`, `sensor.keg\_<id>\_id`



\## Services

\- `beer\_keg\_ha.export\_history`

\- `beer\_keg\_ha.refresh\_kegs`



\## Notes

\- WS for realtime, REST poll + watchdog for resilience.

\- Fill% chooses full\_weight: device → per-keg override → `volume × SG × 0.998` → default.



