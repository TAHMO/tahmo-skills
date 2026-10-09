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
"""Remove station rainfall that disagrees with IMERG satellite estimates.

Reads a daily point_obs Zarr (e.g. from the observations skill), samples NASA
IMERG Early at each station from the Dynamical point API, compares daily
totals, sets disagreeing station-days to NaN, and records why in a flag variable.
"""

from __future__ import annotations

import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd
import requests
from weather_skills_core import DataError, Dataset, UsageError, weather_skill
from weather_skills_core.units import parse_aggregation_period, units_equal

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.1"

DYNAMICAL_URL = "https://api.dynamical.org/v1/analyses"
IMERG_PRODUCT = "nasa-imerg-analysis-early"
IMERG_VARIABLE = "precipitation_surface"  # kg m-2 s-1 == mm s-1
IMERG_STEP = pd.Timedelta(minutes=30)
STEPS_PER_DAY = 48
MAX_QUERIES_PER_REQUEST = 10
MAX_WINDOW_DAYS = 31
HTTP_TIMEOUT = 120
MAX_ATTEMPTS = 4
BACKOFF_BASE_S = 2.0
DEFAULT_WORKERS = 4

DEFAULT_ABS_THRESHOLD = 10.0
DEFAULT_RATIO_THRESHOLD = 2.0
DEFAULT_WET_THRESHOLD = 1.0

# CF flag bits on the satellite_qc variable. 0 means the day agrees.
FLAG_ABS_DIFF = 1
FLAG_RATIO = 2
FLAG_WET_DRY = 4
FLAG_NO_SATELLITE = 8
FLAG_MEANINGS = "abs_difference ratio wet_dry_mismatch no_satellite"
FLAG_FILL = np.int8(-1)  # no station observation that day


# ------------------------------------------------------------------ IMERG


def _post(queries: list[dict]) -> list[dict]:
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(DYNAMICAL_URL, json={"queries": queries}, timeout=HTTP_TIMEOUT)
            if resp.status_code == 200:
                results = resp.json().get("results")
                if isinstance(results, list) and len(results) == len(queries):
                    return results
                last_error = "unexpected response shape"
            elif resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
            else:
                raise DataError(
                    f"Dynamical IMERG request rejected: HTTP {resp.status_code} {resp.text[:300]}"
                )
        except (requests.ConnectionError, requests.Timeout, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_BASE_S * 2 ** (attempt - 1) + random.uniform(0, 0.5))
    raise DataError(f"Dynamical IMERG request failed after {MAX_ATTEMPTS} attempts: {last_error}")


def _windows(lo: pd.Timestamp, hi: pd.Timestamp):
    """End-exclusive UTC windows of at most MAX_WINDOW_DAYS covering [lo, hi)."""
    cur = lo
    while cur < hi:
        nxt = min(cur + pd.Timedelta(days=MAX_WINDOW_DAYS), hi)
        yield cur, nxt
        cur = nxt


def fetch_imerg(points: pd.DataFrame, lo: pd.Timestamp, hi: pd.Timestamp, workers: int):
    """Half-hourly IMERG Early (mm per half hour) at each point over UTC [lo, hi).

    ``points`` is indexed by station id with latitude/longitude columns.
    Returns ``{station_id: Series}`` indexed by UTC-naive half-hour start time.
    """
    batches = []
    ids = list(points.index)
    for w_lo, w_hi in _windows(lo, hi):
        for i in range(0, len(ids), MAX_QUERIES_PER_REQUEST):
            batches.append(
                [
                    {
                        "queryId": sid,
                        "dataProductId": IMERG_PRODUCT,
                        "location": {
                            "latitude": float(points.at[sid, "latitude"]),
                            "longitude": float(points.at[sid, "longitude"]),
                        },
                        "startTime": w_lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "endTime": w_hi.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "variables": [IMERG_VARIABLE],
                    }
                    for sid in ids[i : i + MAX_QUERIES_PER_REQUEST]
                ]
            )

    parts: dict[str, list[pd.Series]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_post, batch) for batch in batches]
        for fut in as_completed(futures):
            for result in fut.result():
                sid = result.get("queryId")
                times = result.get("analysisTimes") or []
                values = (result.get("data") or {}).get(IMERG_VARIABLE) or []
                if len(times) != len(values):
                    raise DataError(f"{sid}: IMERG times and values differ in length")
                if not times:
                    continue
                index = pd.to_datetime(times, utc=True).tz_convert(None)
                mm = pd.to_numeric(pd.Series(values), errors="coerce").to_numpy()
                mm = mm * IMERG_STEP.total_seconds()
                parts.setdefault(sid, []).append(pd.Series(mm, index=index))
    out = {}
    for sid, series in parts.items():
        s = pd.concat(series).sort_index()
        out[sid] = s[~s.index.duplicated(keep="first")]
    return out


def imerg_daily(
    halfhourly: pd.Series,
    days: pd.DatetimeIndex,
    zone: ZoneInfo,
    day_start_hour: int,
) -> pd.Series:
    """Sum half-hours into the same days the observations use.

    An IMERG value is labelled with the start of its half hour, so it belongs to
    the day whose window [start, start + 1 day) contains that start. Days
    missing any of their 48 half-hours are NaN.
    """
    if halfhourly.empty:
        return pd.Series(np.nan, index=days)
    local = halfhourly.index.tz_localize("UTC").tz_convert(zone).tz_localize(None)
    keys = (local - pd.Timedelta(hours=day_start_hour)).floor("D")
    grouped = pd.Series(halfhourly.to_numpy(), index=keys).groupby(level=0)
    totals = grouped.sum(min_count=1).where(grouped.count() >= STEPS_PER_DAY)
    return totals.reindex(days)


# ------------------------------------------------------------------ comparison


def compare(obs: np.ndarray, sat: np.ndarray, abs_thr: float, ratio_thr: float, wet_thr: float):
    """Bit flags per cell: which rules each observation fails (0 = agrees).

    Rules (from the TAHMO clean-precip pipeline):
    - |obs - sat| >= abs_thr mm
    - max/min >= ratio_thr, when both are > 0
    - wet/dry mismatch: one side > wet_thr mm and the other <= wet_thr mm
    """
    flags = np.zeros(obs.shape, dtype=np.int8)
    has_obs = ~np.isnan(obs)
    has_sat = ~np.isnan(sat)
    both = has_obs & has_sat
    with np.errstate(invalid="ignore", divide="ignore"):
        lo = np.fmin(obs, sat)
        hi = np.fmax(obs, sat)
        ratio = np.where(both & (lo > 0), hi / lo, np.nan)
        flags[both & (np.abs(obs - sat) >= abs_thr)] |= FLAG_ABS_DIFF
        flags[both & (ratio >= ratio_thr)] |= FLAG_RATIO
        wet_obs, wet_sat = obs > wet_thr, sat > wet_thr
        flags[both & (wet_obs != wet_sat)] |= FLAG_WET_DRY
    flags[has_obs & ~has_sat] |= FLAG_NO_SATELLITE
    flags[~has_obs] = FLAG_FILL
    return flags


# ------------------------------------------------------------------ input checks


def _station_dim(ds) -> str:
    for dim in ("station_id", "point_id"):
        if dim in ds.dims:
            return dim
    raise UsageError(
        f"input has no station dimension (station_id/point_id); dims: {dict(ds.sizes)}"
    )


def _require_daily(da) -> None:
    interval = da.attrs.get("aggregation_period") or da.attrs.get("data_interval")
    try:
        daily = parse_aggregation_period(str(interval)).to("day").magnitude == 1
    except Exception:  # noqa: BLE001 - missing or unparseable interval
        daily = False
    if not daily:
        raise UsageError(
            f"{da.name!r} must be daily (data_interval '1 day'); got {interval!r}. "
            "Fetch with observations --resolution daily."
        )


def _day_definition(ds, station_dim):
    """Per-station (zone, day_start_hour) matching how the input days were binned."""
    boundary = ds.attrs.get("tahmo_day_boundary")
    start_hour = int(ds.attrs.get("tahmo_day_start_hour", 0))
    if boundary is None:
        print(
            "Warning: input has no tahmo_day_boundary attr; assuming UTC days starting at 00:00.",
            file=sys.stderr,
        )
        boundary = "utc"
    zones = {}
    tz_names = ds["timezone"].values if "timezone" in ds.coords else [""] * ds.sizes[station_dim]
    for sid, tz in zip(ds[station_dim].values, tz_names, strict=True):
        zone = ZoneInfo("UTC")
        if boundary == "local" and tz:
            try:
                zone = ZoneInfo(str(tz))
            except (ZoneInfoNotFoundError, ValueError):
                print(f"{sid}: unknown timezone {tz!r}; using UTC days", file=sys.stderr)
        zones[str(sid)] = zone
    return boundary, start_hour, zones


# ------------------------------------------------------------------ skill


@weather_skill(name="observations-satellite-qc", version=_SKILL_VERSION)
@weather_skill.argument(
    "-i",
    "--input",
    type=Dataset("point_obs"),
    required=True,
    help="Daily station Zarr, e.g. from the observations skill.",
)
@weather_skill.argument(
    "--variable",
    "-v",
    default="precip",
    help="Daily precipitation variable to check (default precip, in mm day-1).",
)
@weather_skill.argument(
    "--abs-threshold",
    type=float,
    default=DEFAULT_ABS_THRESHOLD,
    help=f"Flag when |station - IMERG| >= this many mm (default {DEFAULT_ABS_THRESHOLD}).",
)
@weather_skill.argument(
    "--ratio-threshold",
    type=float,
    default=DEFAULT_RATIO_THRESHOLD,
    help=(
        "Flag when the larger of the two is >= this many times the smaller, both > 0 "
        f"(default {DEFAULT_RATIO_THRESHOLD})."
    ),
)
@weather_skill.argument(
    "--wet-threshold",
    type=float,
    default=DEFAULT_WET_THRESHOLD,
    help=(
        "Wet-day threshold in mm; flag when one side is above it and the other is not "
        f"(default {DEFAULT_WET_THRESHOLD})."
    ),
)
@weather_skill.argument(
    "--keep-unverified",
    action="store_true",
    help="Keep station-days that have no complete IMERG day to compare against (default: remove).",
)
@weather_skill.argument(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    help=f"Concurrent Dynamical API requests (default {DEFAULT_WORKERS}).",
)
def satellite_qc(
    ds,
    variable,
    abs_threshold,
    ratio_threshold,
    wet_threshold,
    keep_unverified,
    workers,
    **kwargs,
):
    """Remove daily station rainfall that disagrees with IMERG Early satellite estimates."""
    if abs_threshold <= 0 or ratio_threshold <= 1 or wet_threshold < 0:
        raise UsageError("need --abs-threshold > 0, --ratio-threshold > 1, --wet-threshold >= 0")
    if workers < 1:
        raise UsageError("--workers must be >= 1")
    if variable not in ds.data_vars:
        raise UsageError(f"variable {variable!r} not in input; has {list(ds.data_vars)}")

    ds = ds.pint.dequantify() if hasattr(ds, "pint") else ds
    station_dim = _station_dim(ds)
    da = ds[variable]
    _require_daily(da)
    units = da.attrs.get("units", "")
    if not units_equal(units, "mm day-1"):
        raise UsageError(f"{variable!r} must be in mm day-1 (daily totals); got {units!r}")
    if "latitude" not in ds.coords or "longitude" not in ds.coords:
        raise UsageError("input needs latitude/longitude coordinates per station")

    days = pd.DatetimeIndex(ds["time"].values).normalize()
    if days.has_duplicates:
        raise UsageError("input time axis has duplicate days")
    boundary, start_hour, zones = _day_definition(ds, station_dim)

    sids = [str(s) for s in ds[station_dim].values]
    points = pd.DataFrame(
        {
            "latitude": ds["latitude"].values.astype(float),
            "longitude": ds["longitude"].values.astype(float),
        },
        index=sids,
    )
    # UTC span covering every station's day windows (+/- 1 day for any offset).
    lo = days.min() - pd.Timedelta(days=1)
    hi = days.max() + pd.Timedelta(days=2)
    print(
        f"Sampling IMERG Early at {len(sids)} station(s) for {days.min().date()}.."
        f"{days.max().date()} ({boundary} days, start {start_hour:02d}:00)",
        file=sys.stderr,
    )
    halfhourly = fetch_imerg(points, lo, hi, workers)

    sat = np.full((len(days), len(sids)), np.nan)
    for j, sid in enumerate(sids):
        series = halfhourly.get(sid)
        if series is not None:
            sat[:, j] = imerg_daily(series, days, zones[sid], start_hour).to_numpy()

    obs = da.transpose("time", station_dim).values.astype(float)
    flags = compare(obs, sat, abs_threshold, ratio_threshold, wet_threshold)
    reject = flags > 0
    if keep_unverified:
        reject &= flags != FLAG_NO_SATELLITE

    out = ds.copy()
    cleaned = np.where(reject, np.nan, obs)
    out[variable] = (("time", station_dim), cleaned, dict(da.attrs))
    out["satellite_precip"] = (
        ("time", station_dim),
        sat,
        {
            "units": "mm day-1",
            "standard_name": "lwe_precipitation_rate",
            "long_name": "IMERG Early daily precipitation at the station",
            "coordinates": "latitude longitude time",
            "source": f"Dynamical {IMERG_PRODUCT} point API (nearest grid cell)",
            "data_interval": "1 day",
        },
    )
    out["satellite_qc"] = (
        ("time", station_dim),
        flags,
        {
            "standard_name": "quality_flag",
            "long_name": f"agreement of {variable} with IMERG Early (0 = agrees)",
            "flag_masks": np.array(
                [FLAG_ABS_DIFF, FLAG_RATIO, FLAG_WET_DRY, FLAG_NO_SATELLITE], dtype=np.int8
            ),
            "flag_meanings": FLAG_MEANINGS,
            "coordinates": "latitude longitude time",
        },
    )
    out["satellite_qc"].encoding["_FillValue"] = FLAG_FILL
    out[variable].attrs["ancillary_variables"] = "satellite_qc satellite_precip"

    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    history = out.attrs.get("history", "")
    out.attrs.update(
        history=f"{history}\n{now} observations-satellite-qc {variable} vs {IMERG_PRODUCT}".strip(),
        satellite_qc_product=IMERG_PRODUCT,
        satellite_qc_abs_threshold=float(abs_threshold),
        satellite_qc_ratio_threshold=float(ratio_threshold),
        satellite_qc_wet_threshold=float(wet_threshold),
        satellite_qc_keep_unverified=int(bool(keep_unverified)),
    )

    _report(flags, reject, sids, variable, keep_unverified)
    return out


def _report(flags, reject, sids, variable, keep_unverified) -> None:
    has_obs = flags != FLAG_FILL
    n_obs = int(has_obs.sum())
    if n_obs == 0:
        raise DataError(f"input has no {variable} observations to check")
    n_removed = int(reject.sum())
    counts = {
        "abs_difference": int(((flags & FLAG_ABS_DIFF) > 0)[has_obs].sum()),
        "ratio": int(((flags & FLAG_RATIO) > 0)[has_obs].sum()),
        "wet_dry_mismatch": int(((flags & FLAG_WET_DRY) > 0)[has_obs].sum()),
        "no_satellite": int(((flags & FLAG_NO_SATELLITE) > 0)[has_obs].sum()),
    }
    print(
        f"Checked {n_obs} station-days: kept {n_obs - n_removed}, removed {n_removed} "
        f"({100 * n_removed / n_obs:.1f}%). Rule hits: "
        + ", ".join(f"{k}={v}" for k, v in counts.items())
        + ("" if not keep_unverified else " (unverified days kept)"),
        file=sys.stderr,
    )
    per_station = pd.DataFrame(
        {
            "station": sids,
            "days": has_obs.sum(axis=0),
            "removed": reject.sum(axis=0),
        }
    )
    per_station = per_station[per_station["days"] > 0]
    per_station["share"] = per_station["removed"] / per_station["days"]
    worst = per_station.sort_values(["share", "removed"], ascending=False).head(10)
    worst = worst[worst["removed"] > 0]
    if not worst.empty:
        print("Stations with the most removed days:", file=sys.stderr)
        for row in worst.itertuples(index=False):
            print(
                f"  {row.station}: {row.removed}/{row.days} ({100 * row.share:.0f}%)",
                file=sys.stderr,
            )


if __name__ == "__main__":
    satellite_qc()
