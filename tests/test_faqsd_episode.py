"""FAQSD reader: per-episode output and parquet weights.

Two things spacescans-web relies on that the v1 port did not provide:
  * time.output_grouping="episode" keeps one row per (PATID, geoid) so the
    web merge can join on its episode_id; "patient" (or no time block)
    averages across a patient's episodes, as v1 did.
  * source.file may be the web runner's per-task C3 *parquet*; the old
    loader read anything that was not .pkl as CSV.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from spacescans.plugins.readers.faqsd import FAQSDExposureSource

FIPS = 12073001100  # one Tallahassee tract


def _faqsd_dir(tmp_path: Path) -> Path:
    d = tmp_path / "FAQSD"; d.mkdir()
    # Same layout EPA ships (positional columns 0/1/4 are what the reader uses).
    days = pd.date_range("2013-01-01", "2013-01-10")
    o3 = pd.DataFrame({"Date": days.strftime("%Y/%m/%d"), "FIPS": FIPS, "Longitude": -84.3, "Latitude": 30.4,
                       "ozone_daily_8hour_maximum(ppb)": [30.0 + i for i in range(10)], "se": 1.0})
    pm = pd.DataFrame({"Date": days.strftime("%Y/%m/%d"), "FIPS": FIPS, "Longitude": -84.3, "Latitude": 30.4,
                       "pm25_daily_average(ug/m3)": [5.0 + i for i in range(10)], "se": 1.0})
    o3.to_csv(d / "2013_ozone_daily_8hour_maximum.txt", index=False)
    pm.to_csv(d / "2013_pm25_daily_average.txt", index=False)
    return d


def _weights_parquet(tmp_path: Path) -> Path:
    p = tmp_path / "c3_tract_us.parquet"
    # web C3 output shape: geoid = episode id, GEOID10 = tract, value = area weight
    # GEOID10 as the STRING the web C3 step emits (from the shapefile), not
    # the int64 the v1 pkl carried — the reader must reconcile the two.
    pd.DataFrame({"geoid": [0, 1], "GEOID10": [str(FIPS), str(FIPS)], "value": [1.0, 1.0]}).to_parquet(p, index=False)
    return p


def _patients() -> pd.DataFrame:
    # One patient, two episodes (distinct synthetic geoids), different windows.
    return pd.DataFrame({"PATID": ["P1", "P1"], "geoid": [0, 1],
                         "start": ["2013-01-01", "2013-01-06"], "end": ["2013-01-05", "2013-01-10"]})


def _reader(tmp_path: Path, grouping: str | None) -> FAQSDExposureSource:
    cfg = SimpleNamespace(
        source=SimpleNamespace(file=str(_weights_parquet(tmp_path))),
        exposure=SimpleNamespace(file=str(_faqsd_dir(tmp_path))),
        time=None if grouping is None else SimpleNamespace(output_grouping=grouping),
    )
    return FAQSDExposureSource(cfg)


def test_episode_grouping_keeps_one_row_per_episode(tmp_path: Path) -> None:
    out = _reader(tmp_path, "episode").compute_patient_exposure(_patients())
    assert list(out.columns) == ["PATID", "geoid", "o3", "pm25"]
    assert len(out) == 2
    by = out.set_index("geoid")
    # episode 0 = days 1–5 → o3 mean of 30..34 = 32 ; episode 1 = days 6–10 → 37
    assert by.loc[0, "o3"] == pytest.approx(32.0)
    assert by.loc[1, "o3"] == pytest.approx(37.0)
    assert by.loc[0, "pm25"] == pytest.approx(7.0)


@pytest.mark.parametrize("grouping", ["patient", None], ids=["patient", "no-time-block"])
def test_patient_grouping_collapses_episodes(tmp_path: Path, grouping) -> None:
    out = _reader(tmp_path, grouping).compute_patient_exposure(_patients())
    assert list(out.columns) == ["PATID", "o3", "pm25"]
    assert len(out) == 1
    assert out.loc[0, "o3"] == pytest.approx(34.5)   # mean over all 10 days


def test_parquet_weights_are_read_as_parquet(tmp_path: Path) -> None:
    r = _reader(tmp_path, "episode")
    w = r._load_weights(tmp_path)
    assert set(w.columns) == {"geoid", "GEOID10", "value"}
    assert w["GEOID10"].dtype == "int64"        # string in the parquet, int after loading
    assert w["GEOID10"].iloc[0] == FIPS
