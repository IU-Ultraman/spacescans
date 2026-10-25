"""Smoke test: spacescans.models.config loads + validates basic dicts."""
import pytest
from spacescans.models.config import DatasetConfig


def test_minimal_yearly_areal_config_validates():
    cfg = DatasetConfig.model_validate({
        "name": "test",
        "linkage_pattern": "yearly_areal",
        "geometry_type": "polygon",
        "source": {"file": "/abs/source.Rda", "join_col": "geoid"},
        "buffer": {"patient_file": "/abs/p.parquet", "patient_adapter": "demo_conus"},
        "exposure": {"file": "/abs/e.Rda", "join_col": "geoid", "value_cols": ["v1"], "year_col": "year"},
        "time": {"years": [2013, 2014], "temporal_resolution": "yearly", "temporal_mode": "yearly"},
        "engine": {"backend": "duckdb"},
        "output": {"path": "/abs/out.parquet", "format": "parquet"},
    })
    assert cfg.name == "test"
    assert cfg.linkage_pattern.value == "yearly_areal"


def test_unknown_pattern_rejected():
    with pytest.raises(Exception):  # pydantic ValidationError
        DatasetConfig.model_validate({
            "name": "x",
            "linkage_pattern": "totally_fake_pattern",
            "geometry_type": "polygon",
            "source": {"file": "x", "join_col": "x"},
            "buffer": {"patient_file": "x", "patient_adapter": "demo_conus"},
            "engine": {"backend": "duckdb"},
            "output": {"path": "x", "format": "parquet"},
        })


def test_engine_workers_is_an_optional_positive_count():
    from pydantic import ValidationError

    from spacescans.models.config import EngineConfig

    assert EngineConfig().workers is None
    assert EngineConfig(workers=4).workers == 4
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            EngineConfig(workers=bad)
