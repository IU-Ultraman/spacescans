"""Sprint 10 Phase A1: gridded_linkage.py output_grouping dispatch.

Mirrors tests/test_precomputed_static_linkage.py and tests/test_precomputed_areal_linkage.py.
Unlike the precomputed_static / precomputed_areal patterns which roll their own
SQL, gridded_linkage delegates the final aggregation to engine.temporal_aggregate.
The dispatch under test is the group_by= argument passed to that engine method:
  - output_grouping="patient" → group_by=["PATID"]
  - output_grouping="episode" → group_by=["PATID", "geoid"]
  - anything else → ValueError from resolve_output_grouping
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# The module imports rasterio/exactextract at top via _extras.require — skip if
# the geo extra isn't installed.
pytest.importorskip("rasterio")
pytest.importorskip("exactextract")

from spacescans.models.config import (
    BufferConfig,
    DatasetConfig,
    EngineConfig,
    ExposureConfig,
    OutputConfig,
    SourceConfig,
    TimeConfig,
)


def _make_demo_config(tmp_path: Path, output_grouping: str) -> DatasetConfig:
    # A minimal weights parquet so the source.file read returns something.
    weights_path = tmp_path / "weights.parquet"
    pd.DataFrame({
        "PATID": ["P1", "P1", "P2"],
        "geoid": [10, 11, 20],
        "grid_id": [1, 2, 3],
        "weight": [1.0, 1.0, 1.0],
    }).to_parquet(weights_path, index=False)

    # Mini patient fixture mirroring precomputed_static_mini.
    patients_path = tmp_path / "patients.parquet"
    pd.DataFrame({
        "PATID": ["P1", "P1", "P2"],
        "geoid": [10, 11, 20],
        "start": pd.to_datetime(["2017-01-01", "2017-07-01", "2017-01-01"]),
        "end":   pd.to_datetime(["2017-06-30", "2017-12-31", "2017-12-31"]),
        "long":  [-86.0, -86.0, -86.0],
        "lat":   [40.0, 40.0, 40.0],
    }).to_parquet(patients_path, index=False)

    return DatasetConfig(
        name="gridded_mini",
        linkage_pattern="gridded",
        geometry_type="raster",
        source=SourceConfig(file=str(weights_path)),
        buffer=BufferConfig(patient_file=str(patients_path), buffer_m=270),
        exposure=ExposureConfig(
            file="/dev/null",
            value_cols=["value"],
            start_col="start_date",
            end_col="end_date",
        ),
        time=TimeConfig(years=[2017], output_grouping=output_grouping),
        engine=EngineConfig(),
        output=OutputConfig(path=str(tmp_path / "out.parquet")),
        plugin="vnl",
    )


class _FakeReader:
    def __init__(self, config):
        self.config = config

    def load_exposure(self, years=None):
        # geoid×start/end exposure (windowed mode — start_col/end_col set)
        return pd.DataFrame({
            "grid_id":    [1, 2, 3],
            "value":      [10.0, 20.0, 30.0],
            "start_date": pd.to_datetime(["2017-01-01"] * 3),
            "end_date":   pd.to_datetime(["2017-12-31"] * 3),
        })


def _make_engine_stub() -> MagicMock:
    """Engine stub that records the TemporalAggSpec.group_by passed in."""
    engine = MagicMock()
    # join: returns a small frame (weights × exposure)
    engine.join.return_value = pd.DataFrame({
        "PATID": ["P1", "P1", "P2"],
        "geoid": [10, 11, 20],
        "grid_id": [1, 2, 3],
        "weight": [1.0, 1.0, 1.0],
        "value": [10.0, 20.0, 30.0],
        "start_date": pd.to_datetime(["2017-01-01"] * 3),
        "end_date":   pd.to_datetime(["2017-12-31"] * 3),
    })
    engine.weighted_aggregate.return_value = pd.DataFrame({
        "geoid": [10, 11, 20],
        "start_date": pd.to_datetime(["2017-01-01"] * 3),
        "end_date":   pd.to_datetime(["2017-12-31"] * 3),
        "value_aw": [10.0, 20.0, 30.0],
    })
    engine.date_range_join.return_value = pd.DataFrame({
        "PATID": ["P1", "P1", "P2"],
        "geoid": [10, 11, 20],
        "start_date": pd.to_datetime(["2017-01-01"] * 3),
        "end_date":   pd.to_datetime(["2017-12-31"] * 3),
        "value_aw": [10.0, 20.0, 30.0],
        "overlap_days": [181, 184, 365],
    })
    # temporal_aggregate: just echo a minimal output frame.
    engine.temporal_aggregate.return_value = pd.DataFrame({
        "PATID": ["P1", "P2"], "value_aw": [15.0, 30.0],
    })
    return engine


def _run(tmp_path: Path, output_grouping: str):
    from spacescans.linkage import gridded_linkage as mod

    cfg = _make_demo_config(tmp_path, output_grouping=output_grouping)
    engine = _make_engine_stub()
    with patch.object(mod, "get_reader", return_value=_FakeReader):
        mod.run_gridded(cfg, engine=engine)
    return engine


def test_gridded_groups_by_patid_when_output_grouping_patient(tmp_path):
    engine = _run(tmp_path, output_grouping="patient")
    spec = engine.temporal_aggregate.call_args.args[1]
    assert spec.group_by == ["PATID"], (
        f"patient branch must dispatch group_by=['PATID']; got {spec.group_by}"
    )


def test_gridded_groups_by_patid_geoid_when_episode(tmp_path):
    engine = _run(tmp_path, output_grouping="episode")
    spec = engine.temporal_aggregate.call_args.args[1]
    assert spec.group_by == ["PATID", "geoid"], (
        f"episode branch must dispatch group_by=['PATID','geoid']; got {spec.group_by}"
    )


def test_gridded_rejects_unknown_output_grouping(tmp_path):
    with pytest.raises(ValueError, match="unsupported output_grouping"):
        _run(tmp_path, output_grouping="foo")
