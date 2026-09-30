# TAHMO API v2 notes

Behaviour the skill relies on. Checked against the live API (2026-09) and the
TAHMO Measurements API documentation rev 1.01
(https://tahmo.org/docs/TAHMO_Measurements_API_documentation_latest.pdf).

## Endpoints

Base URL `https://datahub.tahmo.org`, HTTP Basic auth (`id:secret`).

| Purpose | Endpoint |
| --- | --- |
| Stations visible to the account | `GET services/assets/v2/stations?sort=code` → `{"data": [...]}` |
| Variable catalogue | `GET services/assets/v2/variables` → `{"data": [{"variable": {...}}]}` |
| Measurements | `GET services/measurements/v2/stations/{code}/measurements/{raw\|controlled}` |

Measurement query params: `start`, `end` (`YYYY-MM-DDTHH:MM:SSZ`, both
inclusive), `variable` (one shortcode), and `sensor`.

- Without `start`/`end` the API returns the last 24 hours (`--probe-latest` uses this).
- The maximum window is one year; a longer one returns HTTP 422 `period too large`.
- `variable` takes only **one** shortcode. Comma lists return nothing, and a
  repeated param uses only the first value. Requesting all variables at once is
  about 10× slower than one request per variable, so the skill requests each
  variable separately.
- Responses are gzip-compressed when requested (`requests` does this by default),
  about 20× smaller.

Response shape (InfluxDB style):

```json
{"results": [{"statement_id": 0, "series": [{
  "name": "controlled",
  "columns": ["time", "mc", "quality", "rc", "rs", "sensor", "station", "td", "ti", "ts", "value", "variable"],
  "values": [["2025-09-01T00:00:00Z", null, 1, 1, 1, "S001832", "TA00567", 1, null, 1, 101.02, "ap"]]}]}]}
```

A window with no data has no `series` key. The `raw` collection has only
`time, quality, sensor, station, value, variable`.

## Quality flags

`quality`: 1 = good, 2 = suspect, 3 = doubtful, 4 = erroneous. A `null` value
means a sensor error. In `raw`, the flag reflects sensor errors only. In
`controlled`, all QC steps affect it, and the per-test results are extra columns:
`rc` range climate, `rs` range sensor, `ts` temporal step, `td` temporal delta,
`ti` / `ts` temporal sigma, `mc` manual change.

## Errors

| Status | Meaning | Skill behaviour |
| --- | --- | --- |
| 401 | Bad credentials (nginx HTML body) | Abort, exit 2 |
| 403 | No access to this station's measurements | Drop the station, continue |
| 400 / 404 / 422 | Bad request / endpoint / period | Drop the station, report the message |
| 429 / 5xx / timeout | Transient | Up to 4 attempts, exponential backoff (honours `Retry-After`) |

## Time

All API timestamps are UTC. Each station has an IANA `location.timezone`
(`timezoneoffset` is unreliable, and is often `0`). TAHMO's documentation says
daily values should be computed in local time, which is why
`--day-boundary local` is the default.

Loggers sample every 5 minutes. A timestamp marks the **end** of the interval
it covers, so daily and hourly bins are right-closed: `(start, start + period]`.

## Units

API units are passed through except:

- `rh` is a 0–1 fraction in the API. The skill multiplies it by 100 and writes `%`.
- `pr` is mm per logger interval. The skill sums it per bin and writes a
  `mm day-1` rate.

## Variable catalogue (API shortcodes)

Meteorological: `pr` precipitation (mm), `te` air temperature (°C), `rh` relative
humidity (fraction), `ap` atmospheric pressure (kPa), `vp` vapour pressure (kPa),
`ws` wind speed (m/s), `wg` wind gust (m/s), `wd` wind direction (degrees),
`ra` shortwave radiation (W/m²), `ld` lightning distance (km), `le` lightning events.

Soil and hydrology: `sm` soil moisture (m³/m³), `st` soil temperature (°C),
`se` soil EC, `mp` matric potential, `wl` water level, `wv` water velocity,
`wq` discharge, `tw` water temperature, `dw` water depth, `ew` water EC.

Diagnostics: `lb` logger battery, `lt` logger temperature, `lp` logger reference
pressure, `ht` humidity-sensor temperature, `tx`/`ty`/`ta` tilt, `cp` cumulative
precipitation, `pt`/`pd` precipitation tip/drop counts, `ec` precipitation EC.

The skill exposes the meteorological and soil variables in SKILL.md. Adding
another is a single `VARIABLES` entry in `scripts/fetch.py`.
