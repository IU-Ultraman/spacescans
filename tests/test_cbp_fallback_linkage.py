"""cbp_fallback_linkage output_grouping dispatch (parity with yearly_areal/fara).

cbp_fallback splits the cohort: patients whose ZIP has no ZBP row fall back to
county-level CBP (a yearly_areal-style county branch), the rest keep their ZBP
values. Before this change the pattern hard-collapsed to per-PATID and dropped
``geoid``, so its C4 output could not feed spacescans-web's per-(patient,
episode) merge. It now honors ``TimeConfig.output_grouping`` like every other
C4 pattern:

  * patient grouping → columns [PATID, *value_cols], no geoid (v1 behaviour)
  * episode grouping → columns [PATID, geoid, *value_cols], one row per episode,
    preserved through BOTH the county-fallback and the ZBP-valid branches
  * unsupported grouping → ValueError
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

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

# The split hard-codes zbp_r_civic (missing test) and zbp_r_bowling (valid test),
# so value_cols must include r_civic and r_bowling.
_VALUE_COLS = ["r_civic", "r_bowling"]


def _make_config(tmp_path: Path, output_grouping: str) -> DatasetConfig:
    return DatasetConfig(
        name="cbp_fallback_test",
        linkage_pattern="cbp_fallback",
        geometry_type="polygon",
        source=SourceConfig(file="county_weights.parquet", join_col="GEOID10"),
        buffer=BufferConfig(patient_file="/dev/null", buffer_m=270),
        exposure=ExposureConfig(
            file="county_cbp.rda",
            key="cbp",
            join_col="fips",
            value_cols=_VALUE_COLS,
            year_col="year",
            zbp_file="zbp_linked.parquet",
        ),
        time=TimeConfig(years=[2017], output_grouping=output_grouping),
        engine=EngineConfig(),
        output=OutputConfig(path=str(tmp_path / "out.parquet")),
    )


def _linked_zbp() -> pd.DataFrame:
    """== c4_zcta5_cbp output: one row per episode, carrying geoid.

    "zbp1" (geoid 0) has a valid ZBP row → ZBP-valid branch.
    "cty1" (geoid 2) is NaN → county-fallback branch.
    """
    return pd.DataFrame({
        "PATID": ["zbp1", "cty1"],
        "geoid": [0, 2],
        "r_civic": [5.0, float("nan")],
        "r_bowling": [1.0, float("nan")],
    })


def _run(tmp_path: Path, output_grouping: str) -> dict:
    from spacescans.linkage import cbp_fallback_linkage as mod

    captured: dict = {}

    def capture_temporal(data, spec):
        captured["group_by"] = spec.group_by
        # Mimic the real engine: county branch emits group_by cols + value cols
        # for the fallback patient (cty1, geoid 2).
        row: dict = {}
        for k in spec.group_by:
            row[k] = ["cty1"] if k == "PATID" else [2]
        for v in spec.value_cols:
            row[v] = [9.0]
        return pd.DataFrame(row)

    fake_engine = MagicMock()
    fake_engine.join.return_value = pd.DataFrame({"geoid": [2], "period_id": [2017]})
    fake_engine.weighted_aggregate.return_value = pd.DataFrame(
        {"geoid": [2], "year": [2017.0], "r_civic": [9.0], "r_bowling": [9.0]}
    )
    fake_engine.temporal_aggregate.side_effect = capture_temporal

    def fake_read_table(path, **kw):
        if str(path) == "zbp_linked.parquet":
            return _linked_zbp()
        return pd.DataFrame(
            {"fips": [1], "year": [2017], "r_civic": [9.0], "r_bowling": [9.0]}
        )

    def fake_write(df, path):
        captured["result"] = df
        return Path(str(path))

    with patch.object(mod, "load_patients", return_value=pd.DataFrame({
        "PATID": ["zbp1", "cty1"], "geoid": [0, 2],
        "start": ["2017-01-01", "2017-01-01"],
        "end": ["2017-12-31", "2017-12-31"],
    })), \
         patch.object(mod, "load_weights", return_value=pd.DataFrame()), \
         patch.object(mod, "read_table", side_effect=fake_read_table), \
         patch.object(mod, "apply_transforms", side_effect=lambda df, *a, **kw: df), \
         patch.object(mod, "build_episode_periods", return_value=pd.DataFrame({
             "PATID": ["cty1"], "geoid": [2], "period_id": [2017], "overlap_days": [365],
         })), \
         patch.object(mod, "write_table", side_effect=fake_write):
        cfg = _make_config(tmp_path, output_grouping)
        mod.run_cbp_fallback(cfg, fake_engine)
    return captured


def test_cbp_fallback_groups_by_patid_when_patient(tmp_path):
    cap = _run(tmp_path, "patient")
    assert cap["group_by"] == ["PATID"]
    result = cap["result"]
    assert "geoid" not in result.columns
    assert set(result.columns) == {"PATID", "r_civic", "r_bowling"}


def test_cbp_fallback_groups_by_patid_geoid_when_episode(tmp_path):
    cap = _run(tmp_path, "episode")
    assert cap["group_by"] == ["PATID", "geoid"]
    result = cap["result"]
    # geoid survives BOTH branches (county-fallback cty1 + ZBP-valid zbp1).
    assert "geoid" in result.columns
    assert result.groupby(["PATID", "geoid"]).size().max() == 1
    assert set(result["PATID"]) == {"cty1", "zbp1"}


def test_cbp_fallback_rejects_unknown_output_grouping(tmp_path):
    with pytest.raises(ValueError, match="output_grouping"):
        _run(tmp_path, "rubbish")
