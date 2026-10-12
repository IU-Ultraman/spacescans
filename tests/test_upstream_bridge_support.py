"""Upstream changes the bridge needs (tier 2 design §2.5, U1–U6).

All of them are additive: the web only calls `spacescans run`, whose path is
unchanged. These tests pin the backward-compatible behaviour.
"""
from __future__ import annotations

import copy
import re
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pandas as pd
import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "src" / "spacescans"
import os
# The web templates are checked too when a spacescans-web checkout sits next to
# this repo (or SPACESCANS_WEB_CONFIGS points at its configs/); absent in CI.
WEB_CONFIGS = Path(os.environ.get("SPACESCANS_WEB_CONFIGS", REPO.parent / "spacescans-web" / "configs"))

BLOCK_GEO = """
import sys
from importlib.abc import MetaPathFinder
_BLOCKED = {"geopandas", "rasterio", "shapely", "exactextract", "pyogrio",
            "pyreadr", "pyhdf", "xarray", "netCDF4"}
class _Blocker(MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in _BLOCKED:
            raise ModuleNotFoundError(f"No module named {name!r} (blocked for base-install test)",
                                      name=name.split(".")[0])
        return None
sys.meta_path.insert(0, _Blocker())
import importlib.util
_real_find_spec = importlib.util.find_spec
importlib.util.find_spec = lambda n, *a, **k: None if n.split(".")[0] in _BLOCKED else _real_find_spec(n, *a, **k)
"""


def _run_py(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=str(REPO))


def make_c4_fixture(tmp: Path, *, years=(2017, 2018)) -> dict:
    """A tiny yearly_areal C4 config that runs on a base install."""
    patients = pd.DataFrame({
        "pid": ["P1", "P1", "P2"],
        "startDate": pd.to_datetime(["2017-01-01", "2018-01-01", "2017-06-01"]),
        "endDate": pd.to_datetime(["2017-12-31", "2018-12-31", "2018-05-31"]),
        "longitude": [-86.1, -86.2, -86.3],
        "latitude": [39.7, 39.8, 39.9],
        "episode_id": [0, 1, 2],
    })
    weights = pd.DataFrame({"geoid": [0, 1, 2, 2], "GEOID10": ["A", "B", "A", "B"],
                            "weight": [1.0, 1.0, 0.5, 0.5]})
    exposure = pd.DataFrame({"GEOID10": ["A", "A", "B", "B"], "year": [2017, 2018, 2017, 2018],
                             "v": [10.0, 20.0, 30.0, 40.0]})
    patients.to_parquet(tmp / "patients.parquet", index=False)
    weights.to_parquet(tmp / "weights.parquet", index=False)
    exposure.to_parquet(tmp / "exposure.parquet", index=False)
    return {
        "name": "c4_fixture",
        "linkage_pattern": "yearly_areal",
        "geometry_type": "polygon",
        "source": {"file": "weights.parquet", "join_col": "GEOID10"},
        "buffer": {"patient_file": "patients.parquet", "patient_adapter": "demo_conus"},
        "exposure": {"file": "exposure.parquet", "join_col": "GEOID10",
                     "value_cols": ["v"], "year_col": "year"},
        "time": {"years": list(years), "output_grouping": "episode"},
        "output": {"path": "c4_fixture.parquet"},
    }


# --- U1: config from a dict -------------------------------------------------

def _yaml_files() -> list[Path]:
    files = sorted((REPO / "configs").rglob("*.yaml"))
    files += sorted((PKG / "resources" / "configs").rglob("*.yaml"))
    if WEB_CONFIGS.is_dir():
        files += sorted(WEB_CONFIGS.rglob("*.yaml"))
    return files


@pytest.mark.parametrize("path", _yaml_files(), ids=lambda p: f"{p.parent.parent.name}/{p.parent.name}/{p.name}")
def test_load_config_dict_matches_load_config(path, tmp_path, monkeypatch, clean_env):
    from spacescans.pipeline.loader import load_config, load_config_dict
    monkeypatch.setenv("SPACESCANS_QUICKSTART_DATA_DIR", str(tmp_path / "qs"))
    kw = {"data_dir": str(tmp_path / "data"), "output_dir": str(tmp_path / "out")}
    raw = yaml.safe_load(path.read_text())
    before = copy.deepcopy(raw)
    try:
        expected = load_config(path, **kw)
    except Exception as e:  # same failure either way
        with pytest.raises(type(e)):
            load_config_dict(raw, **kw)
        return
    assert load_config_dict(raw, **kw) == expected
    assert raw == before, "load_config_dict must not mutate its input"


def test_load_config_dict_rejects_non_mapping():
    from spacescans.config_resolution import ConfigResolutionError
    from spacescans.pipeline.loader import load_config_dict
    with pytest.raises(ConfigResolutionError, match="mapping"):
        load_config_dict(None)
    with pytest.raises(ConfigResolutionError):
        load_config_dict(["a"])


def test_pipeline_from_dict_equals_from_config(tmp_path, clean_env):
    from spacescans.pipeline.runner import Pipeline
    raw = make_c4_fixture(tmp_path)
    cfg_path = tmp_path / "c4.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    a = Pipeline.from_config(cfg_path, data_dir=str(tmp_path), output_dir=str(tmp_path / "out"))
    b = Pipeline.from_dict(raw, data_dir=str(tmp_path), output_dir=str(tmp_path / "out"))
    assert a.config == b.config
    out = b.run()
    df = pd.read_parquet(out)
    assert list(df.columns) == ["PATID", "geoid", "v"]
    assert sorted(df["geoid"].tolist()) == [0, 1, 2]


# --- U2: Pipeline() initialises the registry -------------------------------

def test_pipeline_constructor_initialises_registry(tmp_path):
    raw = make_c4_fixture(tmp_path)
    code = textwrap.dedent(f"""
        from spacescans.pipeline import registry
        assert not registry._initialized
        from spacescans.models.config import DatasetConfig
        from spacescans.config_resolution import resolve_config
        from pathlib import Path
        raw = {raw!r}
        cfg = DatasetConfig.model_validate(resolve_config(raw, data_dir=Path({str(tmp_path)!r}),
                                                          output_dir=Path({str(tmp_path)!r})))
        from spacescans.pipeline.runner import Pipeline
        out = Pipeline(cfg).run()
        print("OUT", out)
    """)
    r = _run_py(code)
    assert r.returncode == 0, r.stderr
    assert "OUT" in r.stdout and (tmp_path / "c4_fixture.parquet").is_file()


# --- U3: unavailable patterns explain themselves ----------------------------

def test_optional_provides_matches_decorators():
    from spacescans.pipeline import registry
    assert set(registry._OPTIONAL_PROVIDES) == set(registry._OPTIONAL_MODULES)
    for mod, (kind, name) in registry._OPTIONAL_PROVIDES.items():
        src = (REPO / "src" / Path(*mod.split("."))).with_suffix(".py").read_text()
        found = re.findall(r'@register_(pattern|reader|prep)\("([a-z_0-9]+)"\)', src)
        assert found == [(kind, name)], mod


def test_unknown_name_is_still_a_plain_keyerror():
    from spacescans._extras import MissingExtraError
    from spacescans.pipeline.registry import get_pattern, get_reader, init
    init()
    with pytest.raises(KeyError) as ei:
        get_pattern("no_such_pattern")
    assert not isinstance(ei.value, MissingExtraError)
    assert "Unknown linkage pattern" in str(ei.value)
    with pytest.raises(KeyError):
        get_reader("no_such_reader")


def test_missing_extra_reraised_for_known_pattern_on_base_install():
    code = BLOCK_GEO + textwrap.dedent("""
        from spacescans.pipeline import registry
        from spacescans._extras import MissingExtraError
        registry.init()
        try:
            registry.get_pattern("boundary_overlap_fast")
        except registry.UnavailablePatternError as e:
            assert isinstance(e, KeyError) and isinstance(e, MissingExtraError)
            assert str(e).startswith("Unknown linkage pattern: boundary_overlap_fast")
            assert e.extra == "geo", e.extra
            assert "spacescans-pipeline[geo]" in str(e)
            print("RAISED")
        try:
            registry.get_reader("noise")
        except registry.UnavailablePatternError as e:
            assert "Unknown reader plugin" in str(e) and e.extra == "geo"
            print("READER")
        av = registry.available()
        info = av["unavailable"]["spacescans.linkage.boundary_overlap_fast_linkage"]
        assert info["extra"] == "geo" and info["provides"] == ["pattern", "boundary_overlap_fast"]
        assert "yearly_areal" in av["patterns"]
        assert "boundary_overlap_fast" not in av["patterns"]
        print("OK")
    """)
    r = _run_py(code)
    assert r.returncode == 0, r.stderr
    assert "RAISED" in r.stdout and "READER" in r.stdout and "OK" in r.stdout


def test_cli_exit_code_unchanged_for_unavailable_pattern(tmp_path):
    cfg = tmp_path / "c3.yaml"
    cfg.write_text(yaml.safe_dump({
        "name": "x", "linkage_pattern": "boundary_overlap_fast", "geometry_type": "polygon",
        "source": {"file": str(tmp_path / "a.shp"), "join_col": "G"},
        "buffer": {"patient_file": str(tmp_path / "p.parquet")},
        "output": {"path": str(tmp_path / "o.parquet")},
    }))
    code = BLOCK_GEO + textwrap.dedent(f"""
        import sys
        from spacescans.cli import main
        sys.exit(main(["run", {str(cfg)!r}]))
    """)
    r = _run_py(code)
    assert r.returncode == 1
    assert "Unknown linkage pattern: boundary_overlap_fast" in r.stderr


def test_missing_extra_error_keeps_old_constructor():
    from spacescans._extras import MissingExtraError, require
    e = MissingExtraError("plain message")
    assert str(e) == "plain message" and e.extra is None and e.missing == ()
    with pytest.raises(MissingExtraError) as ei:
        require("nope_extra", "this_module_does_not_exist")
    assert ei.value.extra == "nope_extra"
    assert ei.value.missing == ("this_module_does_not_exist",)
    assert "IU-Ultraman/spacescans" in str(ei.value)


# --- U4: engine options ------------------------------------------------------

def test_engine_config_defaults_are_none():
    from spacescans.models.config import EngineConfig
    e = EngineConfig()
    assert (e.threads, e.memory_limit, e.temp_directory) == (None, None, None)
    with pytest.raises(Exception):
        EngineConfig(threads=0)


def test_duckdb_engine_default_settings_unchanged():
    import duckdb
    from spacescans.engine.duckdb_engine import DuckDBEngine
    ref = duckdb.connect().execute(
        "SELECT current_setting('threads'), current_setting('memory_limit'), "
        "current_setting('temp_directory')").fetchone()
    s = DuckDBEngine().settings()
    assert (s["threads"], s["memory_limit"], s["temp_directory"]) == tuple(str(v) for v in ref)


def test_duckdb_engine_applies_settings(tmp_path):
    from spacescans.engine.duckdb_engine import DuckDBEngine
    s = DuckDBEngine(threads=2, memory_limit="1GB", temp_directory=str(tmp_path / "spill")).settings()
    assert s["threads"] == "2"
    assert s["memory_limit"] in ("953.6 MiB", "1.0 GiB", "1GB", "1000.0 MiB")
    assert s["temp_directory"] == str(tmp_path / "spill")


def test_pipeline_threads_reach_duckdb(tmp_path, clean_env):
    from spacescans.pipeline.runner import Pipeline
    raw = make_c4_fixture(tmp_path)
    raw["engine"] = {"threads": 1}
    pipe = Pipeline.from_dict(raw, data_dir=str(tmp_path), output_dir=str(tmp_path))
    assert pipe.engine.settings()["threads"] == "1"
    base = Pipeline.from_dict(make_c4_fixture(tmp_path), data_dir=str(tmp_path), output_dir=str(tmp_path))
    out_default = pd.read_parquet(base.run()).sort_values("geoid").reset_index(drop=True)
    out_one = pd.read_parquet(pipe.run()).sort_values("geoid").reset_index(drop=True)
    pd.testing.assert_frame_equal(out_default, out_one, check_exact=True)


# --- U5: python -m spacescans -----------------------------------------------

def test_python_m_spacescans_help():
    r = subprocess.run([sys.executable, "-m", "spacescans", "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    assert "run" in r.stdout and "quickstart" in r.stdout


# --- U6: capabilities --------------------------------------------------------

def test_capabilities_shape():
    import spacescans
    caps = spacescans.capabilities()
    assert caps["spacescans"]["version"] == spacescans.__version__
    assert caps["spacescans"]["code_fp"].startswith("sha256:")
    assert set(caps["extras"]) == {"geo", "rda", "hdf4", "nc"}
    assert caps["flags"]["episode_grouping"] is True
    assert "yearly_areal" in caps["registry"]["patterns"]
    assert caps["python"]["executable"] == sys.executable
    assert set(caps["native"]) == {"gdal", "gdal_pyogrio", "geos", "proj"}


def test_capabilities_on_base_install():
    code = BLOCK_GEO + textwrap.dedent("""
        import json, spacescans
        caps = spacescans.capabilities()
        assert caps["extras"]["geo"]["available"] is False
        assert caps["native"]["gdal"] is None
        print(json.dumps(caps["extras"]["geo"]))
    """)
    r = _run_py(code)
    assert r.returncode == 0, r.stderr
    assert '"available": false' in r.stdout


def test_code_fingerprint_matches_wheel_algorithm(tmp_path):
    from spacescans._capabilities import code_fingerprint, fingerprint_wheel
    pkg = tmp_path / "spacescans"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text("x = 1\n")
    (pkg / "sub" / "m.py").write_text("y = 2\n")
    (pkg / "sub" / "data.yaml").write_text("ignored: true\n")
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "junk.py").write_text("ignored\n")
    whl = tmp_path / "x.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.writestr("spacescans/sub/m.py", "y = 2\n")
        zf.writestr("spacescans/__init__.py", "x = 1\n")
        zf.writestr("spacescans/sub/data.yaml", "ignored: true\n")
        zf.writestr("spacescans_pipeline-0.0.0.dist-info/METADATA", "Name: x\n")
    assert code_fingerprint(pkg) == fingerprint_wheel(whl)
    (pkg / "sub" / "m.py").write_text("y = 3\n")
    code_fingerprint.cache_clear()
    assert code_fingerprint(pkg) != fingerprint_wheel(whl)
