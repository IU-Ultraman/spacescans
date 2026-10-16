"""Parity with the web: the same inputs through spacescans-web's functions and
through spacescans.custom must give the same results, byte for byte where the
web writes bytes.

Opt-in, because it needs a spacescans-web checkout and the example files:

    SPACESCANS_WEB_BACKEND=/path/to/spacescans-web/backend
    SPACESCANS_CUSTOM_EXAMPLES=/path/to/custom_exposome_examples
    SPACESCANS_CUSTOM_R1=/path/to/spacescans-paper/results/data/validation/pipeline_outputs/custom

The web modules are copied into a temporary directory and imported from there
(with a stub ``app.config``), so the checkout is only read, never written.
The R1 directory is the paper's reference web run of the custom raster path
(synth_1k, the yearly example rasters).
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from spacescans import custom

pytestmark = [pytest.mark.geo, pytest.mark.slow]


def _env_dir(name: str) -> Path:
    value = os.environ.get(name)
    if not value or not Path(value).is_dir():
        pytest.skip(f"{name} is not set to a directory")
    return Path(value)


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    pytest.importorskip("rasterio")
    backend = _env_dir("SPACESCANS_WEB_BACKEND")
    root = tmp_path_factory.mktemp("webcopy")
    app = root / "app"
    (app / "experiments").mkdir(parents=True)
    for rel in ("custom_exposomes.py", "raster_values.py", "cohort_dates.py",
                "experiments/custom.py", "experiments/bg_ndi_wi.py"):
        shutil.copy(backend / "app" / rel, app / rel)
    (app / "__init__.py").write_text("")
    (app / "experiments" / "__init__.py").write_text("")
    (app / "experiments" / "_merge.py").write_text(
        "def write_partial(**kw):\n    raise NotImplementedError\n")
    # The web's own _unprovisioned, lifted out of variable_registry.py (whose
    # module needs jsonschema and the catalog files).
    src = (backend / "app" / "variable_registry.py").read_text()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_unprovisioned")
    (app / "variable_registry.py").write_text(
        "from __future__ import annotations\nfrom pathlib import Path\n\n"
        + ast.get_source_segment(src, fn) + "\n")
    (app / "config.py").write_text(textwrap.dedent("""
        from pathlib import Path
        from types import SimpleNamespace
        settings = SimpleNamespace(
            DATA_DIR=Path("/nonexistent"), SPACESCANS_DATA_DIR=Path("/nonexistent"),
            SPACESCANS_CONFIG_TEMPLATES_DIR=Path("/nonexistent"),
            C3_CACHE_DIR=Path("/nonexistent"), SPACESCANS_PIPELINE_PYTHON=Path("/nonexistent"),
            SPACESCANS_PIPELINE_CLI=Path("/nonexistent"))
    """))
    saved = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}
    for k in saved:
        del sys.modules[k]
    sys.path.insert(0, str(root))
    try:
        import app.config
        import app.custom_exposomes
        import app.experiments.custom
        import app.raster_values
        yield SimpleNamespace(settings=app.config.settings, lib=app.custom_exposomes,
                              rv=app.raster_values, runner=app.experiments.custom,
                              configs=backend.parent / "configs")
    finally:
        sys.path.remove(str(root))
        for k in [k for k in sys.modules if k == "app" or k.startswith("app.")]:
            del sys.modules[k]
        sys.modules.update(saved)


@pytest.fixture(scope="module")
def examples():
    return _env_dir("SPACESCANS_CUSTOM_EXAMPLES")


# file, boundary, key column, value columns, year column, units
CSV_EXAMPLES = [
    ("example_tract_static.csv", "Tract", "tract", ["greenness", "heat_index"], None,
     {"greenness": "index", "heat_index": "°F"}),
    ("example_tract_yearly.csv", "Tract", "tract", ["ndvi"], "year", {"ndvi": "index"}),
    ("example_bg_static.csv", "BG", "bg_geoid", ["greenness", "heat_index"], None, {}),
    ("example_bg_yearly.csv", "BG", "bg_geoid", ["ndvi"], "year", {}),
    ("example_zcta5_static.csv", "ZCTA5", "zcta", ["greenness", "heat_index"], None,
     {"heat_index": "F"}),
    ("example_zcta5_yearly.csv", "ZCTA5", "zcta", ["ndvi"], "year", {}),
    ("example_county_static.csv", "County", "county_fips", ["greenness", "heat_index"], None,
     {"greenness": "µg/m³"}),
    ("example_county_yearly.csv", "County", "county_fips", ["ndvi"], "year", {}),
]
RASTER_EXAMPLES = ["example_raster_static.tif", "example_raster_multiband_static.tif"] + [
    f"example_raster_{y}.tif" for y in range(2013, 2020)]


def _web_create(web, monkeypatch, tmp_path, **kw):
    monkeypatch.setattr(web.settings, "DATA_DIR", tmp_path / "webdata")
    return web.lib.create(1, **kw)


def _web_create_raster(web, monkeypatch, tmp_path, **kw):
    monkeypatch.setattr(web.settings, "DATA_DIR", tmp_path / "webdata")
    return web.lib.create_raster(1, **kw)


# --------------------------------------------------------------------------
# CSV uploads
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name,boundary,key_col,value_cols,year_col,units", CSV_EXAMPLES,
                         ids=[c[0] for c in CSV_EXAMPLES])
def test_csv_examples_match_the_web(web, examples, monkeypatch, tmp_path,
                                    name, boundary, key_col, value_cols, year_col, units):
    content = (examples / name).read_bytes()
    assert custom.preview_csv(content) == web.lib.preview(content)
    args = dict(boundary=boundary, key_col=key_col, value_cols=value_cols, year_col=year_col)
    assert custom.validate_csv(content, **args) == web.lib.validate(content, **args)

    labels = {value_cols[0]: "First column", "not_selected": "x"}
    m = _web_create(web, monkeypatch, tmp_path, content=content, name=f"  {name} ",
                    description="", value_labels=labels, value_units=units,
                    uploaded_filename=name, **args)
    d = custom.define_polygon(content, label=f"  {name} ", description="", labels=labels,
                              units=units, uploaded_filename=name, dataset_id=m["dataset_id"],
                              owner_uid=1, values_path=m["values_path"],
                              created_at=m["created_at"], **args)
    assert list(d.items()) == list(m.items())        # same fields, values and order
    # The web's first candidate id is the package's derivation with the uid as salt.
    assert m["dataset_id"] == custom.derive_dataset_id(content, name, salt="1")


def _head(path: Path, n: int = 400) -> list[str]:
    with open(path) as f:
        return [next(f).rstrip("\n") for _ in range(n)]


def _error_cases(examples):
    tract = _head(examples / "example_tract_static.csv")
    yearly = _head(examples / "example_tract_yearly.csv")
    lost_zero = [tract[0]] + [line.lstrip("0") for line in tract[1:]]
    duplicated = tract + tract[1:3]
    blank_key = tract + [",0.5,80"]
    text_values = [tract[0]] + [f"{line.split(',')[0]},high,low" for line in tract[1:]]
    bad_year = yearly + ["01001020100,someday,0.4"]
    dup_year = yearly + [yearly[1]]
    return [
        ("lost leading zero", lost_zero, dict(boundary="Tract", key_col="tract",
                                              value_cols=["greenness"], year_col=None)),
        ("wrong boundary", tract, dict(boundary="BG", key_col="tract",
                                       value_cols=["greenness"], year_col=None)),
        ("duplicate keys", duplicated, dict(boundary="Tract", key_col="tract",
                                            value_cols=["greenness"], year_col=None)),
        ("blank key", blank_key, dict(boundary="Tract", key_col="tract",
                                      value_cols=["greenness"], year_col=None)),
        ("text values", text_values, dict(boundary="Tract", key_col="tract",
                                          value_cols=["greenness", "heat_index"], year_col=None)),
        ("missing column", tract, dict(boundary="Tract", key_col="GEOID",
                                       value_cols=["greenness"], year_col=None)),
        ("key used as value", tract, dict(boundary="Tract", key_col="tract",
                                          value_cols=["tract"], year_col=None)),
        ("bad year", bad_year, dict(boundary="Tract", key_col="tract",
                                    value_cols=["ndvi"], year_col="year")),
        ("duplicate year", dup_year, dict(boundary="Tract", key_col="tract",
                                          value_cols=["ndvi"], year_col="year")),
        ("unknown boundary", tract, dict(boundary="Parcel", key_col="tract",
                                         value_cols=["greenness"], year_col=None)),
    ]


def test_csv_rejections_match_the_web_word_for_word(web, examples):
    for label, lines, args in _error_cases(examples):
        content = "\n".join(lines).encode()
        with pytest.raises(web.lib.CustomExposomeError) as theirs:
            web.lib.validate(content, **args)
        with pytest.raises(custom.CustomExposomeError) as ours:
            custom.validate_csv(content, **args)
        assert str(ours.value) == str(theirs.value), label


# --------------------------------------------------------------------------
# GeoTIFF uploads
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", RASTER_EXAMPLES)
def test_raster_examples_inspect_like_the_web(web, examples, name):
    content = (examples / name).read_bytes()
    theirs = web.rv.inspect(content)
    assert custom.inspect_raster(content) == theirs
    assert custom.inspect_raster(examples / name) == theirs
    assert custom.preview_raster(content) == web.lib.preview_raster(content)


def test_example_grid_is_the_documented_one(examples):
    meta = custom.inspect_raster(examples / "example_raster_multiband_static.tif")
    assert meta["grid_hash"] == "745b4463" and meta["band_count"] == 3


def _same_raster_definition(web, monkeypatch, tmp_path, rasters, **kw):
    m = _web_create_raster(web, monkeypatch, tmp_path, rasters=rasters, name="Example raster",
                           description="", **kw)
    paths = {r["uploaded_filename"]: r["path"] for r in m["rasters"]}
    d = custom.define_raster(rasters, label="Example raster", description="",
                             dataset_id=m["dataset_id"], owner_uid=1,
                             paths=[paths[f] for f, _c, _y in rasters],
                             created_at=m["created_at"], **_package_args(kw))
    assert list(d.items()) == list(m.items())
    return m


def _package_args(kw):
    out = dict(kw)
    for web_name, ours in (("value_labels", "labels"), ("value_units", "units")):
        if web_name in out:
            out[ours] = out.pop(web_name)
    return out


def test_static_raster_definition_matches_the_web(web, examples, monkeypatch, tmp_path):
    rasters = [("example_raster_static.tif",
                (examples / "example_raster_static.tif").read_bytes(), None)]
    _same_raster_definition(web, monkeypatch, tmp_path, rasters, value_col="ndvi",
                            value_label="Greenness", value_unit="index")


def test_yearly_raster_definition_matches_the_web(web, examples, monkeypatch, tmp_path):
    rasters = [(f"example_raster_{y}.tif", (examples / f"example_raster_{y}.tif").read_bytes(), y)
               for y in (2016, 2013, 2019, 2014, 2015, 2018, 2017)]          # any order
    m = _same_raster_definition(web, monkeypatch, tmp_path, rasters, value_col="val",
                                value_unit="index")
    assert m["coverage_years"] == [2013, 2019] and m["grid_hash"] == "745b4463"


def test_multiband_raster_definition_matches_the_web(web, examples, monkeypatch, tmp_path):
    name = "example_raster_multiband_static.tif"
    rasters = [(name, (examples / name).read_bytes(), None)]
    _same_raster_definition(web, monkeypatch, tmp_path, rasters, bands=[3, 1, 2],
                            value_cols=["east", "lat", "lon"],
                            value_labels={"lat": "Cell-centre latitude"},
                            value_units={"lat": "deg", "lon": "°"})


def test_raster_rejections_match_the_web(web, examples, monkeypatch, tmp_path):
    multi = (examples / "example_raster_multiband_static.tif").read_bytes()
    single = (examples / "example_raster_2013.tif").read_bytes()
    cases = [
        dict(rasters=[("m.tif", multi, None)], bands=[1, 4], value_cols=["a", "b"]),
        dict(rasters=[("m.tif", multi, None)], bands=[1, 2], value_cols=["a", "A"]),
        dict(rasters=[("a.tif", single, 2013), ("b.tif", single, 2013)]),
        dict(rasters=[("a.tif", single, 2013), ("m.tif", multi, 2014)], bands=[2],
             value_cols=["x"]),
        dict(rasters=[("bad.tif", b"tract,v\n1,2\n", None)]),
    ]
    for case in cases:
        with pytest.raises(web.lib.CustomExposomeError) as theirs:
            _web_create_raster(web, monkeypatch, tmp_path, name="x", description="", **case)
        with pytest.raises(custom.CustomExposomeError) as ours:
            custom.define_raster(case.pop("rasters"), label="x", **case)
        assert str(ours.value) == str(theirs.value)


# --------------------------------------------------------------------------
# plan, render, materialise, cache keys, diagnostics
# --------------------------------------------------------------------------

def _grid_ids(width=1180, height=520, west=-125.0, north=50.0, res=0.05):
    """Cells of the example grid under a spread of CONUS points (both sides of
    100 W, so the multi-band example's band 3 has data and nodata), plus ids
    outside the raster."""
    lons = np.linspace(-124.0, -67.5, 23)
    lats = np.linspace(25.5, 48.5, 11)
    ids = [int((north - lat) // res) * width + int((lon - west) // res)
           for lon in lons for lat in lats]
    return ids + [-1, width * height, width * height + 7]


@pytest.fixture
def selection(web, examples, monkeypatch, tmp_path):
    """Web manifests for every kind of dataset, as a task config would hold them."""
    defs = {}
    for name, boundary, key_col, value_cols, year_col, units in CSV_EXAMPLES:
        if name.startswith("example_bg_yearly"):
            continue                                   # 36 MB; the tract yearly covers it
        m = _web_create(web, monkeypatch, tmp_path, content=(examples / name).read_bytes(),
                        name=name, description="", boundary=boundary, key_col=key_col,
                        value_cols=value_cols, year_col=year_col, value_units=units,
                        uploaded_filename=name)
        defs[m["variable_key"]] = m
    static = _web_create_raster(web, monkeypatch, tmp_path, name="static raster", description="",
                                rasters=[("s.tif", (examples / "example_raster_static.tif")
                                          .read_bytes(), None)], value_col="ndvi")
    yearly = _web_create_raster(web, monkeypatch, tmp_path, name="yearly raster", description="",
                                rasters=[(f"{y}.tif", (examples / f"example_raster_{y}.tif")
                                          .read_bytes(), y) for y in range(2013, 2020)],
                                value_col="val")
    multi = _web_create_raster(web, monkeypatch, tmp_path, name="multi raster", description="",
                               rasters=[("m.tif", (examples / "example_raster_multiband_static.tif")
                                         .read_bytes(), None)],
                               bands=[3, 1, 2], value_cols=["east", "lat", "lon"])
    for m in (static, yearly, multi):
        defs[m["variable_key"]] = m
    return defs


def test_plan_and_rendered_yaml_match_the_web(web, selection, monkeypatch, tmp_path):
    import pandas as pd
    monkeypatch.setattr(web.settings, "SPACESCANS_CONFIG_TEMPLATES_DIR", web.configs)
    variables = list(selection)
    config = {"variables": variables, "buffer": {"size": 500, "raster_res_m": 50},
              "custom_variables": selection}
    theirs = web.runner.plan(config)
    ours = custom.plan(variables, selection)
    assert [(s.name, s.template_relpath, s.is_c3) for s in ours] == \
        [(s.name, s.template_relpath, s.is_c3) for s in theirs]
    assert sum(s.name.startswith("c3_customgrid_") for s in ours) == 1   # one shared grid

    task_dir = tmp_path / "tasks" / "task-1234abcd"
    (task_dir / "output").mkdir(parents=True)
    ids = _grid_ids()
    pd.DataFrame({"geoid": range(len(ids)), "grid_id": ids, "weight": 1.0}).to_parquet(
        task_dir / "output" / "c3_customgrid_745b4463.parquet", index=False)
    ctx = custom.Context(run_dir=task_dir, cohort_parquet=task_dir / "input.parquet",
                         output_level="episode",     # the web 0.2.7 configs
                         buffer_m=500, raster_res_m=50, name_suffix="_task_1234abcd")
    tables = 0
    for web_step, step in zip(theirs, ours):
        log = task_dir / "logs.jsonl"
        log.unlink(missing_ok=True)
        web_yaml = web.runner.render_yaml(web_step, task_dir, config).read_text()
        web_logs = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        values = custom.values_path(ctx, "" if step.is_c3 else step.dataset)
        web_values = values.read_bytes() if values.exists() else None

        records = custom.prepare_step(step, ctx, definitions=selection)
        cfg = custom.render(step, yaml.safe_load((web.configs / step.template).read_text()), ctx,
                            variables=variables, definitions=selection)
        assert yaml.safe_dump(cfg, sort_keys=False) == web_yaml, step.name
        assert records == [{k: r[k] for k in ("level", "source", "msg")} for r in web_logs]
        if web_values is not None:
            assert values.read_bytes() == web_values, step.name     # same parquet bytes
            assert len(records) == 1 and records[0]["msg"].startswith("materialised ")
            values.unlink()
            tables += 1
    assert tables == 3                  # the static, yearly and multi-band rasters


def test_cache_keys_and_tags_match_the_web(web, selection, tmp_path):
    p = tmp_path / "input.parquet"
    p.write_bytes(os.urandom(256))
    for step in custom.plan(list(selection), selection):
        if not step.is_c3:
            continue
        tag = web.runner._boundary_tag_for_step(step)
        assert custom.boundary_tag_for_step(step) == tag == step.cache_tag
        for buffer_m, raster_res_m in ((270, 25), (500, 50), (1000, 25)):
            cfg = {"buffer": {"size": buffer_m, "raster_res_m": raster_res_m}}
            assert custom.legacy_cache_key(p, tag, buffer_m=buffer_m, raster_res_m=raster_res_m) \
                == web.runner._cache_key(p, tag, cfg)


def test_diagnostics_match_the_web(web, selection, tmp_path):
    import pandas as pd
    task_dir = tmp_path / "task-diag0001"
    out = task_dir / "output"
    out.mkdir(parents=True)
    keys = list(selection)
    tract = next(k for k in keys if selection[k]["boundary"] == "Tract"
                 and not selection[k]["year_col"])
    raster = next(k for k in keys if custom.is_raster(selection[k]) and
                  len(selection[k]["value_cols"]) > 1)
    # Tract weights that touch the file's first tracts and three it lacks.
    covered = pd.read_csv(selection[tract]["values_path"], dtype=str, nrows=5)["tract"].tolist()
    pd.DataFrame({"geoid": range(8), "GEOID10": covered + ["72001956300", "72001956400",
                                                           "72003430100"],
                  "value": 1.0}).to_parquet(out / "c3_tract_us.parquet", index=False)
    pd.DataFrame({"geoid": range(8), "greenness": [0.3] * 3 + [np.nan] * 5,
                  "heat_index": [80.0] * 8}).to_parquet(out / f"c4_{tract}.parquet", index=False)
    pd.DataFrame({"grid_id": [1, 2, 3, 4], "east": [1.0, np.nan, np.nan, np.nan],
                  "lat": [1.0, 2.0, 3.0, np.nan], "lon": [1.0] * 4}).to_parquet(
        out / f"values_{raster}.parquet", index=False)
    pd.DataFrame({"geoid": range(4), "east": [1.0, np.nan, np.nan, np.nan],
                  "lat": [1.0] * 4}).to_parquet(out / f"c4_{raster}.parquet", index=False)

    variables = [tract, raster]
    config = {"variables": variables, "custom_variables": selection}
    web.runner._report_linkage_rate(task_dir, variables, config)
    web_records = [{k: r[k] for k in ("level", "source", "msg")}
                   for r in map(json.loads, (task_dir / "logs.jsonl").read_text().splitlines())]
    assert custom.diagnostics(task_dir, variables, selection) == web_records
    assert len(web_records) == 7
    for key in variables:
        assert custom.coverage_diagnosis(task_dir, key, selection[key]) == \
            web.runner._coverage_diagnosis(task_dir, key, selection[key])


def test_provisioned_boundaries_match_the_web(web, monkeypatch, tmp_path):
    root = tmp_path / "data-root"
    (root / "TRACT" / "C3").mkdir(parents=True)
    (root / "BG").mkdir()
    (root / "ZCTA5").mkdir()
    (root / "ZCTA5" / ".gitkeep").write_text("")
    monkeypatch.setattr(web.settings, "SPACESCANS_DATA_DIR", root)
    assert custom.provisioned_boundaries(root) == web.lib.provisioned_boundaries()


# --------------------------------------------------------------------------
# the paper's reference web run (R1)
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def r1():
    run = _env_dir("SPACESCANS_CUSTOM_R1")
    config = json.loads((run / "config.json").read_text())
    return SimpleNamespace(dir=run, config=config, defs=config["custom_variables"],
                           variables=config["variables"])


def test_r1_inputs_are_the_example_rasters(r1, examples):
    (key,) = r1.variables
    for raster in r1.defs[key]["rasters"]:
        local = examples / raster["uploaded_filename"]
        assert hashlib.sha256(local.read_bytes()).hexdigest() == raster["sha256"]


def test_r1_definition_is_rebuilt_from_the_example_rasters(r1, examples):
    (key,) = r1.variables
    ref = r1.defs[key]
    d = custom.define_raster(
        [(examples / r["uploaded_filename"], r["year"]) for r in ref["rasters"]],
        label=ref["label"], description=ref["description"], value_col="val", value_unit="index",
        paths=[r["path"] for r in ref["rasters"]], dataset_id=ref["dataset_id"],
        owner_uid=ref["owner_uid"], created_at=ref["created_at"])
    # R1 predates multi-band uploads: it has no bands/band_nodata fields yet.
    assert {k: v for k, v in d.items() if k not in ("bands", "band_nodata")} == ref


def test_r1_plan_and_yaml_are_reproduced(r1, load_template):
    run_dir = Path("/app/data/tasks/paper-A-custom")
    ctx = custom.Context(run_dir=run_dir, cohort_parquet=run_dir / "input.parquet",
                         output_level="episode",     # the web 0.2.7 configs
                         buffer_m=r1.config["buffer"]["size"],
                         raster_res_m=r1.config["buffer"]["raster_res_m"],
                         name_suffix="_task_A-custom")
    steps = custom.plan(r1.variables, r1.defs)
    assert [s.name for s in steps] == \
        [p.stem for p in sorted((r1.dir / "pipeline_configs").glob("*.yaml"))]
    for step in steps:
        cfg = custom.render(step, load_template(step.template), ctx, variables=r1.variables,
                            definitions=r1.defs)
        assert yaml.safe_dump(cfg, sort_keys=False) == \
            (r1.dir / "pipeline_configs" / f"{step.name}.yaml").read_text()


def test_r1_value_table_is_reproduced(r1, examples, tmp_path):
    import pandas as pd
    (key,) = r1.variables
    local = {**r1.defs[key], "rasters": [
        {**r, "path": str(examples / r["uploaded_filename"])} for r in r1.defs[key]["rasters"]]}
    res = custom.materialise_values(local, c3_parquet=r1.dir / "output" / "c3_customgrid_745b4463.parquet",
                                    out_path=tmp_path / "values.parquet")
    ref = pd.read_parquet(r1.dir / "output" / f"values_{key}.parquet")
    pd.testing.assert_frame_equal(pd.read_parquet(res.path), ref, check_exact=True)
    assert res.rows == 7343


def test_r1_linkage_report_is_reproduced(r1):
    logged = [json.loads(line) for line in (r1.dir / "logs.jsonl").read_text().splitlines()]
    merge = [{k: r[k] for k in ("level", "source", "msg")} for r in logged if r["source"] == "merge"]
    assert custom.diagnostics(r1.dir, r1.variables, r1.defs) == merge
