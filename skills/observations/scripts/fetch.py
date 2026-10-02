# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cf_xarray",
#   "cftime",
#   "numpy",
#   "pandas",
#   "pint-xarray>=0.6",
#   "requests",
#   "xarray",
#   "zarr",
# ]
# ///
"""Fetch TAHMO station observations from the TAHMO API and write a point_obs Zarr.

Talks to the TAHMO API v2 (https://datahub.tahmo.org) directly over HTTPS with
HTTP Basic auth. Credentials: TAHMO_API_USERNAME and TAHMO_API_PASSWORD.
"""

from __future__ import annotations

import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd
import requests
import xarray as xr
from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_dsg, udunits_error, verify_cf_dsg
from weather_skills_core.standard_utils import apply_write_encoding, require_env
from weather_skills_core.units import precip_amounts_to_rates, stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.0"

API_BASE_URL = "https://datahub.tahmo.org"
STATIONS_PATH = "services/assets/v2/stations"
MEASUREMENTS_PATH = "services/measurements/v2/stations/{station}/measurements/{dataset}"

HTTP_TIMEOUT = 120
MAX_ATTEMPTS = 4
BACKOFF_BASE_S = 2.0
# The API caps one request at one year. Smaller windows keep each response
# small (the API is slow per byte) and spread work across --workers.
CHUNK_DAYS = 31
DEFAULT_WORKERS = 8
DEFAULT_MIN_COVERAGE = 0.8
DEFAULT_MAX_QUALITY = 1
PROBE_STATIONS = 5

DATASETS = ("controlled", "raw")
RESOLUTIONS = ("daily", "hourly", "native")
DAY_BOUNDARIES = ("local", "utc")

# TAHMO quality flags (TAHMO Measurements API documentation, rev 1.01).
QUALITY_LABELS = {1: "good", 2: "suspect", 3: "doubtful", 4: "erroneous"}

_TIME_UNITS = "minutes since 1970-01-01 00:00:00"
_TIME_CALENDAR = "proleptic_gregorian"


@dataclass(frozen=True)
class VarSpec:
    shortcode: str
    units: str
    standard_name: str
    long_name: str
    reducer: str  # sum | mean | max | min | circmean
    scale: float = 1.0
    native: bool = True  # False: only meaningful after aggregation (tmax/tmin)


# Canonical output name -> how to build it from a TAHMO shortcode. Precip is
# summed to an amount (mm) per interval and converted to a mm day-1 rate before
# write, per the weather-skills units contract.
VARIABLES: dict[str, VarSpec] = {
    "precip": VarSpec("pr", "mm", "lwe_thickness_of_precipitation_amount", "precipitation", "sum"),
    "temperature": VarSpec("te", "degree_Celsius", "air_temperature", "air temperature", "mean"),
    "tmax": VarSpec(
        "te", "degree_Celsius", "air_temperature", "maximum air temperature", "max", native=False
    ),
    "tmin": VarSpec(
        "te", "degree_Celsius", "air_temperature", "minimum air temperature", "min", native=False
    ),
    "humidity": VarSpec("rh", "%", "relative_humidity", "relative humidity", "mean", scale=100.0),
    "pressure": VarSpec("ap", "kPa", "surface_air_pressure", "atmospheric pressure", "mean"),
    "wind_speed": VarSpec("ws", "m s-1", "wind_speed", "wind speed", "mean"),
    "wind_gust": VarSpec("wg", "m s-1", "wind_speed_of_gust", "wind gust", "max"),
    "wind_direction": VarSpec("wd", "degree", "wind_from_direction", "wind direction", "circmean"),
    "radiation": VarSpec(
        "ra",
        "W m-2",
        "surface_downwelling_shortwave_flux_in_air",
        "shortwave radiation",
        "mean",
    ),
    "vapor_pressure": VarSpec(
        "vp", "kPa", "water_vapor_partial_pressure_in_air", "vapor pressure", "mean"
    ),
    "soil_moisture": VarSpec(
        "sm",
        "m3 m-3",
        "volume_fraction_of_condensed_water_in_soil",
        "soil moisture content",
        "mean",
    ),
    "soil_temperature": VarSpec(
        "st", "degree_Celsius", "soil_temperature", "soil temperature", "mean"
    ),
}
DEFAULT_VARIABLES = ["precip", "temperature", "humidity", "pressure"]
_SHORTCODE_TO_NAME = {}
for _name, _spec in VARIABLES.items():
    _SHORTCODE_TO_NAME.setdefault(_spec.shortcode, _name)

_CELL_METHOD = {"sum": "sum", "mean": "mean", "max": "maximum", "min": "minimum"}

# Countries with TAHMO stations (plus the rest of Africa so names resolve).
# name -> (ISO 3166-1 alpha-2, alpha-3). Any other alpha-2 code is accepted as-is.
COUNTRIES: dict[str, tuple[str, str]] = {
    "Algeria": ("DZ", "DZA"),
    "Angola": ("AO", "AGO"),
    "Benin": ("BJ", "BEN"),
    "Botswana": ("BW", "BWA"),
    "Burkina Faso": ("BF", "BFA"),
    "Burundi": ("BI", "BDI"),
    "Cabo Verde": ("CV", "CPV"),
    "Cameroon": ("CM", "CMR"),
    "Central African Republic": ("CF", "CAF"),
    "Chad": ("TD", "TCD"),
    "Comoros": ("KM", "COM"),
    "Congo": ("CG", "COG"),
    "Côte d'Ivoire": ("CI", "CIV"),
    "DR Congo": ("CD", "COD"),
    "Djibouti": ("DJ", "DJI"),
    "Egypt": ("EG", "EGY"),
    "Equatorial Guinea": ("GQ", "GNQ"),
    "Eritrea": ("ER", "ERI"),
    "Eswatini": ("SZ", "SWZ"),
    "Ethiopia": ("ET", "ETH"),
    "Gabon": ("GA", "GAB"),
    "Gambia": ("GM", "GMB"),
    "Ghana": ("GH", "GHA"),
    "Guinea": ("GN", "GIN"),
    "Guinea-Bissau": ("GW", "GNB"),
    "Kenya": ("KE", "KEN"),
    "Lesotho": ("LS", "LSO"),
    "Liberia": ("LR", "LBR"),
    "Libya": ("LY", "LBY"),
    "Madagascar": ("MG", "MDG"),
    "Malawi": ("MW", "MWI"),
    "Mali": ("ML", "MLI"),
    "Mauritania": ("MR", "MRT"),
    "Mauritius": ("MU", "MUS"),
    "Morocco": ("MA", "MAR"),
    "Mozambique": ("MZ", "MOZ"),
    "Namibia": ("NA", "NAM"),
    "Niger": ("NE", "NER"),
    "Nigeria": ("NG", "NGA"),
    "Rwanda": ("RW", "RWA"),
    "São Tomé and Príncipe": ("ST", "STP"),
    "Senegal": ("SN", "SEN"),
    "Seychelles": ("SC", "SYC"),
    "Sierra Leone": ("SL", "SLE"),
    "Somalia": ("SO", "SOM"),
    "South Africa": ("ZA", "ZAF"),
    "South Sudan": ("SS", "SSD"),
    "Sudan": ("SD", "SDN"),
    "Tanzania": ("TZ", "TZA"),
    "Togo": ("TG", "TGO"),
    "Tunisia": ("TN", "TUN"),
    "Uganda": ("UG", "UGA"),
    "Zambia": ("ZM", "ZMB"),
    "Zimbabwe": ("ZW", "ZWE"),
}
_COUNTRY_ALIASES = {
    "cote d'ivoire": "CI",
    "cote divoire": "CI",
    "ivory coast": "CI",
    "drc": "CD",
    "democratic republic of the congo": "CD",
    "congo-kinshasa": "CD",
    "republic of the congo": "CG",
    "congo-brazzaville": "CG",
    "the gambia": "GM",
    "swaziland": "SZ",
    "cape verde": "CV",
    "united republic of tanzania": "TZ",
}


class AuthError(UsageError):
    """HTTP 401: credentials rejected. Aborts the whole run."""


class StationForbidden(DataError):
    """HTTP 403: this account cannot read the station's measurements."""


# --------------------------------------------------------------------------- HTTP


class TahmoClient:
    """Minimal thread-safe TAHMO API v2 client (one requests.Session per thread)."""

    def __init__(self, username: str, password: str, base_url: str = API_BASE_URL):
        self._auth = (username, password)
        self._base = base_url.rstrip("/")
        self._local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.auth = self._auth
            session.headers["Accept"] = "application/json"
            self._local.session = session
        return session

    def get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self._base}/{path}"
        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self._session().get(url, params=params, timeout=HTTP_TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                resp = None
            if resp is not None:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        last_error = "response was not JSON"
                elif resp.status_code == 401:
                    raise AuthError(
                        "TAHMO rejected the credentials (HTTP 401). Check "
                        "TAHMO_API_USERNAME and TAHMO_API_PASSWORD."
                    )
                elif resp.status_code == 403:
                    raise StationForbidden(_api_message(resp) or "HTTP 403 forbidden")
                elif resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code} {_api_message(resp)}".strip()
                else:
                    raise DataError(
                        f"TAHMO API error for {path} {params or ''}: HTTP "
                        f"{resp.status_code} {_api_message(resp)}".strip()
                    )
            if attempt < MAX_ATTEMPTS:
                delay = _retry_after(resp) or BACKOFF_BASE_S * 2 ** (attempt - 1)
                time.sleep(delay + random.uniform(0, 0.5))
        raise DataError(f"TAHMO API request failed after {MAX_ATTEMPTS} attempts: {last_error}")

    def stations(self) -> pd.DataFrame:
        payload = self.get(STATIONS_PATH, {"sort": "code"})
        rows = []
        for item in payload.get("data") or []:
            loc = item.get("location") or {}
            rows.append(
                {
                    "code": str(item.get("code", "")).upper(),
                    "name": (loc.get("name") or "").strip(),
                    "country": (loc.get("countrycode") or "").upper(),
                    "latitude": _float(loc.get("latitude")),
                    "longitude": _float(loc.get("longitude")),
                    "elevation": _float(loc.get("elevationmsl")),
                    "timezone": loc.get("timezone") or "",
                    "active": item.get("status") == 1,
                    "installed": (item.get("installationdate") or "")[:10],
                }
            )
        cols = [
            "code",
            "name",
            "country",
            "latitude",
            "longitude",
            "elevation",
            "timezone",
            "active",
            "installed",
        ]
        return pd.DataFrame(rows, columns=cols).set_index("code", drop=False)

    def measurements(
        self,
        station: str,
        shortcode: str,
        start: datetime | None,
        end: datetime | None,
        dataset: str,
    ) -> pd.DataFrame:
        """One (station, variable) window as a long frame: time, value, quality, sensor."""
        params = {"variable": shortcode}
        if start is not None:
            params["start"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        if end is not None:
            params["end"] = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = self.get(MEASUREMENTS_PATH.format(station=station, dataset=dataset), params)
        frames = []
        for result in payload.get("results") or []:
            for series in result.get("series") or []:
                columns = series.get("columns") or []
                values = series.get("values") or []
                if values:
                    frames.append(pd.DataFrame(values, columns=columns))
        if not frames:
            return _empty_measurements()
        df = pd.concat(frames, ignore_index=True)
        missing = {"time", "value", "quality", "variable"} - set(df.columns)
        if missing:
            raise DataError(f"{station}: measurement response lacks columns {sorted(missing)}")
        if "sensor" not in df.columns:
            df["sensor"] = ""
        df = df[df["variable"] == shortcode]
        return df[["time", "value", "quality", "sensor"]]


def _empty_measurements() -> pd.DataFrame:
    return pd.DataFrame(columns=["time", "value", "quality", "sensor"])


def _api_message(resp) -> str:
    try:
        return str(resp.json()["error"]["message"])
    except Exception:  # noqa: BLE001
        return ""


def _retry_after(resp) -> float | None:
    if resp is None:
        return None
    try:
        return min(float(resp.headers.get("Retry-After", "")), 60.0)
    except ValueError:
        return None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


# ------------------------------------------------------------------ selection


def resolve_country(token: str, known_codes: set[str]) -> str:
    """ISO alpha-2 / alpha-3 / English name -> alpha-2."""
    raw = token.strip()
    upper = raw.upper()
    for name, (iso2, iso3) in COUNTRIES.items():
        if upper in (iso2, iso3) or raw.casefold() == name.casefold():
            return iso2
    alias = _COUNTRY_ALIASES.get(raw.casefold().replace("’", "'"))
    if alias:
        return alias
    if len(upper) == 2 and upper.isalpha() and upper in known_codes:
        return upper
    raise UsageError(
        f"unknown --country {token!r}. Use an ISO alpha-2/alpha-3 code or an English "
        f"name, e.g. KE, KEN, Kenya. Countries visible to this account: "
        f"{', '.join(sorted(known_codes)) or 'none'}"
    )


def select_stations(stations: pd.DataFrame, *, station, country, bbox, active_only):
    """Apply every given selector (each one narrows the set)."""
    sel = stations
    if station:
        wanted = [s.strip().upper() for s in station]
        unknown = [s for s in wanted if s not in stations.index]
        if unknown:
            raise UsageError(
                f"station(s) {unknown} are not visible to this TAHMO account (unknown "
                "code or no access). Run --list-stations to see what is available."
            )
        sel = sel.loc[list(dict.fromkeys(wanted))]
    if country:
        known = set(stations["country"].dropna())
        codes = {resolve_country(c, known) for c in country}
        sel = sel[sel["country"].isin(codes)]
    if bbox is not None:
        north, west, south, east = bbox
        lat, lon = sel["latitude"], sel["longitude"]
        in_lon = (lon >= west) & (lon <= east) if west <= east else (lon >= west) | (lon <= east)
        sel = sel[(lat <= north) & (lat >= south) & in_lon]
    if active_only:
        sel = sel[sel["active"]]
    return sel


# ------------------------------------------------------------------ time windows


def station_zone(tz_name: str, code: str, warned: set) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        if code not in warned:
            print(
                f"{code}: unknown timezone {tz_name!r}; using UTC day boundaries", file=sys.stderr
            )
            warned.add(code)
        return ZoneInfo("UTC")


def utc_window(start: date, end: date, zone: ZoneInfo, day_start_hour: int):
    """UTC [start, end) covering local days start..end (inclusive), shifted by the hour."""
    lo = datetime(start.year, start.month, start.day, tzinfo=zone) + timedelta(hours=day_start_hour)
    hi = datetime(end.year, end.month, end.day, tzinfo=zone) + timedelta(
        days=1, hours=day_start_hour
    )
    return lo.astimezone(UTC).replace(tzinfo=None), hi.astimezone(UTC).replace(tzinfo=None)


def chunk_window(lo: datetime, hi: datetime, days: int = CHUNK_DAYS):
    """Split [lo, hi] into inclusive request windows no longer than ``days``."""
    out = []
    cur = lo
    step = timedelta(days=days)
    while cur <= hi:
        nxt = min(cur + step, hi)
        out.append((cur, nxt))
        if nxt >= hi:
            break
        cur = nxt + timedelta(seconds=1)
    return out


# ------------------------------------------------------------------ processing


def clean_samples(raw: pd.DataFrame, max_quality: int, scale: float) -> pd.Series:
    """Quality-filter one station-variable and pick one sensor value per timestamp.

    Drops null values (sensor errors) and flags above ``max_quality``. Where
    several sensors report the same timestamp, keeps the best flag (ties broken
    by sensor code so the pick is deterministic). Returns a UTC-naive series.
    """
    if raw.empty:
        return pd.Series(dtype=float)
    df = raw.dropna(subset=["value"]).copy()
    df["quality"] = pd.to_numeric(df["quality"], errors="coerce")
    df = df[df["quality"] <= max_quality]
    if df.empty:
        return pd.Series(dtype=float)
    df["time"] = pd.to_datetime(df["time"], utc=True, format="ISO8601").dt.tz_convert(None)
    df["sensor"] = df["sensor"].fillna("").astype(str)
    df = df.sort_values(["time", "quality", "sensor"]).drop_duplicates("time", keep="first")
    return pd.Series(df["value"].astype(float).to_numpy() * scale, index=df["time"].to_numpy())


def native_interval(index: pd.DatetimeIndex) -> pd.Timedelta | None:
    """Most common positive spacing between samples (the logger interval)."""
    if len(index) < 2:
        return None
    diffs = pd.Series(np.diff(index.values.astype("datetime64[ns]")))
    diffs = diffs[diffs > pd.Timedelta(0)]
    if diffs.empty:
        return None
    return pd.Timedelta(diffs.mode().iloc[0])


def _circmean(values: np.ndarray) -> float:
    rad = np.deg2rad(values)
    deg = float(np.rad2deg(np.arctan2(np.sin(rad).mean(), np.cos(rad).mean())) % 360.0)
    return 0.0 if np.isclose(deg, 360.0) else deg  # -1e-15 % 360 rounds to 360.0


def aggregate(
    samples: pd.Series,
    reducer: str,
    *,
    freq: str,
    zone: ZoneInfo | None,
    day_start_hour: int,
    min_coverage: float,
    interval: pd.Timedelta | None,
) -> pd.Series:
    """Reduce samples into hourly/daily bins with a completeness check.

    TAHMO timestamps mark the end of the logger interval, so bins are
    right-closed: (t0, t0 + period]. The label is the bin start. For daily
    bins the start is local midnight (``zone``) plus ``day_start_hour``, and
    the label is that local calendar date. Bins with fewer than
    ``min_coverage`` of the expected samples are NaN.
    """
    if samples.empty:
        return pd.Series(dtype=float)
    times = pd.DatetimeIndex(samples.index)
    if zone is not None:
        times = times.tz_localize("UTC").tz_convert(zone).tz_localize(None)
    shifted = times - pd.Timedelta(hours=day_start_hour) - pd.Timedelta(1, "ns")
    period = pd.Timedelta(days=1) if freq == "D" else pd.Timedelta(hours=1)
    keys = shifted.floor(freq)
    grouped = pd.Series(samples.to_numpy(), index=keys).groupby(level=0)

    if reducer == "circmean":
        values = grouped.agg(lambda s: _circmean(s.to_numpy()))
    else:
        values = grouped.agg(reducer)
    counts = grouped.count()
    if interval is None or interval <= pd.Timedelta(0):
        expected = 1.0
    else:
        expected = max(period / interval, 1.0)
    coverage = (counts / expected).clip(upper=1.0)
    return values.where(coverage >= min_coverage)


# ------------------------------------------------------------------ dataset


def _var_attrs(name: str, resolution: str, dataset: str, max_quality: int) -> dict:
    spec = VARIABLES[name]
    exc = udunits_error(spec.units)
    if exc is not None:
        raise DataError(f"units {spec.units!r} for {name!r} are not udunits-valid ({exc})")
    attrs = {
        "units": spec.units,
        "standard_name": spec.standard_name,
        "long_name": f"{resolution} {spec.long_name}" if resolution != "native" else spec.long_name,
        "tahmo_shortcode": spec.shortcode,
        "tahmo_dataset": dataset,
        "tahmo_max_quality": np.int8(max_quality),
    }
    if resolution != "native" and spec.reducer in _CELL_METHOD:
        # Precip becomes a mean rate over the bin; "time: sum" would mark it as
        # a total, which weather-skills rate math (convert-to-totals) refuses.
        method = "mean" if name == "precip" else _CELL_METHOD[spec.reducer]
        attrs["cell_methods"] = f"time: {method}"
    return attrs


def build_dataset(series_by_var: dict, meta: pd.DataFrame, time_index: pd.DatetimeIndex):
    """``{var: {station: Series}}`` -> Dataset with dims (time, station_id)."""
    stations = sorted({sid for per in series_by_var.values() for sid in per})
    data_vars = {}
    for name, per_station in series_by_var.items():
        grid = np.full((len(time_index), len(stations)), np.nan)
        for j, sid in enumerate(stations):
            s = per_station.get(sid)
            if s is None or s.empty:
                continue
            grid[:, j] = s.reindex(time_index).to_numpy(dtype=float)
        data_vars[name] = (("time", "station_id"), grid)
    m = meta.loc[stations]
    return xr.Dataset(
        data_vars,
        coords={
            "time": time_index.values.astype("datetime64[ns]"),
            "station_id": np.array(stations, dtype=str),
            "latitude": ("station_id", m["latitude"].to_numpy(dtype=float)),
            "longitude": ("station_id", m["longitude"].to_numpy(dtype=float)),
            "elevation": ("station_id", m["elevation"].to_numpy(dtype=float)),
            "name": ("station_id", m["name"].to_numpy(dtype=str)),
            "country": ("station_id", m["country"].to_numpy(dtype=str)),
            "timezone": ("station_id", m["timezone"].to_numpy(dtype=str)),
        },
    )


def _resolve_variables(variable) -> list[str]:
    names = []
    for token in variable or DEFAULT_VARIABLES:
        key = token.strip()
        key = _SHORTCODE_TO_NAME.get(key, key)  # accept raw TAHMO shortcodes (pr, te, …)
        if key not in VARIABLES:
            raise UsageError(
                f"unknown --variable {token!r}. Choose from: {', '.join(VARIABLES)} "
                f"(or the TAHMO shortcodes {', '.join(sorted(_SHORTCODE_TO_NAME))})"
            )
        names.append(key)
    return list(dict.fromkeys(names))


def _client() -> TahmoClient:
    username, password = require_env(
        "TAHMO_API_USERNAME",
        "TAHMO_API_PASSWORD",
        message=(
            "TAHMO_API_USERNAME and TAHMO_API_PASSWORD must be set to your TAHMO API "
            "credentials (request access at https://tahmo.org)."
        ),
    )
    return TahmoClient(username, password)


def _print_stations(sel: pd.DataFrame) -> None:
    out = sel.copy()
    out["active"] = out["active"].map({True: "yes", False: "no"})
    out.to_csv(sys.stdout, index=False, float_format="%.5f")


def _probe_latest(client, stations: pd.DataFrame, ident: str) -> str:
    names = _resolve_variables([ident] if ident else ["precip"])
    shortcode = VARIABLES[names[0]].shortcode
    candidates = stations[stations["active"]].head(PROBE_STATIONS)
    if candidates.empty:
        candidates = stations.head(PROBE_STATIONS)
    latest = None
    for code in candidates.index:
        try:
            # No start/end: the API returns the last 24 hours only (cheap).
            raw = client.measurements(code, shortcode, None, None, "raw")
        except StationForbidden:
            continue
        if raw.empty:
            continue
        ts = pd.to_datetime(raw["time"], utc=True, format="ISO8601").max()
        latest = ts if latest is None else max(latest, ts)
    return latest.date().isoformat() if latest is not None else "none"


# ------------------------------------------------------------------ skill


@weather_skill(name="observations", version=_SKILL_VERSION)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--variable",
    "-v",
    action="append",
    help=(
        "Variable to fetch (repeatable): " + ", ".join(VARIABLES) + ". TAHMO shortcodes "
        "(pr, te, rh, ...) also work. Default: " + " ".join(DEFAULT_VARIABLES) + "."
    ),
)
@weather_skill.argument(
    "--country",
    action="append",
    help="Country to select stations from (repeatable): ISO alpha-2, alpha-3, or English name.",
)
@weather_skill.argument(
    "--station",
    action="append",
    help="TAHMO station code, e.g. TA00001 (repeatable).",
)
@weather_skill.argument(
    "--active-only",
    action="store_true",
    help="Only stations TAHMO currently marks active.",
)
@weather_skill.argument(
    "--resolution",
    choices=RESOLUTIONS,
    default="daily",
    help="Output time step: daily (default), hourly, or native (logger interval, ~5 min).",
)
@weather_skill.argument(
    "--dataset",
    choices=DATASETS,
    default="controlled",
    help="TAHMO collection: controlled (full QC, default) or raw (sensor-error flags only).",
)
@weather_skill.argument(
    "--max-quality",
    type=int,
    choices=sorted(QUALITY_LABELS),
    default=DEFAULT_MAX_QUALITY,
    help="Worst TAHMO quality flag kept: 1 good (default), 2 suspect, 3 doubtful, 4 erroneous.",
)
@weather_skill.argument(
    "--min-coverage",
    type=float,
    default=DEFAULT_MIN_COVERAGE,
    help=(
        "Fraction (0-1] of expected logger samples a daily/hourly value needs; "
        f"sparser bins are NaN (default {DEFAULT_MIN_COVERAGE})."
    ),
)
@weather_skill.argument(
    "--day-boundary",
    choices=DAY_BOUNDARIES,
    default="local",
    help="Daily bins follow each station's local day (default, TAHMO guidance) or UTC days.",
)
@weather_skill.argument(
    "--day-start-hour",
    type=int,
    default=0,
    help="Hour (0-23) a daily bin starts, e.g. 9 for 09:00-09:00 rain-gauge days (default 0).",
)
@weather_skill.argument(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    help=f"Max concurrent API requests (default {DEFAULT_WORKERS}). Lower on HTTP 429.",
)
@weather_skill.argument(
    "--list-stations",
    action="store_true",
    probe=True,
    help=(
        "Print the stations visible to this account (after --country/--station/--bbox/"
        "--active-only filters) as CSV on stdout and exit. No -o."
    ),
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    default=None,
    metavar="VARIABLE",
    probe=True,
    help=(
        "Print the latest UTC date with observations (or none) on stdout and exit. "
        "Checks a few active stations in the selection; optional VARIABLE (default precip)."
    ),
)
def fetch(
    start_time,
    end_time,
    bbox,
    variable,
    country,
    station,
    active_only,
    resolution,
    dataset,
    max_quality,
    min_coverage,
    day_boundary,
    day_start_hour,
    workers,
    **kwargs,
):
    """Fetch TAHMO station observations and write a point_obs weather-skills Zarr."""
    client = _client()
    stations = client.stations()
    if stations.empty:
        raise DataError("this TAHMO account has access to no stations")
    sel = select_stations(
        stations, station=station, country=country, bbox=bbox, active_only=active_only
    )

    if kwargs.get("list_stations"):
        _print_stations(sel)
        return None
    if kwargs.get("probe_latest") is not None:
        print(_probe_latest(client, sel if not sel.empty else stations, kwargs["probe_latest"]))
        return None

    if not (station or country or bbox is not None):
        raise UsageError(
            "select stations with --station, --country, and/or --bbox "
            "(run --list-stations to see what this account can access)"
        )
    if not 0 < min_coverage <= 1:
        raise UsageError("--min-coverage must be in (0, 1]")
    if not 0 <= day_start_hour <= 23:
        raise UsageError("--day-start-hour must be between 0 and 23")
    if workers < 1:
        raise UsageError("--workers must be >= 1")
    names = _resolve_variables(variable)
    if resolution == "native":
        derived = [n for n in names if not VARIABLES[n].native]
        if derived:
            raise UsageError(f"{derived} need --resolution hourly or daily")

    # Drop stations installed after the window ends: they cannot have data.
    installed = pd.to_datetime(sel["installed"], errors="coerce")
    sel = sel[~(installed > pd.Timestamp(end_time))]
    if sel.empty:
        raise DataError("no TAHMO stations match the selection for this period")

    daily = resolution == "daily"
    local_days = daily and day_boundary == "local"
    warned: set = set()
    zones = {
        code: station_zone(row["timezone"], code, warned) if local_days else ZoneInfo("UTC")
        for code, row in sel.iterrows()
    }
    windows = {
        code: utc_window(start_time, end_time, zones[code], day_start_hour if daily else 0)
        for code in sel.index
    }

    shortcodes = list(dict.fromkeys(VARIABLES[n].shortcode for n in names))
    tasks = [
        (code, sc, lo, hi)
        for code in sel.index
        for sc in shortcodes
        for lo, hi in chunk_window(*windows[code])
    ]
    print(
        f"Fetching {len(shortcodes)} variable(s) for {len(sel)} station(s) "
        f"{start_time}..{end_time} ({len(tasks)} requests, dataset={dataset})",
        file=sys.stderr,
    )

    parts: dict[tuple[str, str], list[pd.DataFrame]] = {}
    failed: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(client.measurements, code, sc, lo, hi, dataset): (code, sc)
            for code, sc, lo, hi in tasks
        }
        try:
            for fut in as_completed(futures):
                code, sc = futures[fut]
                try:
                    frame = fut.result()
                except AuthError:
                    raise
                except (StationForbidden, DataError) as exc:
                    failed.setdefault(code, str(exc))
                    continue
                parts.setdefault((code, sc), []).append(frame)
        except AuthError:
            pool.shutdown(wait=False, cancel_futures=True)
            raise

    for code, reason in sorted(failed.items()):
        print(f"{code}: DROPPED ({reason})", file=sys.stderr)

    freq = "D" if daily else "h"
    series_by_var: dict[str, dict[str, pd.Series]] = {n: {} for n in names}
    intervals: set[pd.Timedelta] = set()
    for code in sel.index:
        if code in failed:
            continue
        lo, hi = windows[code]
        for sc in shortcodes:
            frames = [f for f in parts.get((code, sc), []) if not f.empty]
            raw = pd.concat(frames, ignore_index=True) if frames else _empty_measurements()
            for name in names:
                spec = VARIABLES[name]
                if spec.shortcode != sc:
                    continue
                samples = clean_samples(raw, max_quality, spec.scale)
                if samples.empty:
                    continue
                if resolution == "native":
                    samples = samples[(samples.index >= lo) & (samples.index < hi)]
                    step = native_interval(pd.DatetimeIndex(samples.index))
                    if step is not None:
                        intervals.add(step)
                    series_by_var[name][code] = samples
                    continue
                # Aggregated bins are right-closed: keep (lo, hi].
                samples = samples[(samples.index > lo) & (samples.index <= hi)]
                series_by_var[name][code] = aggregate(
                    samples,
                    spec.reducer,
                    freq=freq,
                    zone=zones[code] if local_days else None,
                    day_start_hour=day_start_hour if daily else 0,
                    min_coverage=min_coverage,
                    interval=native_interval(pd.DatetimeIndex(samples.index)),
                )

    # Drop station-variables with no surviving values; drop empty variables.
    for name in names:
        series_by_var[name] = {sid: s for sid, s in series_by_var[name].items() if s.notna().any()}
    empty_vars = [n for n in names if not series_by_var[n]]
    if empty_vars and len(empty_vars) < len(names):
        print(
            f"Warning: no station reported usable {empty_vars} for {start_time}..{end_time}; "
            "omitted from the output.",
            file=sys.stderr,
        )
    series_by_var = {n: v for n, v in series_by_var.items() if v}
    if not series_by_var:
        raise DataError(
            f"no usable TAHMO observations for {names} in {start_time}..{end_time} "
            f"(dataset={dataset}, max-quality={max_quality}, min-coverage={min_coverage})"
        )

    if resolution == "native":
        time_index = pd.DatetimeIndex(
            sorted(set().union(*(s.index for per in series_by_var.values() for s in per.values())))
        )
    elif daily:
        time_index = pd.date_range(start_time, end_time, freq="D")
    else:
        time_index = pd.date_range(
            pd.Timestamp(start_time), pd.Timestamp(end_time) + pd.Timedelta(hours=23), freq="h"
        )

    ds = build_dataset(series_by_var, sel, time_index)
    reporting = ds.sizes["station_id"]
    print(f"Wrote {reporting} of {len(sel)} selected station(s) with data", file=sys.stderr)

    stamp_cf_dsg(
        ds,
        {n: _var_attrs(n, resolution, dataset, max_quality) for n in ds.data_vars},
        station_id_long_name="TAHMO station code",
        name_long_name="station name",
    )
    ds["elevation"].attrs.update(
        standard_name="surface_altitude", long_name="station elevation", units="m", positive="up"
    )
    ds["country"].attrs.update(long_name="ISO 3166-1 alpha-2 country code")
    ds["timezone"].attrs.update(long_name="station IANA timezone")
    if daily:
        ds["time"].attrs["long_name"] = (
            "local calendar date of the observation day"
            if local_days
            else "UTC calendar date of the observation day"
        )
    elif resolution == "hourly":
        ds["time"].attrs["long_name"] = "start of the UTC hour (interval is right-closed)"
    else:
        ds["time"].attrs["long_name"] = "end of the logger interval (UTC)"

    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    ds.attrs.update(
        Conventions="CF-1.13",
        featureType="timeSeries",
        title="TAHMO station observations",
        source=f"TAHMO API v2 ({API_BASE_URL}), {dataset} dataset",
        institution="Trans-African Hydro-Meteorological Observatory (TAHMO)",
        references="https://tahmo.org",
        history=f"{now} observations {start_time}..{end_time} resolution={resolution}",
        weather_skills_source="tahmo",
        tahmo_dataset=dataset,
        tahmo_max_quality=np.int8(max_quality),
        tahmo_resolution=resolution,
    )
    if resolution != "native":
        ds.attrs["tahmo_min_coverage"] = float(min_coverage)
    if daily:
        ds.attrs["tahmo_day_boundary"] = day_boundary
        ds.attrs["tahmo_day_start_hour"] = np.int8(day_start_hour)

    verify_cf_dsg(ds)
    apply_write_encoding(
        ds,
        time_units=_TIME_UNITS,
        time_calendar=_TIME_CALENDAR,
        fills={v: np.float64(np.nan) for v in ds.data_vars},
    )

    if daily:
        period = "1 day"
    elif resolution == "hourly":
        period = "1 hour"
    elif len(intervals) == 1:
        period = f"{int(next(iter(intervals)).total_seconds() // 60)} minute"
    else:
        period = None
    if period is None:
        # Mixed logger intervals across stations: amounts per sample cannot be
        # expressed on one axis, so leave precip as amounts and stamp bounds.
        print(
            f"Warning: mixed logger intervals {sorted(str(i) for i in intervals)}; "
            "precip left as per-sample amounts (mm).",
            file=sys.stderr,
        )
        return stamp_data_interval(ds) if ds.sizes["time"] >= 2 else ds
    ds = precip_amounts_to_rates(ds, interval=period)
    if "precip" in ds:
        ds["precip"].attrs["long_name"] = (
            f"{resolution} precipitation rate" if resolution != "native" else "precipitation rate"
        )
    return stamp_data_interval(ds, period=period)


if __name__ == "__main__":
    fetch()
