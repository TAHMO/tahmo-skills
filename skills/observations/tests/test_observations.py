"""Correctness tests for observations (fake TAHMO API; no live network)."""

from __future__ import annotations

import os
from datetime import timedelta
from itertools import pairwise
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history

CREDS = {"TAHMO_API_USERNAME": "user", "TAHMO_API_PASSWORD": "pass"}
COLUMNS = ["time", "quality", "sensor", "station", "value", "variable"]


@pytest.fixture(scope="module")
def mod():
    return load_skill("observations", "fetch")


def _station(code, country, tz, lat=0.0, lon=36.0, status=1, installed="2018-01-01"):
    return {
        "code": code,
        "status": status,
        "installationdate": f"{installed}T00:00:00Z",
        "location": {
            "name": f"Station {code}",
            "countrycode": country,
            "latitude": lat,
            "longitude": lon,
            "elevationmsl": 1500,
            "timezone": tz,
        },
    }


STATIONS = [
    _station("TA00001", "KE", "Africa/Nairobi", lat=-1.3, lon=36.8),
    _station("TA00002", "GH", "Africa/Accra", lat=5.6, lon=-0.2),
    _station("TA00003", "KE", "Africa/Nairobi", lat=0.5, lon=35.3, status=0),
    _station("TE00017", "UG", "Africa/Kampala", lat=0.3, lon=32.6),  # Ecowitt (ARCUS)
]


def five_min(start, end):
    """Interval-end timestamps (start, end] every 5 minutes."""
    t = pd.Timestamp(start) + pd.Timedelta(minutes=5)
    return pd.date_range(t, pd.Timestamp(end), freq="5min")


class FakeAPI:
    """Serves synthetic TAHMO v2 responses; records measurement requests."""

    def __init__(self, series=None, forbidden=(), stations=STATIONS):
        # series: {(station, shortcode): list of rows [time, quality, sensor, value]}
        self.series = series or {}
        self.forbidden = set(forbidden)
        self.stations = stations
        self.requests = []

    def get(self, path, params=None):
        params = params or {}
        if path.startswith("services/assets/v2/stations"):
            return {"data": self.stations}
        station = path.split("/")[4]
        self.requests.append((station, dict(params)))
        if station in self.forbidden:
            raise self.mod.StationForbidden("no authorization to access station measurements")
        rows = self.series.get((station, params.get("variable")), [])
        lo = pd.Timestamp(params["start"]).tz_localize(None) if "start" in params else None
        hi = pd.Timestamp(params["end"]).tz_localize(None) if "end" in params else None
        values = []
        for t, quality, sensor, value in rows:
            ts = pd.Timestamp(t)
            if (lo is None or ts >= lo) and (hi is None or ts <= hi):
                iso = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
                values.append([iso, quality, sensor, station, value, params["variable"]])
        if not values:
            return {"results": [{"statement_id": 0}]}
        return {
            "results": [{"statement_id": 0, "series": [{"columns": COLUMNS, "values": values}]}]
        }


def rows(times, value, quality=1, sensor="S1"):
    vals = value if callable(value) else (lambda _t: value)
    return [[t, quality, sensor, vals(t)] for t in times]


def run(mod, api, tmp_path, *argv):
    api.mod = mod
    out = tmp_path / "out.zarr"
    with (
        patch.dict(os.environ, CREDS),
        patch.object(mod.TahmoClient, "get", lambda self, p, q=None: api.get(p, q)),
    ):
        run_skill(mod.fetch, *argv, "-o", str(out))
    return xr.open_zarr(out, consolidated=True), out


# ----------------------------------------------------------------- pure helpers


def test_chunk_window_covers_range_without_overlap(mod):
    lo = pd.Timestamp("2024-01-01").to_pydatetime()
    hi = pd.Timestamp("2024-12-31T23:59").to_pydatetime()
    chunks = mod.chunk_window(lo, hi)
    assert chunks[0][0] == lo and chunks[-1][1] == hi
    for (a, b), (c, _d) in pairwise(chunks):
        assert b - a <= timedelta(days=mod.CHUNK_DAYS)
        assert c == b + timedelta(seconds=1)


@pytest.mark.parametrize("token", ["KE", "ke", "KEN", "Kenya", "kenya"])
def test_resolve_country_forms(mod, token):
    assert mod.resolve_country(token, {"KE"}) == "KE"


def test_resolve_country_aliases_and_unknown(mod):
    from weather_skills_core import UsageError

    assert mod.resolve_country("Ivory Coast", set()) == "CI"
    assert mod.resolve_country("US", {"US"}) == "US"
    with pytest.raises(UsageError):
        mod.resolve_country("Atlantis", {"KE"})


def test_circular_mean_wraps_north(mod):
    assert mod._circmean(np.array([350.0, 10.0])) == pytest.approx(0.0, abs=1e-9)


def test_clean_samples_prefers_best_quality_sensor(mod):
    raw = pd.DataFrame(
        [
            ["2025-01-01T00:05:00Z", 2.0, 2, "S1"],
            ["2025-01-01T00:05:00Z", 1.0, 1, "S2"],
            ["2025-01-01T00:10:00Z", None, 1, "S1"],  # sensor error
            ["2025-01-01T00:15:00Z", 9.0, 4, "S1"],  # erroneous
        ],
        columns=["time", "value", "quality", "sensor"],
    )
    out = mod.clean_samples(raw, max_quality=2, scale=1.0)
    assert list(out.values) == [1.0]


# ----------------------------------------------------------------- end to end


def test_missing_credentials_exits_2(mod, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in CREDS}
    with patch.dict(os.environ, env, clear=True), pytest.raises(SystemExit) as exc:
        run_skill(mod.fetch, "--station", "TA00002", "--start-time", "2025-01-01",
                  "--end-time", "2025-01-01", "-o", str(tmp_path / "x.zarr"))  # fmt: skip
    assert exc.value.code == 2


def test_daily_utc_station_values_and_metadata(mod, tmp_path):
    day1 = five_min("2025-01-01", "2025-01-02")
    day2 = five_min("2025-01-02", "2025-01-03")
    times = day1.append(day2)
    api = FakeAPI(
        {
            ("TA00002", "pr"): rows(times, 0.1),  # 288 * 0.1 = 28.8 mm/day
            ("TA00002", "te"): rows(times, lambda t: 20.0 + t.hour),
            ("TA00002", "rh"): rows(times, 0.5),
        }
    )
    ds, out = run(
        mod, api, tmp_path, "--station", "TA00002", "--start-time", "2025-01-01",
        "--end-time", "2025-01-02", "-v", "precip", "-v", "temperature", "-v", "tmax",
        "-v", "tmin", "-v", "humidity",
    )  # fmt: skip

    assert ds.sizes == {"time": 2, "station_id": 1}
    np.testing.assert_allclose(ds["precip"].values.ravel(), [28.8, 28.8])
    assert ds["precip"].attrs["units"] == "mm day-1"
    assert ds["precip"].attrs["standard_name"] == "lwe_precipitation_rate"
    assert ds["precip"].attrs["cell_methods"] == "time: mean"
    np.testing.assert_allclose(ds["humidity"].values.ravel(), [50.0, 50.0])
    assert ds["humidity"].attrs["units"] == "%"
    # Right-closed bins: 00:00 of day 2 (hour 0) belongs to day 1.
    assert float(ds["tmax"].isel(time=0, station_id=0)) == pytest.approx(43.0)
    assert float(ds["tmin"].isel(time=0, station_id=0)) == pytest.approx(20.0)
    assert ds["tmax"].attrs["cell_methods"] == "time: maximum"
    assert ds["temperature"].attrs["units"] == "degree_Celsius"
    assert ds["station_id"].attrs["cf_role"] == "timeseries_id"
    assert str(ds["country"].values[0]) == "GH"
    assert ds.attrs["featureType"] == "timeSeries"
    assert ds.attrs["weather_skills_source"] == "tahmo"
    assert ds["precip"].attrs["data_interval"] == "1 day"
    assert load_history(out)[-1]["skill"] == "observations"
    # One request per (station, shortcode): te is shared by temperature/tmax/tmin.
    assert sorted(p["variable"] for _s, p in api.requests) == ["pr", "rh", "te"]


def test_quality_and_coverage_masking(mod, tmp_path):
    day1 = five_min("2025-01-01", "2025-01-02")
    day2 = five_min("2025-01-02", "2025-01-03")[:100]  # 100/288 < 0.8 coverage
    suspect = [[t, 2, "S1", 5.0] for t in five_min("2025-01-03", "2025-01-04")]
    api = FakeAPI({("TA00002", "pr"): rows(day1, 0.1) + rows(day2, 0.1) + suspect})

    ds, _ = run(mod, api, tmp_path, "--station", "TA00002", "--start-time", "2025-01-01",
                "--end-time", "2025-01-03", "-v", "precip")  # fmt: skip
    vals = ds["precip"].values.ravel()
    assert vals[0] == pytest.approx(28.8)
    assert np.isnan(vals[1])  # too sparse, not a fake low total
    assert np.isnan(vals[2])  # suspect data excluded by default

    ds2, _ = run(mod, api, tmp_path, "--station", "TA00002", "--start-time", "2025-01-01",
                 "--end-time", "2025-01-03", "-v", "precip", "--max-quality", "2",
                 "--min-coverage", "0.3")  # fmt: skip
    vals2 = ds2["precip"].values.ravel()
    assert vals2[1] == pytest.approx(10.0)
    assert vals2[2] == pytest.approx(288 * 5.0)


def test_local_day_boundary_shifts_bins(mod, tmp_path):
    # Nairobi is UTC+3: local day 2025-01-01 is UTC 2024-12-31T21:00 .. 2025-01-01T21:00.
    local_day = five_min("2024-12-31T21:00", "2025-01-01T21:00")
    api = FakeAPI({("TA00001", "pr"): rows(local_day, 0.1)})

    ds, _ = run(mod, api, tmp_path, "--station", "TA00001", "--start-time", "2025-01-01",
                "--end-time", "2025-01-01", "-v", "precip")  # fmt: skip
    assert float(ds["precip"].isel(time=0, station_id=0)) == pytest.approx(28.8)
    assert api.requests[0][1]["start"] == "2024-12-31T21:00:00Z"
    assert "local" in ds["time"].attrs["long_name"]

    ds_utc, _ = run(mod, api, tmp_path, "--station", "TA00001", "--start-time", "2025-01-01",
                    "--end-time", "2025-01-01", "-v", "precip", "--day-boundary", "utc",
                    "--min-coverage", "0.5")  # fmt: skip
    assert float(ds_utc["precip"].isel(time=0, station_id=0)) == pytest.approx(0.1 * 252)


def test_day_start_hour(mod, tmp_path):
    times = five_min("2025-01-01T00:00", "2025-01-03T00:00")
    api = FakeAPI({("TA00002", "pr"): rows(times, lambda t: 1.0 if t.hour < 9 else 0.0)})
    ds, _ = run(mod, api, tmp_path, "--station", "TA00002", "--start-time", "2025-01-01",
                "--end-time", "2025-01-01", "-v", "precip", "--day-start-hour", "9")  # fmt: skip
    # 09:00 Jan 1 -> 09:00 Jan 2 contains the 00:05..09:00 block of Jan 2 (108 samples).
    assert float(ds["precip"].isel(time=0, station_id=0)) == pytest.approx(108.0)


def test_hourly_and_native(mod, tmp_path):
    times = five_min("2025-01-01", "2025-01-02")
    api = FakeAPI({("TA00002", "pr"): rows(times, 0.1), ("TA00002", "ws"): rows(times, 2.0)})
    ds, _ = run(mod, api, tmp_path, "--station", "TA00002", "--start-time", "2025-01-01",
                "--end-time", "2025-01-01", "-v", "precip", "--resolution", "hourly")  # fmt: skip
    assert ds.sizes["time"] == 24
    np.testing.assert_allclose(ds["precip"].values.ravel(), 1.2 * 24)  # 1.2 mm/h in mm day-1
    assert ds["precip"].attrs["data_interval"] == "1 hour"

    ds_n, _ = run(mod, api, tmp_path, "--station", "TA00002", "--start-time", "2025-01-01",
                  "--end-time", "2025-01-01", "-v", "wind_speed", "--resolution", "native")  # fmt: skip
    assert ds_n.sizes["time"] == 287  # 00:05..23:55 on the calendar day
    assert ds_n["wind_speed"].attrs["data_interval"] == "5 minute"
    assert "cell_methods" not in ds_n["wind_speed"].attrs


def test_native_rejects_derived_variables(mod, tmp_path):
    with pytest.raises(SystemExit) as exc:
        run(mod, FakeAPI(), tmp_path, "--station", "TA00002", "--start-time", "2025-01-01",
            "--end-time", "2025-01-01", "-v", "tmax", "--resolution", "native")  # fmt: skip
    assert exc.value.code == 2


def test_forbidden_station_dropped_others_kept(mod, tmp_path, capsys):
    times = five_min("2025-01-01", "2025-01-02")
    api = FakeAPI({("TA00001", "pr"): rows(times, 0.1)}, forbidden={"TA00003"})
    ds, _ = run(mod, api, tmp_path, "--country", "Kenya", "--start-time", "2025-01-01",
                "--end-time", "2025-01-01", "-v", "precip", "--day-boundary", "utc")  # fmt: skip
    assert list(ds["station_id"].values) == ["TA00001"]
    assert "TA00003: DROPPED" in capsys.readouterr().err


def test_selectors_intersect_and_active_only(mod):
    stations = mod.TahmoClient.__new__(mod.TahmoClient)
    api = FakeAPI()
    with patch.object(mod.TahmoClient, "get", lambda self, p, q=None: api.get(p, q)):
        table = stations.stations()
    kenya = mod.select_stations(table, station=None, country=["KEN"], bbox=None, active_only=False)
    assert list(kenya.index) == ["TA00001", "TA00003"]
    active = mod.select_stations(table, station=None, country=["KE"], bbox=None, active_only=True)
    assert list(active.index) == ["TA00001"]
    boxed = mod.select_stations(
        table, station=None, country=["KE"], bbox=(1.0, 35.0, 0.0, 36.0), active_only=False
    )
    assert list(boxed.index) == ["TA00003"]


def test_list_stations_prints_csv_without_output(mod, capsys):
    api = FakeAPI()
    with (
        patch.dict(os.environ, CREDS),
        patch.object(mod.TahmoClient, "get", lambda self, p, q=None: api.get(p, q)),
    ):
        run_skill(mod.fetch, "--list-stations", "--country", "GH")
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0].startswith("code,name,country,latitude,longitude")
    assert len(out) == 2 and out[1].startswith("TA00002,")


def test_probe_latest(mod, capsys):
    api = FakeAPI({("TA00002", "pr"): rows(five_min("2025-03-04", "2025-03-05T06:00"), 0.0)})
    api.mod = mod
    with (
        patch.dict(os.environ, CREDS),
        patch.object(mod.TahmoClient, "get", lambda self, p, q=None: api.get(p, q)),
    ):
        run_skill(mod.fetch, "--probe-latest", "--station", "TA00002")
    assert capsys.readouterr().out.strip() == "2025-03-05"
    assert "start" not in api.requests[0][1]  # last-24h default window, cheap


def test_http_retries_then_auth_error(mod, monkeypatch):
    class Resp:
        def __init__(self, code, body=None):
            self.status_code = code
            self._body = body or {}
            self.headers = {}

        def json(self):
            return self._body

    calls = []
    replies = [Resp(503), Resp(429), Resp(200, {"data": []})]

    def fake_get(self, url, params=None, timeout=None):
        calls.append(url)
        return replies.pop(0)

    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(mod.requests.Session, "get", fake_get)
    client = mod.TahmoClient("u", "p")
    assert client.get("services/assets/v2/stations") == {"data": []}
    assert len(calls) == 3

    replies[:] = [Resp(401)]
    with pytest.raises(mod.AuthError):
        client.get("services/assets/v2/stations")

    replies[:] = [Resp(422, {"error": {"message": "period too large"}})]
    with pytest.raises(mod.DataError, match="period too large"):
        client.get("services/measurements/v2/stations/TA1/measurements/controlled")


# ----------------------------------------------------------------- Ecowitt (TE) rain


def running_total(times, rain_per_reading, start=0.0, reset_after=None):
    """Ecowitt-style event counter: cumulative rain, optionally reset to 0 at an index."""
    total, values = start, []
    for i, (_t, rain) in enumerate(zip(times, rain_per_reading, strict=True)):
        if reset_after is not None and i == reset_after:
            total = 0.0
        total += rain
        values.append(round(total, 4))
    return [[t, 1, "S9", v] for t, v in zip(times, values, strict=True)]


def test_looks_like_counter(mod):
    rising = np.concatenate([np.full(10, 1.0), np.linspace(1, 20, 40), np.full(20, 20.0), [0, 0]])
    assert mod.looks_like_counter(rising) is True
    bursty = np.tile([0.0, 0.2, 0.4, 0.1, 0.0, 0.3], 20)  # ATMOS 41 style: falls after rain
    assert mod.looks_like_counter(bursty) is False
    assert mod.looks_like_counter([0.0, 0.2, 0.4]) is None  # too little rain to tell


def test_counter_to_increments_rises_holds_and_resets(mod):
    times = five_min("2025-01-01", "2025-01-01T00:35")
    raw = pd.DataFrame(
        [[t.strftime("%Y-%m-%dT%H:%M:%SZ"), v, 1, "S9"]
         for t, v in zip(times, [2.0, 2.0, 2.5, 3.0, 0.0, 0.4, 0.3], strict=True)],
        columns=["time", "value", "quality", "sensor"],
    )  # fmt: skip
    out, unchanged = mod.counter_to_increments(raw, max_quality=1)
    assert unchanged == []
    # First reading is the baseline; a fall to 0 is a reset; a fall to 0.3 is a
    # reset with 0.3 mm of new rain.
    np.testing.assert_allclose(out["value"].to_numpy(), [0.0, 0.5, 0.5, 0.0, 0.4, 0.3])


def test_te_station_daily_rain_from_running_total(mod, tmp_path, capsys):
    baseline = five_min("2024-12-31", "2025-01-01")
    day1 = five_min("2025-01-01", "2025-01-02")
    day2 = five_min("2025-01-02", "2025-01-03")
    times = baseline.append(day1).append(day2)
    rain = np.zeros(len(times))
    rain[: len(baseline)][100:150] = 0.1  # 5 mm on the baseline day (counter carries it)
    rain[len(baseline) : len(baseline) + len(day1)][60:120] = 0.2  # 12 mm on day 1
    rain[len(baseline) + len(day1) :][10:40] = 0.1  # 3 mm on day 2, after a reset
    counter = running_total(times, rain, reset_after=len(baseline) + len(day1))
    api = FakeAPI({("TE00017", "pr"): counter})

    ds, _ = run(mod, api, tmp_path, "--station", "TE00017", "--start-time", "2025-01-01",
                "--end-time", "2025-01-02", "-v", "precip", "--day-boundary", "utc")  # fmt: skip
    np.testing.assert_allclose(ds["precip"].values.ravel(), [12.0, 3.0], atol=1e-6)
    assert ds.attrs["tahmo_rain_counter_stations"] == "TE00017"
    assert "running event total" in ds["precip"].attrs["comment"]
    # One extra day is fetched as the baseline for the first reading.
    assert api.requests[0][1]["start"] == "2024-12-31T00:00:00Z"
    assert "Converted Ecowitt running-total rain" in capsys.readouterr().err


def test_te_coverage_counts_hours_with_a_reading(mod, tmp_path):
    # Readings every 5 min in the morning, every 30 min after noon: 168 of 288
    # samples, but every hour has a reading, so no rain is lost.
    times = five_min("2024-12-31T23:00", "2025-01-01T12:00").append(
        pd.date_range("2025-01-01T12:30", "2025-01-02T00:00", freq="30min")
    )
    rain = np.where((times > pd.Timestamp("2025-01-01T13:00")) & (times <= pd.Timestamp(
        "2025-01-01T18:00")), 0.5, 0.0)  # fmt: skip
    rain[5:60] = 0.05  # morning drizzle so the series has enough rises to judge
    api = FakeAPI({("TE00017", "pr"): running_total(times, rain, start=1.0)})

    ds, _ = run(mod, api, tmp_path, "--station", "TE00017", "--start-time", "2025-01-01",
                "--end-time", "2025-01-01", "-v", "precip", "--day-boundary", "utc")  # fmt: skip
    expected = rain[(times > pd.Timestamp("2025-01-01")) & (times <= pd.Timestamp("2025-01-02"))]
    assert float(ds["precip"].values.ravel()[0]) == pytest.approx(expected.sum())


def test_te_station_with_per_interval_rain_is_left_alone(mod, tmp_path, capsys):
    times = five_min("2025-01-01", "2025-01-02")
    pattern = np.tile([0.0, 0.2, 0.4, 0.1, 0.0, 0.3], len(times) // 6)
    api = FakeAPI({("TE00017", "pr"): [[t, 1, "S9", v] for t, v in zip(times, pattern,
                                                                       strict=True)]})  # fmt: skip
    ds, _ = run(mod, api, tmp_path, "--station", "TE00017", "--start-time", "2025-01-01",
                "--end-time", "2025-01-01", "-v", "precip", "--day-boundary", "utc")  # fmt: skip
    assert float(ds["precip"].values.ravel()[0]) == pytest.approx(pattern.sum())
    assert "tahmo_rain_counter_stations" not in ds.attrs
    assert "does not behave like a running total" in capsys.readouterr().err


def test_te_station_other_variables_unchanged(mod, tmp_path):
    times = five_min("2025-01-01", "2025-01-02")
    api = FakeAPI({("TE00017", "te"): rows(times, 21.0)})
    ds, _ = run(mod, api, tmp_path, "--station", "TE00017", "--start-time", "2025-01-01",
                "--end-time", "2025-01-01", "-v", "temperature", "--day-boundary", "utc")  # fmt: skip
    assert float(ds["temperature"].values.ravel()[0]) == pytest.approx(21.0)
    assert api.requests[0][1]["start"] == "2025-01-01T00:00:00Z"  # no baseline day
