---
name: tahmo-fetch
description: Fetch quality-controlled TAHMO weather-station observations (rainfall, temperature incl. daily max/min, humidity, pressure, wind, radiation, soil) from the TAHMO API for chosen stations, countries, or a bbox, and write a point_obs weather-skills Zarr at daily, hourly, or native 5-minute resolution. Also lists the stations an account can access. Use when a task needs in-situ African station data, e.g. to compare against gridded satellite, reanalysis, or forecast data.
license: MIT
compatibility: Requires Python 3.12 and uv. Calls the TAHMO API v2 (https://datahub.tahmo.org) over HTTPS; requires TAHMO_API_USERNAME and TAHMO_API_PASSWORD in the environment.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  catalog-group: fetchers
  variables:
    - precip
    - temperature
    - tmax
    - tmin
    - humidity
    - pressure
    - wind_speed
    - wind_gust
    - wind_direction
    - radiation
    - vapor_pressure
    - soil_moisture
    - soil_temperature
  availability:
    shape: range
    policy: none
    lag_days: 0
    note: Near-real-time (about 30 min latency) from each station's installation date; access is per account
  openclaw:
    requires:
      env:
        - TAHMO_API_USERNAME
        - TAHMO_API_PASSWORD
    primaryEnv: TAHMO_API_USERNAME
---

# tahmo-fetch

Downloads observations from the Trans-African Hydro-Meteorological Observatory
(TAHMO) station network through the TAHMO API v2 and writes a CF-1.13
timeSeries (point_obs) Zarr.

Per station and variable, the skill:

1. requests only that variable, in windows of at most 31 days, in parallel;
2. keeps samples whose TAHMO quality flag is at or below `--max-quality`
   (default: good only) and drops null values (sensor errors);
3. keeps the best-flagged sensor where a station has several for one variable;
4. for `daily`/`hourly`, reduces the 5-minute logger samples into bins
   (sum for rain, max for gusts, circular mean for direction, mean otherwise)
   and sets any bin with less than `--min-coverage` of its expected samples
   to NaN, so a gap is never reported as a dry day.

## When to use

- In-situ rainfall/temperature/humidity/etc. for African stations, daily or
  sub-daily.
- Ground truth for `plot-compare` against CHIRPS/IMERG/ERA5/forecasts, or input
  to `aggregate-temporal`.
- Discovering which stations an account can access (`--list-stations`).

For credential-free worldwide daily stations, `ghcn-daily-fetch` is an alternative.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py (--station CODE | --country C | --bbox N/W/S/E)... \
    --start-time YYYY-MM-DD --end-time YYYY-MM-DD [-v VAR ...] [--resolution daily|hourly|native] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations [--country C] [--bbox N/W/S/E] [--active-only]
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [VARIABLE] [--country C | --station CODE]
```

### Selecting stations

Each selector narrows the selection; combine them freely. At least one is
required for a fetch.

- `--station` — TAHMO station code, e.g. `TA00001` (repeatable).
- `--country` — ISO alpha-2 (`KE`), alpha-3 (`KEN`), or English name
  (`Kenya`, `Côte d'Ivoire`, `Ivory Coast`) (repeatable).
- `--bbox` — `N/W/S/E` decimal degrees. For a named region, get a bbox from
  `resolve-region`; `--country` is usually more precise than a country bbox.
- `--active-only` — drop stations TAHMO currently marks inactive. Leave it off
  for historical periods: inactive stations may still have old data.

Stations installed after `--end-time` are skipped automatically.

### Arguments

- `--start-time`, `--end-time` — inclusive date range, absolute `YYYY-MM-DD`.
  For relative windows ("last two weeks") use `resolve-time`; for the latest
  published day use `--probe-latest`. There is no upper limit on range length.
- `--variable`, `-v` — repeatable; default `precip temperature humidity pressure`.
  Names from the table below; raw TAHMO shortcodes (`pr`, `te`, `rh`, …) are
  also accepted. Variables no selected station reports are omitted with a warning.
- `--resolution` — `daily` (default), `hourly`, or `native` (the logger
  interval, normally 5 minutes). `tmax`/`tmin` need `hourly` or `daily`.
- `--dataset` — `controlled` (default: full TAHMO QC) or `raw` (only
  sensor-error flags). Use `raw` only when you need values QC removed.
- `--max-quality` — worst flag kept: `1` good (default), `2` suspect,
  `3` doubtful, `4` erroneous.
- `--min-coverage` — fraction (0–1] of expected samples a daily/hourly bin
  needs (default `0.8`). Lower it to keep partial days, knowing that partial
  rain totals are underestimates.
- `--day-boundary` — `local` (default) bins daily values on each station's
  local calendar day, as TAHMO recommends; `utc` bins on UTC days (to match a
  UTC-day gridded product such as IMERG daily). Only affects `daily`.
- `--day-start-hour` — hour a daily bin starts (default `0`). `9` gives
  09:00–09:00 rain-gauge days, labelled with the date the day starts. Only
  affects `daily`.
- `--workers` — concurrent API requests (default 8). Lower it on HTTP 429.
  Does not change the output.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--list-stations` — print matching stations as CSV (`code, name, country,
  latitude, longitude, elevation, timezone, active, installed`) and exit. No `-o`.
- `--probe-latest [VARIABLE]` — print the latest UTC date (`YYYY-MM-DD`) with
  observations of VARIABLE (default `precip`) at a few active stations in the
  selection, or `none`, and exit. No `-o`.

### Variables

| `-v` | TAHMO | Units out | Daily/hourly reduction |
| --- | --- | --- | --- |
| `precip` | `pr` | `mm day-1` (rate) | sum → rate |
| `temperature` | `te` | `degree_Celsius` | mean |
| `tmax` | `te` | `degree_Celsius` | max |
| `tmin` | `te` | `degree_Celsius` | min |
| `humidity` | `rh` | `%` (API gives 0–1; ×100) | mean |
| `pressure` | `ap` | `kPa` | mean |
| `wind_speed` | `ws` | `m s-1` | mean |
| `wind_gust` | `wg` | `m s-1` | max |
| `wind_direction` | `wd` | `degree` (from) | circular mean |
| `radiation` | `ra` | `W m-2` | mean |
| `vapor_pressure` | `vp` | `kPa` | mean |
| `soil_moisture` | `sm` | `m3 m-3` | mean |
| `soil_temperature` | `st` | `degree_Celsius` | mean |

Precipitation is always written as a **rate** in `mm day-1`, per the
weather-skills units contract. At daily resolution the number equals the day's
total in mm. At hourly or native resolution, multiply by the interval to get
depth, or use `aggregate-temporal` and then `convert-to-totals`.

### Output

A Zarr with dims `(time, station_id)` and coords `latitude`, `longitude`,
`elevation` (m), `name`, `country` (ISO alpha-2), and `timezone` on
`station_id`. Only stations with at least one usable value are included.
Missing cells are NaN.

- `daily`: `time` is the observation day's date (local or UTC, per
  `--day-boundary`); `data_interval="1 day"`.
- `hourly`: `time` is the start of the UTC hour; `data_interval="1 hour"`.
- `native`: `time` is the logger timestamp (UTC), which marks the end of the
  sampling interval; `data_interval` is the logger interval (e.g. `5 minute`).

TAHMO timestamps mark the end of each logger interval, so bins are
right-closed: the 00:00 sample belongs to the previous day.

Each variable carries `units`, `standard_name`, `long_name`, `cell_methods`
(when aggregated), and `tahmo_shortcode`/`tahmo_dataset`/`tahmo_max_quality`.
Global attrs record the settings (`tahmo_resolution`, `tahmo_min_coverage`,
`tahmo_day_boundary`, …), `weather_skills_source=tahmo`, and
`featureType=timeSeries`.

### Failures

- Missing or rejected credentials → exit 2 with a message; nothing is written.
- Unknown station code or country → exit 2 and a list of what is available.
- A station the account cannot read (HTTP 403), or one whose requests still
  fail after retries, is dropped and logged as `CODE: DROPPED (reason)` on
  stderr; the other stations are still written.
- No usable data at all → exit 1.

Transient errors (HTTP 429/5xx, timeouts) are retried with backoff.

### Provenance

The decorator stamps `weather_skills_history` with this skill's name, version,
and arguments. Inspect it with the `provenance` skill.

## Examples

```bash
# Which Kenyan stations can this account read?
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations --country Kenya --active-only

# Daily rain + max/min temperature for all Ghana stations in March 2026
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --country GH --start-time 2026-03-01 --end-time 2026-03-31 \
    -v precip -v tmax -v tmin -o /tmp/tahmo_gh.zarr

# 09:00-09:00 rain-gauge days for two stations, UTC days for IMERG comparison
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --station TA00001 --station TA00002 \
    --start-time 2026-01-01 --end-time 2026-01-31 -v precip --day-boundary utc -o /tmp/rain.zarr

# Hourly wind at stations in a bbox
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5/34/-5/42 --start-time 2026-02-01 --end-time 2026-02-02 \
    --resolution hourly -v wind_speed -v wind_direction -o /tmp/wind.zarr
```

See [references/REFERENCE.md](references/REFERENCE.md) for API details.
