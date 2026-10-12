"""3-tier resolution: --data-dir > $SPACESCANS_DATA_DIR > yaml base_dir."""
import os
import pytest
from pathlib import Path
from spacescans.config_resolution import (
    resolve_data_dir, resolve_output_dir, expand_path, resolve_config,
    ConfigResolutionError,
)


def test_caller_spacescans_env_does_not_reach_tests():
    """tests/conftest.py hides the caller's SPACESCANS_* variables from each test.

    Without that, an exported SPACESCANS_DATA_DIR wins over the data_dir
    argument when ${SPACESCANS_DATA_DIR} is expanded, and
    test_resolve_config_rewrites_all_path_fields fails.
    """
    assert [k for k in os.environ if k.startswith("SPACESCANS_")] == []


# ---------- expand_path ----------

def test_expand_path_absolute_returned_as_is(tmp_path):
    abs_path = str(tmp_path / "x.txt")
    assert expand_path(abs_path, data_dir=None) == Path(abs_path)


def test_expand_path_template_variable_expanded(tmp_path, clean_env):
    out = expand_path("${SPACESCANS_DATA_DIR}/County/x.shp", data_dir=tmp_path)
    assert out == tmp_path / "County" / "x.shp"


def test_expand_path_relative_uses_data_dir(tmp_path):
    out = expand_path("County/x.shp", data_dir=tmp_path)
    assert out == tmp_path / "County" / "x.shp"


def test_expand_path_relative_without_data_dir_raises(clean_env):
    with pytest.raises(ConfigResolutionError) as exc_info:
        expand_path("County/x.shp", data_dir=None)
    msg = str(exc_info.value)
    assert "no data root configured" in msg
    assert "--data-dir" in msg
    assert "SPACESCANS_DATA_DIR" in msg
    assert "base_dir" in msg


# ---------- resolve_data_dir 3-tier priority ----------

def test_resolve_data_dir_cli_wins_over_env(tmp_path, monkeypatch):
    cli = tmp_path / "cli"
    env = tmp_path / "env"
    cli.mkdir(); env.mkdir()
    monkeypatch.setenv("SPACESCANS_DATA_DIR", str(env))
    assert resolve_data_dir(cli_value=str(cli), yaml_value=None) == cli


def test_resolve_data_dir_env_wins_over_yaml(tmp_path, monkeypatch):
    env = tmp_path / "env"
    yml = tmp_path / "yml"
    env.mkdir(); yml.mkdir()
    monkeypatch.setenv("SPACESCANS_DATA_DIR", str(env))
    assert resolve_data_dir(cli_value=None, yaml_value=str(yml)) == env


def test_resolve_data_dir_yaml_used_when_cli_and_env_unset(tmp_path, clean_env):
    yml = tmp_path / "yml"
    yml.mkdir()
    assert resolve_data_dir(cli_value=None, yaml_value=str(yml)) == yml


def test_resolve_data_dir_returns_none_when_all_unset(clean_env):
    assert resolve_data_dir(cli_value=None, yaml_value=None) is None


# ---------- resolve_output_dir mirror tests ----------

def test_resolve_output_dir_default_to_cwd_output(tmp_path, monkeypatch, clean_env):
    monkeypatch.chdir(tmp_path)
    assert resolve_output_dir(cli_value=None, yaml_value=None) == tmp_path / "output"


def test_resolve_output_dir_env_used(tmp_path, monkeypatch):
    monkeypatch.setenv("SPACESCANS_OUTPUT_DIR", str(tmp_path / "out"))
    assert resolve_output_dir(cli_value=None, yaml_value=None) == tmp_path / "out"


# ---------- resolve_config integration ----------

def test_resolve_config_rewrites_all_path_fields(tmp_path):
    raw = {
        "name": "test",
        "linkage_pattern": "yearly_areal",
        "source": {"file": "${SPACESCANS_DATA_DIR}/X.parquet", "join_col": "id"},
        "buffer": {"patient_file": "P.parquet"},
        "output": {"path": "result.parquet"},
    }
    resolved = resolve_config(raw, data_dir=tmp_path, output_dir=tmp_path / "out")
    assert resolved["source"]["file"] == str(tmp_path / "X.parquet")
    assert resolved["buffer"]["patient_file"] == str(tmp_path / "P.parquet")
    assert resolved["output"]["path"] == str(tmp_path / "out" / "result.parquet")


def test_resolve_config_preserves_non_path_fields(tmp_path):
    raw = {
        "name": "test",
        "buffer": {"patient_file": "P.parquet", "buffer_m": 270},
        "output": {"path": "x.parquet", "format": "parquet"},
    }
    resolved = resolve_config(raw, data_dir=tmp_path, output_dir=tmp_path)
    assert resolved["buffer"]["buffer_m"] == 270
    assert resolved["output"]["format"] == "parquet"


def test_resolve_config_rewrites_road_cache_dir(tmp_path):
    """road_cache_dir is used by c3 tiger/nhd proximity configs."""
    raw = {
        "source": {
            "file": "${SPACESCANS_DATA_DIR}/County/x.shp",
            "road_cache_dir": "cache/tiger",
        },
    }
    resolved = resolve_config(raw, data_dir=tmp_path, output_dir=tmp_path / "out")
    assert resolved["source"]["road_cache_dir"] == str(tmp_path / "cache" / "tiger")
