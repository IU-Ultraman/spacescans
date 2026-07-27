"""Sprint 7 Phase A: precomputed_static_linkage.py output_grouping dispatch.

Mirrors tests/test_precomputed_areal_linkage.py — uses a 10-row mini fixture
with two multi-episode PATIDs (P1 -> {10,11}; P2 -> {20,21}) so the episode
branch must emit strictly more rows than the patient branch.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from spacescans.models.config import (
    BufferConfig,
    DatasetConfig,
    EngineConfig,
    ExposureConfig,
    OutputConfig,
    SourceConfig,
    TimeConfig,
)

FIXTURE = Path(__file__).parent / "fixtures" / "precomputed_static_mini.parquet"


def _make_demo_config(tmp_path: Path, output_grouping: str) -> DatasetConfig:
    return DatasetConfig(
        name="precomputed_static_mini",
        linkage_pattern="precomputed_static",
        geometry_type="line",
        source=SourceConfig(file="/dev/null"),
        buffer=BufferConfig(patient_file=str(FIXTURE), buffer_m=270),
        exposure=ExposureConfig(
            file="/dev/null",
            value_cols=["dist_coast_m", "ndvi"],
        ),
        time=TimeConfig(years=[2017], output_grouping=output_grouping),
        engine=EngineConfig(),
        output=OutputConfig(path=str(tmp_path / "out.parquet")),
        plugin="nhd_bluespace",
    )


def _exposure_frame() -> pd.DataFrame:
    """Geoid-level static exposure for every geoid in the fixture."""
    return pd.DataFrame({
        "geoid":        [10, 11, 20, 21, 30],
        "dist_coast_m": [100.0, 200.0, 300.0, 400.0, 500.0],
        "ndvi":         [0.10, 0.20, 0.30, 0.40, 0.50],
    })


class _FakeReader:
    def __init__(self, config):
        self.config = config

    def load_exposure(self):
        return _exposure_frame()


def _run(tmp_path: Path, output_grouping: str) -> pd.DataFrame:
    from spacescans.linkage import precomputed_static_linkage as mod

    cfg = _make_demo_config(tmp_path, output_grouping=output_grouping)
    with patch.object(mod, "get_reader", return_value=_FakeReader):
        mod.run_precomputed_static(cfg, engine=None)
    return pd.read_parquet(cfg.output.path)


def test_precomputed_static_groups_by_patid_when_output_grouping_patient(tmp_path):
    df = _run(tmp_path, output_grouping="patient")
    assert list(df.columns) == ["PATID", "dist_coast_m", "ndvi"]
    assert df["PATID"].is_unique
    assert len(df) == 8


def test_precomputed_static_groups_by_patid_geoid_when_episode(tmp_path):
    df_patient = _run(tmp_path, output_grouping="patient")
    df_episode = _run(tmp_path, output_grouping="episode")
    assert list(df_episode.columns) == ["PATID", "geoid", "dist_coast_m", "ndvi"]
    assert df_episode.groupby(["PATID", "geoid"]).size().max() == 1
    assert len(df_episode) > len(df_patient)
    assert len(df_episode) == 10


def test_precomputed_static_rejects_unknown_output_grouping(tmp_path):
    with pytest.raises(ValueError, match="unsupported output_grouping"):
        _run(tmp_path, output_grouping="foo")
