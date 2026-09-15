"""acag_multi honours time.output_grouping the way gridded_linkage does.

spacescans-web joins every C4 parquet back onto its cohort on
(PATID, geoid) — geoid being the synthetic per-row episode id the demo_conus
adapter assigns. A pattern that groups by PATID alone drops geoid, and the
web merge then raises KeyError on a column it cannot find. This pins the
dispatch and that geoid survives the per-pollutant merge and _nbm derivation.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

pytest.importorskip("rasterio")
pytest.importorskip("xarray")

from spacescans.models.config import (  # noqa: E402
    BufferConfig, DatasetConfig, EngineConfig, ExposureConfig,
    OutputConfig, SourceConfig, TimeConfig,
)


def _config(tmp_path: Path, output_grouping: str | None) -> DatasetConfig:
    weights = tmp_path / "weights.parquet"
    pd.DataFrame({"geoid": [10, 11, 20], "grid_id": [1, 2, 3], "weight": [1.0, 1.0, 1.0]}).to_parquet(weights, index=False)
    patients = tmp_path / "patients.parquet"
    pd.DataFrame({
        "PATID": ["P1", "P1", "P2"], "geoid": [10, 11, 20],
        "start": pd.to_datetime(["2017-01-01", "2017-07-01", "2017-01-01"]),
        "end": pd.to_datetime(["2017-06-30", "2017-12-31", "2017-12-31"]),
        "long": [-86.0] * 3, "lat": [40.0] * 3,
    }).to_parquet(patients, index=False)
    root = tmp_path / "xNorthAmerica"
    for d in ("PM25", "PM25_bm"):                    # only two species present; the rest are skipped
        (root / d / "BiWeekly").mkdir(parents=True)
    return DatasetConfig(
        name="acag_mini", linkage_pattern="acag_multi", geometry_type="raster",
        source=SourceConfig(file=str(weights)),
        buffer=BufferConfig(patient_file=str(patients), buffer_m=270),
        exposure=ExposureConfig(file=str(root), value_cols=["value"], start_col="start_date", end_col="end_date"),
        time=None if output_grouping is None else TimeConfig(years=[2017], output_grouping=output_grouping),
        engine=EngineConfig(), output=OutputConfig(path=str(tmp_path / "out.parquet")),
    )


class _FakeReader:
    def __init__(self, config): self.config = config
    def load_exposure(self, years=None):
        return pd.DataFrame({"grid_id": [1, 2, 3], "value": [10.0, 20.0, 30.0],
                             "start_date": pd.to_datetime(["2017-01-01"] * 3),
                             "end_date": pd.to_datetime(["2017-12-31"] * 3)})


def _engine(group_keys_seen: list) -> MagicMock:
    eng = MagicMock()
    eng.join.return_value = pd.DataFrame({"geoid": [10, 11, 20], "grid_id": [1, 2, 3], "weight": [1.0] * 3,
                                         "value": [10.0, 20.0, 30.0],
                                         "start_date": pd.to_datetime(["2017-01-01"] * 3),
                                         "end_date": pd.to_datetime(["2017-12-31"] * 3)})
    eng.weighted_aggregate.return_value = pd.DataFrame({"geoid": [10, 11, 20], "value_aw": [10.0, 20.0, 30.0],
                                                        "start_date": pd.to_datetime(["2017-01-01"] * 3),
                                                        "end_date": pd.to_datetime(["2017-12-31"] * 3)})
    eng.date_range_join.return_value = pd.DataFrame({"PATID": ["P1", "P1", "P2"], "geoid": [10, 11, 20],
                                                     "value_aw": [10.0, 20.0, 30.0], "overlap_days": [181, 184, 365],
                                                     "start_date": pd.to_datetime(["2017-01-01"] * 3)})
    def _temporal(df, spec):
        keys = spec.group_by if isinstance(spec.group_by, list) else [spec.group_by]
        group_keys_seen.append(list(keys))
        return df.groupby(keys, as_index=False)["value_aw"].mean()
    eng.temporal_aggregate.side_effect = _temporal
    return eng


@pytest.mark.parametrize("grouping,expected_keys,expected_rows", [
    ("episode", ["PATID", "geoid"], 3),
    ("patient", ["PATID"], 2),
    (None, ["PATID"], 2),          # no time block → v1/CLI default
])
def test_output_grouping_dispatch(tmp_path: Path, grouping, expected_keys, expected_rows) -> None:
    from spacescans.linkage import acag_linkage
    seen: list = []
    with patch.object(acag_linkage, "ACAGExposureSource", _FakeReader):
        out = acag_linkage.run_acag_multi(_config(tmp_path, grouping), _engine(seen))
    assert seen and all(k == expected_keys for k in seen), seen
    df = pd.read_parquet(out)
    assert len(df) == expected_rows
    assert ("geoid" in df.columns) == (grouping == "episode")
    # both PM25 dirs exist → base, _bm, and derived _nbm all present
    assert {"pm25", "pm25_bm", "pm25_nbm"} <= set(df.columns)
    assert (df["pm25_nbm"] == df["pm25"] - df["pm25_bm"]).all()


def test_empty_result_keeps_group_keys(tmp_path: Path) -> None:
    from spacescans.linkage import acag_linkage
    class _Empty(_FakeReader):
        def load_exposure(self, years=None):
            return pd.DataFrame(columns=["grid_id", "value", "start_date", "end_date"])
    with patch.object(acag_linkage, "ACAGExposureSource", _Empty):
        out = acag_linkage.run_acag_multi(_config(tmp_path, "episode"), _engine([]))
    assert list(pd.read_parquet(out).columns) == ["PATID", "geoid"]
