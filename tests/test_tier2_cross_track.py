"""Cross-track invariants of the tier-2 modules, checked on the integration branch.

Phase A kept several facts in two places; phase B (orch-core) keeps one copy
and makes the other side import it:

* the custom upload path's steps: ``custom.Step`` *is* ``catalog.Step``,
  ``custom.plan`` is ``catalog.plan("custom", ...)``, ``custom.render`` applies
  the catalog's render contract like ``orchestrate.render``, and both legacy
  key functions wrap ``orchestrate.cache.legacy_key`` (``WeightCache("legacy-v1")``);
* the cohort columns read as text: ``cohort.FIPS_STR_COLS`` is
  ``catalog.recipes.COHORT_STR_COLUMNS``;
* the custom constants: ``custom.BOUNDARY_C3`` etc. are derived from the catalog;
* the web catalog the cohort fixtures were generated from is the package's
  ``variables.json`` (the fixture keeps no copy).

Still two sources, held equal here: the variable list (``catalog.variables()``
and the data manifest) and the boundary layers (``data.boundary_datasets()``
and ``custom.BOUNDARY_C3``). Base install only, except the raster case (rasterio).
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

from spacescans import catalog, cohort, custom, data
from spacescans.catalog import recipes
from spacescans.custom import steps as custom_steps
from spacescans.orchestrate.cache import WeightCache
from spacescans.orchestrate.context import RunContext
from spacescans.orchestrate.render import render as orch_render

REPO = Path(__file__).resolve().parents[1]


def _catalog_reference_renderer():
    """The catalog track's reference renderer (its render contract, as code)."""
    spec = importlib.util.spec_from_file_location(
        "_catalog_conftest_for_cross_track", REPO / "tests" / "catalog" / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render_reference


def _signature(step):
    return (step.name, step.template, step.kind, step.cache_tag, step.uses_raster_res)


def _polygon_definitions(tmp_path: Path) -> dict[str, dict]:
    bg = tmp_path / "bg_yearly.csv"
    bg.write_text("GEOID10,year,v1,v2\n" + "".join(
        f"18097{i:07d},{y},{i * 0.5},{y - 2000}\n" for i in range(1, 6) for y in (2015, 2016)),
        encoding="utf-8")
    tract = tmp_path / "tract_static.csv"
    tract.write_text("GEOID10,score\n" + "".join(f"180970{i:05d},{i}\n" for i in range(1, 6)),
                     encoding="utf-8")
    defs = [
        custom.define_polygon(bg, boundary="BG", key_col="GEOID10", value_cols=["v1", "v2"],
                              year_col="year", label="cross-track bg"),
        custom.define_polygon(tract, boundary="Tract", key_col="GEOID10", value_cols=["score"],
                              label="cross-track tract"),
    ]
    return {d["variable_key"]: d for d in defs}


def _raster_definition(tmp_path: Path) -> dict:
    np = pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    tif = tmp_path / "grid.tif"
    with rasterio.open(tif, "w", driver="GTiff", width=10, height=10, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_origin(-87.0, 40.5, 0.1, 0.1),
                       nodata=-9999) as dst:
        dst.write(np.arange(100, dtype="float32").reshape(10, 10), 1)
    return custom.define_raster([(tif, None)], label="cross-track grid")


def _assert_custom_paths_agree(defs: dict[str, dict], tmp_path: Path) -> None:
    render_reference = _catalog_reference_renderer()
    keys = list(defs)
    assert custom.Step is catalog.Step
    for order in (keys, list(reversed(keys))):
        assert catalog.plan("custom", order, overlay=defs) == custom.plan(order, defs)
        assert ([_signature(s) for s in catalog.plan("custom", order, overlay=defs)]
                == [_signature(s) for s in custom.plan(order, defs)])

    import yaml

    run_dir = tmp_path / "task-0123abcd"
    ctx = custom.Context(run_dir=run_dir, cohort_parquet=run_dir / "input.parquet",
                         buffer_m=270, raster_res_m=25, name_suffix="_task_0123abcd")
    cohort_file = tmp_path / "input.parquet"
    cohort_file.write_bytes(b"only hashed")
    sha = hashlib.sha256(cohort_file.read_bytes()).hexdigest()
    run_ctx = RunContext(run_dir=run_dir, cohort_parquet=run_dir / "input.parquet",
                         data_dir=tmp_path, name_suffix="_task_0123abcd", overlay=defs)
    key_ctx = RunContext(run_dir=run_dir, cohort_parquet=cohort_file, data_dir=tmp_path)
    legacy = WeightCache(tmp_path / "c3_cache", key_format="legacy-v1")
    for step in catalog.plan("custom", keys, overlay=defs):
        expected = render_reference(step, run_dir=run_dir, buffer_m=270, raster_res_m=25,
                                    name_suffix="_task_0123abcd")
        cfg = custom.render(step, catalog.template(step.template), ctx, variables=keys,
                            definitions=defs)
        assert yaml.safe_dump(cfg, sort_keys=False) == expected, step.name
        assert orch_render(step, run_ctx) == cfg, step.name
        if step.is_c3:
            assert (recipes.legacy_cache_key(step, sha, buffer_m=270, raster_res_m=25)
                    == custom.legacy_cache_key(cohort_file, custom.boundary_tag_for_step(step),
                                               buffer_m=270, raster_res_m=25)
                    == legacy.key(step, key_ctx))


def test_custom_constants_agree():
    assert dict(recipes.CUSTOM_BOUNDARY_C3) == {
        b: name for b, (name, _template, _tag) in custom_steps.BOUNDARY_C3.items()}
    assert recipes.CUSTOM_C4_TEMPLATE == custom_steps.C4_TEMPLATE
    assert recipes.CUSTOM_GRID_TEMPLATE == custom_steps.C3_GRID_TEMPLATE
    assert recipes.CUSTOM_GRID_TAG_PREFIX == custom_steps.GRID_TAG_PREFIX
    assert recipes.CUSTOM_GRID_STEP_PREFIX == custom_steps.GRID_STEP_PREFIX
    assert tuple(recipes.CUSTOM_DEFAULT_YEARS) == tuple(custom_steps._DEFAULT_YEARS)
    for template in custom_steps.TEMPLATES:
        assert template in catalog.templates()


def test_cohort_string_columns_are_one_table():
    assert cohort.FIPS_STR_COLS is recipes.COHORT_STR_COLUMNS
    assert ({k: tuple(v) for k, v in recipes.COHORT_STR_COLUMNS.items()}
            == {k: tuple(v) for k, v in cohort.FIPS_STR_COLS.items()})
    assert {r.cohort_variant for r in recipes.all_recipes().values()} <= set(cohort.VARIANTS)


def test_cohort_fixtures_use_the_package_catalog():
    import json
    from importlib import resources

    fix = REPO / "tests" / "fixtures" / "web_parity"
    assert not (fix / "variable_metadata.json").exists()
    recorded = json.loads((fix / "manifest.json").read_text())["web_files_sha256"]
    packaged = resources.files("spacescans.catalog").joinpath("variables.json").read_bytes()
    assert hashlib.sha256(packaged).hexdigest() == \
        recorded["backend/app/data/variable_metadata.json"]
    assert not (REPO / "tests" / "custom" / "templates").exists()


def test_manifest_catalog_and_custom_name_the_same_things():
    assert set(data.load_manifest().variables) == set(catalog.variables())
    assert set(data.boundary_datasets()) == set(custom_steps.BOUNDARY_C3)


def test_custom_polygon_steps_agree(tmp_path):
    _assert_custom_paths_agree(_polygon_definitions(tmp_path), tmp_path)


def test_custom_raster_steps_agree(tmp_path):
    defs = _polygon_definitions(tmp_path)
    raster = _raster_definition(tmp_path)
    defs[raster["variable_key"]] = raster
    _assert_custom_paths_agree(defs, tmp_path)


def test_coverage_takes_catalog_variables_and_custom_overlays(tmp_path):
    import pandas as pd

    from spacescans.orchestrate import compute_coverage

    df = pd.DataFrame({"pid": ["P1", "P1", "P2"],
                       "startDate": ["2015-01-01", "2016-01-01", "2020-01-01"],
                       "endDate": ["2015-12-31", "2016-12-31", "2021-06-30"],
                       "longitude": [-86.1, -86.2, -120.0], "latitude": [39.7, 39.8, 47.0]})
    as_objects = compute_coverage(df, ["ndi", "walkability"], catalog.variables())
    as_dicts = compute_coverage(df, ["ndi", "walkability"],
                                {k: v.raw for k, v in catalog.variables().items()})
    assert as_objects == as_dicts
    defs = _polygon_definitions(tmp_path)
    report = compute_coverage(df, list(defs), catalog.variables(overlay=defs))
    assert set(report["variables"]) == set(defs)
