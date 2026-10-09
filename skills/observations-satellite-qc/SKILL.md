---
name: observations-satellite-qc
description: Remove TAHMO station rainfall that disagrees with NASA IMERG Early satellite estimates. Takes a daily station Zarr from the observations skill, samples IMERG at each station (credential-free Dynamical API), compares daily totals with absolute-difference, ratio, and wet/dry rules, sets failing station-days to NaN, and records why in a flag variable. Use to clean station rainfall before analysis, or to find stations whose gauges disagree with satellite.
license: MIT
compatibility: Requires Python 3.12 and uv. Calls the public Dynamical point API (https://api.dynamical.org) over HTTPS; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/observations_satellite_qc.py *)
metadata:
  catalog-group: transforms
---

# observations-satellite-qc

Quality-controls daily station rainfall against NASA IMERG Early. For each
station and day it:

1. samples IMERG Early half-hourly precipitation at the station's grid cell
   (nearest 0.1° cell) from the Dynamical point API;
2. sums the half-hours over **the same day the station value covers**: local or
   UTC, starting at the same hour, read from the input's
   `tahmo_day_boundary` / `tahmo_day_start_hour` attrs and `timezone` coord;
3. compares the two daily totals and removes (sets to NaN) station-days that
   fail any rule.

Only the checked variable is changed; other variables pass through untouched.

## When to use

- Cleaning station rainfall before computing totals, climatologies, or
  comparisons with forecasts.
- Finding stations or periods where the gauge and satellite disagree (blocked
  funnels, broken sensors, local storms).

Satellite estimates also have errors, especially for short convective storms
and on light-rain days, so a flag means "disagrees with IMERG", not "the gauge
is wrong". Check the per-station summary before drawing conclusions.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/observations_satellite_qc.py -i <daily_obs.zarr> -o <clean.zarr> \
    [-v precip] [--abs-threshold 10] [--ratio-threshold 2] [--wet-threshold 1] [--keep-unverified]
```

The input must be **daily** station precipitation in `mm day-1`, e.g. from
`observations --resolution daily -v precip`.

### Arguments

- `-i`, `--input` — daily point_obs Zarr with `latitude`/`longitude` per station.
- `-o`, `--output` — output Zarr path.
- `--variable`, `-v` — precipitation variable to check (default `precip`).
- `--abs-threshold` — flag when |station − IMERG| ≥ this many mm (default `10`).
- `--ratio-threshold` — flag when the larger total is ≥ this many times the
  smaller, when both are above 0 (default `2`).
- `--wet-threshold` — flag when one side is above this many mm and the other is
  not (default `1`).
- `--keep-unverified` — keep station-days with no complete IMERG day to compare
  against. By default they are removed. IMERG Early usually lags real time by
  several hours, so the latest day is often unverified.
- `--workers` — concurrent API requests (default 4).

A station-day is removed if **any** rule fires. Raise `--ratio-threshold` to
stop removing light-rain days where both totals are small (e.g. 0.7 mm vs
0.3 mm fails the default ratio rule).

## Output

The input dataset, with:

- `precip` (or `-v`): disagreeing station-days set to NaN; its
  `ancillary_variables` attr points at the two variables below.
- `satellite_precip`: IMERG Early daily total at the station (`mm day-1`), NaN
  when IMERG is missing any half-hour of the day.
- `satellite_qc`: CF flag variable (`int8`). `0` means the day agrees. Otherwise
  it is the sum of `flag_masks`: `1` abs_difference, `2` ratio,
  `4` wet_dry_mismatch, `8` no_satellite. `-1` (`_FillValue`) means no station
  value that day.

Global attrs record the product and thresholds (`satellite_qc_*`). The run
prints station-days kept and removed, hits per rule, and the stations with the
most removed days to stderr.

## Failures

- Input not daily, not in `mm day-1`, or missing coordinates → exit 2.
- Dynamical API errors are retried with backoff; a request that still fails
  stops the run (exit 1). Nothing is written.

## Examples

```bash
# Daily Kenya rainfall for March, then remove days that disagree with IMERG
uv run ${CLAUDE_SKILL_DIR}/../observations/scripts/fetch.py --country KE \
    --start-time 2026-03-01 --end-time 2026-03-31 -v precip -o /tmp/ke.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/observations_satellite_qc.py -i /tmp/ke.zarr -o /tmp/ke_clean.zarr

# Only remove large disagreements
uv run ${CLAUDE_SKILL_DIR}/scripts/observations_satellite_qc.py -i /tmp/ke.zarr -o /tmp/ke_clean.zarr \
    --ratio-threshold 4 --keep-unverified
```

## Provenance

The decorator appends a `observations-satellite-qc` entry to the input's
`weather_skills_history`, so the chain records `observations` → `observations-satellite-qc`.
