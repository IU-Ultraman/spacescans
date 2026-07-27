"""Sprint 2: TimeConfig.output_grouping + 3 linkage pattern dispatch.

These tests use mocked engine calls so they exercise the linkage modules'
dispatch logic without spinning up DuckDB on real data.
"""
import pandas as pd
import pytest

from spacescans.models.config import TimeConfig


def test_time_config_default_output_grouping_is_patient():
    cfg = TimeConfig()
    assert cfg.output_grouping == "patient"


def test_time_config_accepts_episode():
    cfg = TimeConfig(output_grouping="episode")
    assert cfg.output_grouping == "episode"


def test_time_config_accepts_patient_explicitly():
    cfg = TimeConfig(output_grouping="patient")
    assert cfg.output_grouping == "patient"


from unittest.mock import MagicMock, patch
from pathlib import Path


def _make_fake_yearly_areal_config(output_grouping: str = "patient"):
    """Build a minimal config object the linkage function can dispatch on."""
    from spacescans.models.config import (
        DatasetConfig, SourceConfig, BufferConfig, ExposureConfig,
        TimeConfig, EngineConfig, OutputConfig,
    )
    return DatasetConfig(
        name="test",
        linkage_pattern="yearly_areal",
        geometry_type="polygon",
        source=SourceConfig(file="/dev/null", join_col="GEOID10"),
        buffer=BufferConfig(patient_file="/dev/null", buffer_m=270),
        exposure=ExposureConfig(file="/dev/null", join_col="GEOID10",
                                value_cols=["v"], year_col="year"),
        time=TimeConfig(years=[2017], output_grouping=output_grouping),
        engine=EngineConfig(),
        output=OutputConfig(path="/tmp/test_out.parquet"),
    )


def test_yearly_areal_passes_patient_group_by_when_default(monkeypatch, tmp_path):
    """output_grouping='patient' → group_by=['PATID']."""
    captured = {}

    fake_engine = MagicMock()
    def capture_spec(data, spec):
        captured["group_by"] = spec.group_by
        return pd.DataFrame({"PATID": [], "v": []})
    fake_engine.join.return_value = pd.DataFrame()
    fake_engine.weighted_aggregate.return_value = pd.DataFrame()
    fake_engine.temporal_aggregate.side_effect = capture_spec

    from spacescans.linkage.yearly_areal_linkage import run_yearly_areal

    with patch("spacescans.linkage.yearly_areal_linkage.load_patients",
               return_value=pd.DataFrame({"PATID": [], "start": [], "end": [],
                                          "long": [], "lat": [], "geoid": []})), \
         patch("spacescans.linkage.yearly_areal_linkage.load_weights",
               return_value=pd.DataFrame()), \
         patch("spacescans.linkage.yearly_areal_linkage.read_table",
               return_value=pd.DataFrame()), \
         patch("spacescans.linkage.yearly_areal_linkage.build_episode_periods",
               return_value=pd.DataFrame({"PATID": [], "geoid": [],
                                          "period_id": [], "overlap_days": []})), \
         patch("spacescans.linkage.yearly_areal_linkage.apply_transforms",
               side_effect=lambda df, *a, **kw: df), \
         patch("spacescans.linkage.yearly_areal_linkage.write_table",
               return_value=Path("/tmp/test_out.parquet")):
        cfg = _make_fake_yearly_areal_config(output_grouping="patient")
        run_yearly_areal(cfg, fake_engine)

    assert captured["group_by"] == ["PATID"]


def test_yearly_areal_passes_patient_geoid_group_by_when_episode(monkeypatch, tmp_path):
    """output_grouping='episode' → group_by=['PATID', 'geoid']."""
    captured = {}

    fake_engine = MagicMock()
    def capture_spec(data, spec):
        captured["group_by"] = spec.group_by
        return pd.DataFrame({"PATID": [], "geoid": [], "v": []})
    fake_engine.join.return_value = pd.DataFrame()
    fake_engine.weighted_aggregate.return_value = pd.DataFrame()
    fake_engine.temporal_aggregate.side_effect = capture_spec

    from spacescans.linkage.yearly_areal_linkage import run_yearly_areal

    with patch("spacescans.linkage.yearly_areal_linkage.load_patients",
               return_value=pd.DataFrame({"PATID": [], "start": [], "end": [],
                                          "long": [], "lat": [], "geoid": []})), \
         patch("spacescans.linkage.yearly_areal_linkage.load_weights",
               return_value=pd.DataFrame()), \
         patch("spacescans.linkage.yearly_areal_linkage.read_table",
               return_value=pd.DataFrame()), \
         patch("spacescans.linkage.yearly_areal_linkage.build_episode_periods",
               return_value=pd.DataFrame({"PATID": [], "geoid": [],
                                          "period_id": [], "overlap_days": []})), \
         patch("spacescans.linkage.yearly_areal_linkage.apply_transforms",
               side_effect=lambda df, *a, **kw: df), \
         patch("spacescans.linkage.yearly_areal_linkage.write_table",
               return_value=Path("/tmp/test_out.parquet")):
        cfg = _make_fake_yearly_areal_config(output_grouping="episode")
        run_yearly_areal(cfg, fake_engine)

    assert captured["group_by"] == ["PATID", "geoid"]


def test_yearly_areal_invalid_output_grouping_raises(monkeypatch, tmp_path):
    fake_engine = MagicMock()
    from spacescans.linkage.yearly_areal_linkage import run_yearly_areal

    with patch("spacescans.linkage.yearly_areal_linkage.load_patients",
               return_value=pd.DataFrame({"PATID": [], "start": [], "end": [],
                                          "long": [], "lat": [], "geoid": []})), \
         patch("spacescans.linkage.yearly_areal_linkage.load_weights",
               return_value=pd.DataFrame()), \
         patch("spacescans.linkage.yearly_areal_linkage.read_table",
               return_value=pd.DataFrame()), \
         patch("spacescans.linkage.yearly_areal_linkage.build_episode_periods",
               return_value=pd.DataFrame()), \
         patch("spacescans.linkage.yearly_areal_linkage.apply_transforms",
               side_effect=lambda df, *a, **kw: df):
        cfg = _make_fake_yearly_areal_config(output_grouping="rubbish")
        with pytest.raises(ValueError, match="output_grouping"):
            run_yearly_areal(cfg, fake_engine)


def _make_fake_bg_vintage_config(output_grouping: str = "episode"):
    from spacescans.models.config import (
        DatasetConfig, SourceConfig, BufferConfig, ExposureConfig,
        TimeConfig, EngineConfig, OutputConfig,
    )
    return DatasetConfig(
        name="test_bg_vintage",
        linkage_pattern="yearly_areal_bg_vintage",
        geometry_type="polygon",
        source=SourceConfig(file="/dev/null", join_col="GEOID10"),
        source_2020=SourceConfig(file="/dev/null", join_col="GEOID"),
        buffer=BufferConfig(patient_file="/dev/null", buffer_m=270),
        exposure=ExposureConfig(
            file="/dev/null",
            vintage_col="bg_vintage",
            join_col_2010="bg_fips_2010",
            join_col_2020="bg_fips_2020",
            value_cols=["v"],
            year_col="index_year",
        ),
        time=TimeConfig(years=[2017], output_grouping=output_grouping),
        engine=EngineConfig(),
        output=OutputConfig(path="/tmp/test_out.parquet"),
    )


def test_yearly_areal_bg_vintage_passes_episode_group_by(monkeypatch, tmp_path):
    """output_grouping='episode' → temporal_aggregate.group_by includes geoid."""
    captured = {}

    fake_engine = MagicMock()
    def capture_spec(data, spec):
        captured["group_by"] = spec.group_by
        return pd.DataFrame({"PATID": [], "geoid": [], "v": []})
    fake_engine.join.return_value = pd.DataFrame()
    fake_engine.weighted_aggregate.return_value = pd.DataFrame()
    fake_engine.temporal_aggregate.side_effect = capture_spec

    from spacescans.linkage import yearly_areal_bg_vintage_linkage as mod

    with patch.object(mod, "load_patients",
                      return_value=pd.DataFrame({"PATID": [], "start": [], "end": [],
                                                  "long": [], "lat": [], "geoid": []})), \
         patch.object(mod, "load_weights", return_value=pd.DataFrame()), \
         patch.object(mod, "read_table",
                      return_value=pd.DataFrame({"bg_vintage": [2010], "v": [0.0]})), \
         patch.object(mod, "build_episode_periods",
                      return_value=pd.DataFrame({"PATID": [], "geoid": [],
                                                  "period_id": [], "overlap_days": []})), \
         patch.object(mod, "apply_transforms",
                      side_effect=lambda df, *a, **kw: df), \
         patch.object(mod, "write_table",
                      return_value=Path("/tmp/test_out.parquet")):
        cfg = _make_fake_bg_vintage_config(output_grouping="episode")
        mod.run_yearly_areal_bg_vintage(cfg, fake_engine)

    assert captured["group_by"] == ["PATID", "geoid"]


def test_static_areal_episode_grouping_widens_sql_output(monkeypatch, tmp_path):
    """static_areal with output_grouping='episode' should produce a result that
    has both PATID and geoid columns (vs just PATID in 'patient' mode)."""
    import pandas as pd
    from spacescans.engine.duckdb_engine import DuckDBEngine
    from spacescans.models.specs import DurationWeightedSpec

    engine = DuckDBEngine()

    # Per-geoid values: GEOID A has value 1.0, GEOID B has value 2.0
    values = pd.DataFrame({
        "geoid": [0, 1],
        "v": [1.0, 2.0],
    })

    # Patient PATID=P1 has 2 episodes (geoid 0 and geoid 1), ~100 days each.
    episodes = pd.DataFrame({
        "PATID": ["P1", "P1"],
        "geoid": [0, 1],
        "start_date": pd.to_datetime(["2017-01-01", "2017-05-01"]),
        "end_date":   pd.to_datetime(["2017-04-10", "2017-08-08"]),
    })

    # Patient mode: one row, v = duration-weighted avg of 1.0 and 2.0
    result_patient = engine.duration_weighted(
        values, episodes, DurationWeightedSpec(value_cols=["v"])
    )
    assert len(result_patient) == 1
    assert "PATID" in result_patient.columns
    assert "geoid" not in result_patient.columns

    # Episode mode: two rows, one per (PATID, geoid)
    result_episode = engine.duration_weighted(
        values, episodes,
        DurationWeightedSpec(value_cols=["v"], group_by_episode=True),
    )
    assert len(result_episode) == 2
    assert "PATID" in result_episode.columns
    assert "geoid" in result_episode.columns
    # Each row's v equals the geoid's value (1.0 or 2.0) because there's
    # no within-(PATID, geoid) averaging.
    sorted_by_geoid = result_episode.sort_values("geoid").reset_index(drop=True)
    assert sorted_by_geoid["v"].tolist() == [1.0, 2.0]


def test_static_areal_linkage_passes_group_by_episode(monkeypatch, tmp_path):
    """Verify run_static_areal sets group_by_episode=True on the spec when
    config.time.output_grouping == 'episode'."""
    captured = {}
    fake_engine = MagicMock()
    def capture_spec(values, episodes, spec):
        captured["group_by_episode"] = spec.group_by_episode
        return pd.DataFrame()
    fake_engine.join.return_value = pd.DataFrame()
    fake_engine.weighted_aggregate.return_value = pd.DataFrame()
    fake_engine.duration_weighted.side_effect = capture_spec

    from spacescans.linkage import static_areal_linkage as mod
    from spacescans.models.config import (
        DatasetConfig, SourceConfig, BufferConfig, ExposureConfig,
        TimeConfig, EngineConfig, OutputConfig,
    )
    cfg = DatasetConfig(
        name="test_static",
        linkage_pattern="static_areal",
        geometry_type="polygon",
        source=SourceConfig(file="/dev/null", join_col="GEOID10"),
        buffer=BufferConfig(patient_file="/dev/null", buffer_m=270),
        exposure=ExposureConfig(file="/dev/null", join_col="GEOID10", value_cols=["v"]),
        time=TimeConfig(output_grouping="episode"),
        engine=EngineConfig(),
        output=OutputConfig(path="/tmp/test_static_out.parquet"),
    )

    with patch.object(mod, "load_patients",
                      return_value=pd.DataFrame({"PATID": [], "start": [], "end": [],
                                                  "long": [], "lat": [], "geoid": []})), \
         patch.object(mod, "load_weights", return_value=pd.DataFrame()), \
         patch.object(mod, "read_table", return_value=pd.DataFrame()), \
         patch.object(mod, "prepare_episodes",
                      return_value=pd.DataFrame({"PATID": [], "geoid": [],
                                                  "start_date": [], "end_date": []})), \
         patch.object(mod, "apply_transforms",
                      side_effect=lambda df, *a, **kw: df), \
         patch.object(mod, "write_table",
                      return_value=Path("/tmp/test_static_out.parquet")):
        mod.run_static_areal(cfg, fake_engine)

    assert captured["group_by_episode"] is True
