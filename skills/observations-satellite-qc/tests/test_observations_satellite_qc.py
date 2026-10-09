"""Correctness tests for observations-satellite-qc (fake Dynamical API; no live network)."""

from __future__ import annotations

import json
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history

MM_PER_HALF_HOUR = 1 / 1800  # kg m-2 s-1 that sums to 1 mm over a half hour


@pytest.fixture(scope="module")
def mod():
    return load_skill("observations-satellite-qc", "observations_satellite_qc")


def obs_zarr(path, precip, *, tz="Africa/Accra", boundary="local", start_hour=0, interval="1 day"):
    """Daily point_obs Zarr shaped like the observations skill's output (one station)."""
    days = pd.date_range("2025-01-01", periods=len(precip), freq="D")
    ds = xr.Dataset(
        {
            "precip": (("time", "station_id"), np.array(precip, dtype=float)[:, None]),
            "temperature": (("time", "station_id"), np.full((len(precip), 1), 25.0)),
        },
        coords={
            "time": days,
            "station_id": ["TA00001"],
            "latitude": ("station_id", [5.6]),
            "longitude": ("station_id", [-0.2]),
            "timezone": ("station_id", [tz]),
        },
        attrs={"tahmo_day_boundary": boundary, "tahmo_day_start_hour": start_hour},
    )
    ds["precip"].attrs.update(
        units="mm day-1", standard_name="lwe_precipitation_rate", data_interval=interval
    )
    ds["temperature"].attrs.update(
        units="degree_Celsius", standard_name="air_temperature", data_interval=interval
    )
    ds.to_zarr(path, mode="w", consolidated=True)
    return path


class FakeDynamical:
    """Serves half-hourly IMERG with a fixed daily total per UTC day."""

    def __init__(self, daily_mm: dict[str, float], status=None):
        self.daily_mm = daily_mm
        self.status = list(status or [])
        self.bodies = []

    def __call__(self, url, json=None, timeout=None):
        self.bodies.append(json)
        code = self.status.pop(0) if self.status else 200
        results = []
        for q in json["queries"]:
            times = pd.date_range(q["startTime"], q["endTime"], freq="30min", inclusive="left")
            values = [
                self.daily_mm.get(t.strftime("%Y-%m-%d"), 0.0) / 48 * MM_PER_HALF_HOUR
                for t in times
            ]
            results.append(
                {
                    "queryId": q["queryId"],
                    "analysisTimes": [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in times],
                    "data": {"precipitation_surface": values},
                }
            )
        return _Resp(code, {"results": results})


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body
        self.text = json.dumps(body)[:100]

    def json(self):
        return self._body


def run(mod, monkeypatch, tmp_path, inp, fake, *argv):
    monkeypatch.setattr(mod.requests, "post", fake)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    out = tmp_path / "qc.zarr"
    run_skill(mod.satellite_qc, "-i", str(inp), "-o", str(out), *argv)
    return xr.open_zarr(out, consolidated=True), out


# ----------------------------------------------------------------- rules


def test_compare_rules(mod):
    obs = np.array([5.0, 20.0, 0.7, 2.0, 0.0, 0.5, 3.0, np.nan])
    sat = np.array([5.5, 5.0, 0.3, 0.0, 0.0, 0.0, np.nan, 1.0])
    flags = mod.compare(obs, sat, 10.0, 2.0, 1.0)
    assert flags[0] == 0  # close agreement
    assert flags[1] == mod.FLAG_ABS_DIFF | mod.FLAG_RATIO  # both wet, so no wet/dry flag
    assert flags[2] == mod.FLAG_RATIO  # both > 0, ratio 2.33
    assert flags[3] == mod.FLAG_WET_DRY  # ratio undefined when one side is 0
    assert flags[4] == 0  # both dry
    assert flags[5] == 0  # 0.5 vs 0: below wet threshold, ratio undefined
    assert flags[6] == mod.FLAG_NO_SATELLITE
    assert flags[7] == mod.FLAG_FILL


def test_imerg_daily_follows_local_day_and_start_hour(mod):
    # 1 mm in each half hour from 2025-01-01T00:00Z for 3 days.
    idx = pd.date_range("2024-12-31T00:00", "2025-01-03T23:30", freq="30min")
    series = pd.Series(1.0, index=idx)
    series[idx < pd.Timestamp("2025-01-01")] = 0.0
    days = pd.DatetimeIndex(["2025-01-01", "2025-01-02"])

    utc = mod.imerg_daily(series, days, ZoneInfo("UTC"), 0)
    np.testing.assert_allclose(utc, [48.0, 48.0])
    # Nairobi (UTC+3): local 2025-01-01 starts 2024-12-31T21:00Z, so 6 dry half-hours.
    nairobi = mod.imerg_daily(series, days, ZoneInfo("Africa/Nairobi"), 0)
    np.testing.assert_allclose(nairobi, [42.0, 48.0])
    # 09:00-09:00 UTC days.
    nine = mod.imerg_daily(series, days, ZoneInfo("UTC"), 9)
    np.testing.assert_allclose(nine, [48.0, 48.0])


def test_imerg_daily_incomplete_day_is_nan(mod):
    idx = pd.date_range("2025-01-01T00:00", "2025-01-01T12:00", freq="30min")
    out = mod.imerg_daily(pd.Series(1.0, index=idx), pd.DatetimeIndex(["2025-01-01"]),
                          ZoneInfo("UTC"), 0)  # fmt: skip
    assert np.isnan(out.iloc[0])


# ----------------------------------------------------------------- end to end


def test_removes_disagreeing_days_and_records_flags(mod, monkeypatch, tmp_path):
    inp = obs_zarr(tmp_path / "obs.zarr", [5.0, 20.0, 0.0, 2.0])
    fake = FakeDynamical({"2025-01-01": 5.5, "2025-01-02": 5.0, "2025-01-03": 0.0})
    ds, out = run(mod, monkeypatch, tmp_path, inp, fake)

    np.testing.assert_allclose(ds["satellite_precip"].values.ravel()[:3], [5.5, 5.0, 0.0])
    precip = ds["precip"].values.ravel()
    assert precip[0] == pytest.approx(5.0)
    assert np.isnan(precip[1])  # 20 vs 5
    assert precip[2] == 0.0
    assert np.isnan(precip[3])  # 2.0 mm vs IMERG 0.0: wet/dry mismatch
    flags = ds["satellite_qc"].values.ravel()
    assert flags[0] == 0 and flags[2] == 0
    assert flags[1] == mod.FLAG_ABS_DIFF | mod.FLAG_RATIO
    assert flags[3] == mod.FLAG_WET_DRY
    assert ds["satellite_qc"].attrs["flag_meanings"].split()[0] == "abs_difference"
    assert ds["satellite_precip"].attrs["units"] == "mm day-1"
    np.testing.assert_allclose(ds["temperature"].values, 25.0)  # other variables untouched
    assert ds.attrs["satellite_qc_abs_threshold"] == 10.0
    assert load_history(out)[-1]["skill"] == "observations-satellite-qc"
    # One station, 4 days (+ buffer) fits one window and one batch.
    assert len(fake.bodies) == 1


def test_unverified_days_removed_unless_kept(mod, monkeypatch, tmp_path):
    inp = obs_zarr(tmp_path / "obs.zarr", [1.0, 1.0])

    def no_data(url, json=None, timeout=None):
        return _Resp(
            200,
            {"results": [{"queryId": q["queryId"], "analysisTimes": [], "data": {}}
                         for q in json["queries"]]},
        )  # fmt: skip

    ds, _ = run(mod, monkeypatch, tmp_path, inp, no_data)
    assert np.isnan(ds["precip"].values).all()
    assert (ds["satellite_qc"].values == mod.FLAG_NO_SATELLITE).all()

    ds2, _ = run(mod, monkeypatch, tmp_path, inp, no_data, "--keep-unverified")
    np.testing.assert_allclose(ds2["precip"].values.ravel(), [1.0, 1.0])


def test_thresholds_are_configurable(mod, monkeypatch, tmp_path):
    inp = obs_zarr(tmp_path / "obs.zarr", [0.7])
    fake = FakeDynamical({"2025-01-01": 0.3})
    ds, _ = run(mod, monkeypatch, tmp_path, inp, fake)
    assert np.isnan(ds["precip"].values).all()  # ratio 2.33 >= 2
    ds2, _ = run(mod, monkeypatch, tmp_path, inp, fake, "--ratio-threshold", "3")
    assert ds2["precip"].values.ravel()[0] == pytest.approx(0.7)


def test_rejects_non_daily_input(mod, monkeypatch, tmp_path):
    inp = obs_zarr(tmp_path / "obs.zarr", [1.0, 1.0], interval="1 hour")
    with pytest.raises(SystemExit) as exc:
        run(mod, monkeypatch, tmp_path, inp, FakeDynamical({}))
    assert exc.value.code == 2


def test_retries_transient_errors(mod, monkeypatch, tmp_path):
    inp = obs_zarr(tmp_path / "obs.zarr", [1.0])
    fake = FakeDynamical({"2025-01-01": 1.0}, status=[503, 429])
    ds, _ = run(mod, monkeypatch, tmp_path, inp, fake)
    assert len(fake.bodies) == 3
    assert ds["precip"].values.ravel()[0] == pytest.approx(1.0)
