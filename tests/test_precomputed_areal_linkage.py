"""Sprint 5 Phase A: precomputed_areal_linkage.py output_grouping dispatch.

These tests exercise the SQL-clause-level dispatch at the terminal aggregation
of run_precomputed_areal. The fixture
tests/fixtures/precomputed_areal_mini.parquet contains 10 patient-episode
rows with two multi-episode PATIDs (P1 -> {10, 11}; P2 -> {20, 21}), so the
episode branch is guaranteed to produce strictly more rows than the patient
branch.
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

FIXTURE = Path(__file__).parent / "fixtures" / "precomputed_areal_mini.parquet"


def _make_demo_config(tmp_path: Path, output_grouping: str) -> DatasetConfig:
    return DatasetConfig(
        name="precomputed_areal_mini",
        linkage_pattern="precomputed_areal",
        geometry_type="line",
        source=SourceConfig(file="/dev/null"),
        buffer=BufferConfig(patient_file=str(FIXTURE), buffer_m=270),
        exposure=ExposureConfig(
            file="/dev/null",
            value_cols=["dist_pri", "dist_sec", "dist_prisec"],
            year_col="year",
        ),
        time=TimeConfig(years=[2017], output_grouping=output_grouping),
        engine=EngineConfig(),
        output=OutputConfig(path=str(tmp_path / "out.parquet")),
        plugin="tiger_roads",
    )


def _exposure_frame() -> pd.DataFrame:
    """Geoid x year exposure for every geoid in the fixture, year 2017."""
    geoids = [10, 11, 20, 21, 30]
    return pd.DataFrame({
        "geoid": geoids,
        "year": [2017] * len(geoids),
        "dist_pri":    [100.0, 200.0, 300.0, 400.0, 500.0],
        "dist_sec":    [110.0, 210.0, 310.0, 410.0, 510.0],
        "dist_prisec": [120.0, 220.0, 320.0, 420.0, 520.0],
    })


class _FakeReader:
    def __init__(self, config):
        self.config = config

    def load_exposure(self, years=None):
        return _exposure_frame()


def _run(tmp_path: Path, output_grouping: str) -> pd.DataFrame:
    from spacescans.linkage import precomputed_areal_linkage as mod

    cfg = _make_demo_config(tmp_path, output_grouping=output_grouping)
    with patch.object(mod, "get_reader", return_value=_FakeReader):
        mod.run_precomputed_areal(cfg, engine=None)
    return pd.read_parquet(cfg.output.path)


def test_precomputed_areal_groups_by_patid_when_output_grouping_patient(tmp_path):
    df = _run(tmp_path, output_grouping="patient")
    assert list(df.columns) == ["PATID", "dist_pri", "dist_sec", "dist_prisec"]
    assert df["PATID"].is_unique
    # 8 unique PATIDs in the fixture.
    assert len(df) == 8


def test_precomputed_areal_groups_by_patid_geoid_when_episode(tmp_path):
    df_patient = _run(tmp_path, output_grouping="patient")
    df_episode = _run(tmp_path, output_grouping="episode")
    assert list(df_episode.columns) == [
        "PATID", "geoid", "dist_pri", "dist_sec", "dist_prisec",
    ]
    # (PATID, geoid) is unique per row.
    assert df_episode.groupby(["PATID", "geoid"]).size().max() == 1
    # Strictly more rows than the patient branch — P1 and P2 each split into 2.
    assert len(df_episode) > len(df_patient)
    assert len(df_episode) == 10


def test_precomputed_areal_rejects_unknown_output_grouping(tmp_path):
    with pytest.raises(ValueError, match="unsupported output_grouping"):
        _run(tmp_path, output_grouping="foo")
