# TAHMO Skills

Agent skills for data from the
[Trans-African Hydro-Meteorological Observatory (TAHMO)](https://tahmo.org)
station network. The skills are built on
[`weather-skills-core`](https://github.com/rhiza-research/weather-skills-core)
(pinned to `main`), so their outputs are standard weather-skills Zarr stores
that compose with the [weather-skills](https://github.com/rhiza-research/weather-skills)
transforms and plotters (`aggregate-temporal`, `plot-compare`, …).

## Skills

| Skill | Role |
| --- | --- |
| [`tahmo-fetch`](skills/tahmo-fetch/) | List accessible stations, and fetch quality-controlled observations (rain, temperature incl. max/min, humidity, pressure, wind, radiation, soil) at daily, hourly, or native 5-min resolution into a point_obs Zarr |

## Quick start

```bash
export TAHMO_API_USERNAME=...   # your TAHMO API id
export TAHMO_API_PASSWORD=...   # your TAHMO API secret

uv sync --group dev
uv run pytest

# Which stations can this account read?
uv run skills/tahmo-fetch/scripts/fetch.py --list-stations --country Kenya

# Daily rainfall and max/min temperature for Kenya, March 2026
uv run skills/tahmo-fetch/scripts/fetch.py --country KE \
  --start-time 2026-03-01 --end-time 2026-03-31 -v precip -v tmax -v tmin -o kenya.zarr

# Latest day with data
uv run skills/tahmo-fetch/scripts/fetch.py --probe-latest --country KE
```

## Install as a Claude plugin

```bash
./install_agent.sh
# or:
claude plugin marketplace add TAHMO/tahmo-skills
claude plugin install tahmo@tahmo-skills
```

## Layout

This repo uses the same packaging model as
[`chc-skills`](https://github.com/rhiza-research/chc-skills): canonical skills live
under `skills/<name>/` (`SKILL.md`, `scripts/`, `tests/`, `references/`), the
Claude plugin is defined by `.claude-plugin/` and `agents/`, and the CLI comes
from `skills-runner`.

See [CONTRIBUTING.md](CONTRIBUTING.md).
