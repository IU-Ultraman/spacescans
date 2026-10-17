"""The custom path's steps: planning, rendering, cache tags, raster values, diagnostics.

Ported from spacescans-web ``backend/tests/test_custom_runner.py`` (the cases
that are not web plumbing). What these pin, in order of how badly each would
bite:

  * output_grouping is always "episode". static_areal feeds it into
    DurationWeightedSpec.group_by_episode; the "patient" default collapses a
    patient's episodes into one row and the merge then has no geoid to rename
    to episode_id. ACAG and FAQSD each shipped this defect once.
  * the C3 cache tag equals the shipped recipe's for that boundary, so a custom
    Tract variable reuses FARA's cached weights instead of recomputing them.
  * static_areal vs yearly_areal is chosen by the presence of a year column.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import yaml

from spacescans import custom

TRACT_DEF = {
    "label": "My greenness",
    "boundary": "Tract",
    "join_col": "GEOID10",
    "key_col": "tract",
    "year_col": None,
    "value_cols": ["greenness", "heat"],
    "values_path": "/lib/user-1/ab12cd34/values.csv",
    "coverage_years": [2013, 2019],
    "experiment": "custom",
}
BG_DEF = {**TRACT_DEF, "boundary": "BG", "label": "My BG index",
          "value_cols": ["idx"], "values_path": "/lib/user-1/beefbeef/values.csv"}
YEARLY_DEF = {**TRACT_DEF, "year_col": "year", "value_cols": ["greenness"],
              "coverage_years": [2015, 2017], "values_path": "/lib/user-1/cafecafe/values.csv"}

A = "custom_ab12cd34"
B = "custom_beefbeef"
C = "custom_cafecafe"
R = "custom_raster01"


def _names(steps):
    return [s.name for s in steps]


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

def test_plan_emits_one_c3_then_one_c4_per_variable():
    steps = custom.plan([A], {A: TRACT_DEF})
    assert _names(steps) == ["c3_tract_us", f"c4_{A}"]
    assert [s.is_c3 for s in steps] == [True, False]
    # one Step model (catalog.Step): the C4 names its dataset and binds the C3 weights
    assert steps[1].dataset == A and steps[1].bind["source.file"] == "c3_tract_us"
    assert steps[1].template == steps[1].template_relpath == "c4/custom_areal.yaml"


def test_two_datasets_on_one_boundary_share_a_single_c3():
    other = f"{A}x"
    steps = custom.plan([A, other], {A: TRACT_DEF, other: {**TRACT_DEF, "label": "Second"}})
    assert _names(steps) == ["c3_tract_us", f"c4_{A}", f"c4_{other}"]


def test_datasets_on_different_boundaries_each_get_their_c3():
    assert _names(custom.plan([A, B], {A: TRACT_DEF, B: BG_DEF})) == \
        ["c3_tract_us", "c3_bg", f"c4_{A}", f"c4_{B}"]


def test_plan_reuses_the_shipped_c3_step_names_and_tags():
    """Sharing the step name and cache tag is what makes the C3 cache shared
    (fara_tract: c3_tract_us/TRACT_FARA; bg_ndi_wi: c3_bg/BG; zcta5_cbp: c3_zcta5/ZCTA5)."""
    assert custom.c3_step_for("Tract").name == "c3_tract_us"
    assert custom.c3_step_for("BG").name == "c3_bg"
    assert custom.c3_step_for("ZCTA5").name == "c3_zcta5"
    assert custom.c3_step_for("County").name == "c3_county_us"
    assert [custom.BOUNDARY_C3[b][2] for b in ("Tract", "BG", "ZCTA5", "County")] == \
        ["TRACT_FARA", "BG", "ZCTA5", "COUNTY"]
    for boundary, (_name, template, tag) in custom.BOUNDARY_C3.items():
        step = custom.c3_step_for(boundary)
        assert step.cache_tag == tag and step.uses_raster_res and step.template == template


def test_plan_rejects_an_empty_selection():
    with pytest.raises(ValueError, match="at least one variable"):
        custom.plan([], {})


def test_plan_rejects_a_variable_with_no_definition():
    with pytest.raises(ValueError, match="no definition"):
        custom.plan([A], {})


def test_plan_rejects_an_unsupported_boundary():
    with pytest.raises(ValueError, match="unsupported boundary"):
        custom.plan([A], {A: {**TRACT_DEF, "boundary": "Parcel"}})


def test_definitions_from_a_web_config():
    cfg = {"variables": [A], "custom_variables": {A: TRACT_DEF}}
    assert custom.definitions_from_config(cfg) == {A: TRACT_DEF}
    assert custom.definitions_from_config({}) == {}
    with pytest.raises(ValueError, match="not a mapping"):
        custom.definitions_from_config({"custom_variables": ["x"]})


def test_parquet_map_matches_the_c4_step_names():
    assert custom.parquet_map([A, C]) == {A: f"c4_{A}.parquet", C: f"c4_{C}.parquet"}


def test_every_template_the_path_renders_is_listed():
    from spacescans import catalog
    assert set(custom.TEMPLATES) == {
        "c3/tract_us_demo.yaml", "c3/bg_us_demo.yaml", "c3/zcta5_us_demo.yaml",
        "c3/county_us_demo.yaml", "c3/custom_grid.yaml", "c4/custom_areal.yaml"}
    assert set(custom.TEMPLATES) <= set(catalog.templates())


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

@pytest.fixture
def rendered(tmp_path, load_template):
    """Render a step against the real templates; return the config and the run dir."""
    def _render(step, variables, definitions, run_dir=None, buffer_m=270, raster_res_m=25,
                output_level="participant"):
        run_dir = run_dir or (tmp_path / "task-abcdef12")
        run_dir.mkdir(parents=True, exist_ok=True)
        ctx = custom.Context(run_dir=run_dir, cohort_parquet=run_dir / "input.parquet",
                             buffer_m=buffer_m, raster_res_m=raster_res_m,
                             name_suffix=f"_task_{run_dir.name[-8:]}", output_level=output_level)
        for _rec in custom.prepare_step(step, ctx, definitions=definitions):
            pass
        cfg = custom.render(step, load_template(step.template), ctx,
                            variables=variables, definitions=definitions)
        # What gets written must round-trip as YAML.
        assert yaml.safe_load(yaml.safe_dump(cfg, sort_keys=False)) == cfg
        return cfg, run_dir
    return _render


def test_c4_static_dataset_renders_static_areal(rendered):
    cfg, run_dir = rendered(custom.c4_step_for(A), [A], {A: TRACT_DEF})
    assert cfg["linkage_pattern"] == "static_areal"
    assert cfg["source"]["file"] == str(run_dir / "output" / "c3_tract_us.parquet")
    assert cfg["source"]["join_col"] == "GEOID10"
    assert cfg["exposure"]["file"] == TRACT_DEF["values_path"]
    assert cfg["exposure"]["join_col"] == "tract"
    assert cfg["exposure"]["value_cols"] == ["greenness", "heat"]
    assert "year_col" not in cfg["exposure"]
    assert "plugin" not in cfg          # no plugin -> generic read_table


def test_c4_yearly_dataset_renders_yearly_areal_with_its_years(rendered):
    cfg, _ = rendered(custom.c4_step_for(C), [C], {C: YEARLY_DEF})
    assert cfg["linkage_pattern"] == "yearly_areal"
    assert cfg["exposure"]["year_col"] == "year"
    assert cfg["time"]["years"] == [2015, 2016, 2017]
    assert cfg["time"]["temporal_resolution"] == cfg["time"]["temporal_mode"] == "yearly"


def test_c4_without_coverage_years_uses_the_shipped_window(rendered):
    d = {k: v for k, v in TRACT_DEF.items() if k != "coverage_years"}
    cfg, _ = rendered(custom.c4_step_for(A), [A], {A: d})
    assert cfg["time"]["years"] == list(range(2013, 2020))


def test_c4_always_sets_the_output_levels_grouping(rendered):
    for key, definition in ((A, TRACT_DEF), (C, YEARLY_DEF)):
        cfg, _ = rendered(custom.c4_step_for(key), [key], {key: definition},
                          output_level="episode")
        assert cfg["time"]["output_grouping"] == "episode"
        cfg, _ = rendered(custom.c4_step_for(key), [key], {key: definition})
        assert cfg["time"]["output_grouping"] == "patient"      # the participant level


def test_c3_gets_episode_grouping_and_the_task_buffer(rendered):
    cfg, run_dir = rendered(custom.c3_step_for("Tract"), [A], {A: TRACT_DEF},
                            buffer_m=500, raster_res_m=50, output_level="episode")
    assert cfg["buffer"]["buffer_m"] == 500
    # boundary_overlap_fast rasterises at this resolution, and it is part of
    # the cache key: the template's default must not survive.
    assert cfg["buffer"]["raster_res_m"] == 50
    assert cfg["buffer"]["patient_file"] == str(run_dir / "input.parquet")
    assert cfg["time"]["output_grouping"] == "episode"
    assert cfg["output"]["path"] == str(run_dir / "output" / "c3_tract_us.parquet")


def test_c3_source_stays_relative_for_the_data_dir(rendered):
    cfg, _ = rendered(custom.c3_step_for("Tract"), [A], {A: TRACT_DEF})
    files = cfg["source"]["file"]
    files = files if isinstance(files, list) else [files]
    assert all(not f.startswith("/") for f in files)


def test_render_names_the_config_after_the_task(rendered):
    cfg, _ = rendered(custom.c4_step_for(A), [A], {A: TRACT_DEF})
    assert cfg["name"] == "custom_areal_task_abcdef12"


def test_render_does_not_modify_the_template(tmp_path, load_template):
    template = load_template("c4/custom_areal.yaml")
    before = yaml.safe_dump(template, sort_keys=False)
    ctx = custom.Context(run_dir=tmp_path, cohort_parquet=tmp_path / "input.parquet")
    custom.render(custom.c4_step_for(A), template, ctx, variables=[A], definitions={A: TRACT_DEF})
    assert yaml.safe_dump(template, sort_keys=False) == before


def test_render_accepts_any_context_with_the_fields(tmp_path, load_template):
    """orchestrate.RunContext has more fields; render reads only these five."""
    from types import SimpleNamespace
    ctx = SimpleNamespace(run_dir=tmp_path, cohort_parquet=tmp_path / "c.parquet", buffer_m=270,
                          raster_res_m=25, name_suffix="", data_dir=Path("/data"))
    cfg = custom.render(custom.c3_step_for("BG"), load_template("c3/bg_us_demo.yaml"), ctx,
                        variables=[B], definitions={B: BG_DEF})
    assert cfg["name"] == "bg_us_demo" and cfg["buffer"]["patient_file"] == str(tmp_path / "c.parquet")


# --------------------------------------------------------------------------
# cache key (legacy-v1, the web's)
# --------------------------------------------------------------------------

def _parquet(tmp_path):
    p = tmp_path / "input.parquet"
    p.write_bytes(b"cohort")
    return p


def test_cache_key_has_the_shipped_polygon_format(tmp_path):
    """sha8 of the input parquet bytes, tag, buffer and raster resolution:
    what fara_tract and bg_ndi_wi compute, so the entries are shared."""
    import hashlib
    sha8 = hashlib.sha256(b"cohort").hexdigest()[:8]
    p = _parquet(tmp_path)
    assert custom.legacy_cache_key(p, "TRACT_FARA", buffer_m=270, raster_res_m=25) == \
        f"{sha8}__TRACT_FARA__b270m__r25m"
    assert custom.legacy_cache_key(p, "BG", buffer_m=270, raster_res_m=25) == f"{sha8}__BG__b270m__r25m"


def test_cache_key_differs_per_boundary_buffer_and_raster(tmp_path):
    p = _parquet(tmp_path)
    base = custom.legacy_cache_key(p, "TRACT_FARA", buffer_m=270, raster_res_m=25)
    assert base == custom.legacy_cache_key(p, "TRACT_FARA", buffer_m=270, raster_res_m=25)
    assert base != custom.legacy_cache_key(p, "BG", buffer_m=270, raster_res_m=25)
    assert base != custom.legacy_cache_key(p, "TRACT_FARA", buffer_m=500, raster_res_m=25)
    assert base != custom.legacy_cache_key(p, "TRACT_FARA", buffer_m=270, raster_res_m=270)


def test_a_polygon_key_needs_the_raster_resolution(tmp_path):
    with pytest.raises(ValueError, match="needs raster_res_m"):
        custom.legacy_cache_key(_parquet(tmp_path), "BG", buffer_m=270)


def test_boundary_tag_lookup_covers_every_c3_step():
    for boundary, (_name, _template, tag) in custom.BOUNDARY_C3.items():
        assert custom.boundary_tag_for_step(custom.c3_step_for(boundary)) == tag, boundary
    assert custom.boundary_tag_for_step(custom.c4_step_for(A)) is None


# --------------------------------------------------------------------------
# raster datasets
# --------------------------------------------------------------------------

def _raster_def(tmp_path, write_tif, *, years=(None,), grid_hash="abcd1234", col="ndvi",
                offset_per_year=100):
    """A raster definition backed by real small GeoTIFFs: value = row*10 + col
    (+ offset per year), so a pixel's value says which cell and year it is."""
    rasters = []
    for i, y in enumerate(years):
        arr = np.array([[r * 10 + c + i * offset_per_year for c in range(5)] for r in range(4)])
        p = write_tif(tmp_path / f"r_{y or 'static'}.tif", arr)
        rasters.append({"path": str(p), "year": y, "uploaded_filename": p.name,
                        "sha256": "x", "bytes": p.stat().st_size})
    return {
        "label": "My raster", "boundary": "Point", "geometry": "raster",
        "join_col": "grid_id", "key_col": "grid_id",
        "year_col": None if years == (None,) else "year",
        "value_cols": [col], "values_path": None, "rasters": rasters, "band": 1,
        "grid_hash": grid_hash,
        "grid": {"width": 5, "height": 4, "nodata": None,
                 "bounds_wgs84": [-85.0, 30.96, -84.95, 31.0]},
        "coverage_years": [2013, 2019] if years == (None,) else [min(years), max(years)],
        "experiment": "custom",
    }


def _c3_weights(run_dir, grid_ids, name="c3_customgrid_abcd1234"):
    import pandas as pd
    (run_dir / "output").mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"geoid": range(len(grid_ids)), "grid_id": grid_ids,
                  "weight": [1.0] * len(grid_ids)}).to_parquet(
        run_dir / "output" / f"{name}.parquet", index=False)


@pytest.mark.geo
def test_plan_gives_a_raster_its_own_c3_named_by_grid(tmp_path, write_tif):
    steps = custom.plan([R], {R: _raster_def(tmp_path, write_tif)})
    assert _names(steps) == ["c3_customgrid_abcd1234", f"c4_{R}"]
    assert steps[0].template == "c3/custom_grid.yaml"
    assert steps[0].cache_tag == "CUSTOMGRIDND_abcd1234" and not steps[0].uses_raster_res


@pytest.mark.geo
def test_two_rasters_on_one_grid_share_the_c3(tmp_path, write_tif):
    other = f"{R}b"
    defs = {R: _raster_def(tmp_path, write_tif), other: _raster_def(tmp_path, write_tif, col="heat")}
    assert _names(custom.plan([R, other], defs)) == \
        ["c3_customgrid_abcd1234", f"c4_{R}", f"c4_{other}"]


@pytest.mark.geo
def test_raster_and_polygon_get_separate_c3s(tmp_path, write_tif):
    defs = {R: _raster_def(tmp_path, write_tif), A: TRACT_DEF}
    assert _names(custom.plan([R, A], defs)) == \
        ["c3_customgrid_abcd1234", "c3_tract_us", f"c4_{R}", f"c4_{A}"]


@pytest.mark.geo
def test_grid_cache_tag_and_key_have_no_rasterisation_suffix(tmp_path, write_tif):
    """grid_weights does not rasterise the buffer; like temis/vnl, no __r part."""
    step = custom.c3_step_for(_raster_def(tmp_path, write_tif))
    assert custom.boundary_tag_for_step(step) == "CUSTOMGRIDND_abcd1234"
    p = _parquet(tmp_path)
    key = custom.legacy_cache_key(p, "CUSTOMGRIDND_abcd1234", buffer_m=270, raster_res_m=25)
    assert key.split("__")[1:] == ["CUSTOMGRIDND_abcd1234", "b270m"]
    assert key != custom.legacy_cache_key(p, "CUSTOMGRIDND_ffff0000", buffer_m=270)


@pytest.mark.geo
def test_c3_raster_render_points_at_the_uploaded_tif(rendered, tmp_path, write_tif):
    d = _raster_def(tmp_path, write_tif)
    cfg, run_dir = rendered(custom.c3_step_for(d), [R], {R: d})
    assert cfg["linkage_pattern"] == "grid_weights"
    assert cfg["source"]["file"] == d["rasters"][0]["path"]
    assert cfg["buffer"]["grid_id_offset"] == 0
    assert cfg["buffer"]["patient_file"] == str(run_dir / "input.parquet")
    # grid_weights does not rasterise the buffer; the polygon-only injection
    # must not leak into a grid step (it would also be a silent cache-key lie).
    assert "raster_res_m" not in cfg["buffer"]
    assert cfg["output"]["path"] == str(run_dir / "output" / "c3_customgrid_abcd1234.parquet")


def test_c3_raster_render_needs_a_raster_in_the_selection(tmp_path, load_template):
    ctx = custom.Context(run_dir=tmp_path, cohort_parquet=tmp_path / "input.parquet")
    step = custom.Step(name="c3_customgrid_ffff0000", template=custom.C3_GRID_TEMPLATE, kind="c3",
                       cache_tag="CUSTOMGRID_ffff0000")
    with pytest.raises(ValueError, match="no raster dataset in the selection renders"):
        custom.render(step, load_template(step.template), ctx, variables=[A],
                      definitions={A: TRACT_DEF})


@pytest.mark.geo
def test_c4_raster_render_materialises_the_value_table(rendered, tmp_path, write_tif):
    """The pixels under the C3 grid ids become (grid_id, col); C4 then reads
    that parquet with no plugin, exactly like a polygon CSV."""
    import pandas as pd
    d = _raster_def(tmp_path, write_tif)
    run_dir = tmp_path / "task-abcdef12"
    _c3_weights(run_dir, [0, 4, 13, 19])                 # (0,0) (0,4) (2,3) (3,4)
    cfg, _ = rendered(custom.c4_step_for(R), [R], {R: d}, run_dir=run_dir)
    assert cfg["linkage_pattern"] == "static_areal"
    assert cfg["source"] == {"file": str(run_dir / "output" / "c3_customgrid_abcd1234.parquet"),
                             "join_col": "grid_id"}
    assert cfg["exposure"]["join_col"] == "grid_id"
    assert cfg["exposure"]["value_cols"] == ["ndvi"]
    assert cfg["exposure"]["file"] == str(run_dir / "output" / f"values_{R}.parquet")
    assert "plugin" not in cfg
    values = pd.read_parquet(cfg["exposure"]["file"])
    assert values.sort_values("grid_id")["ndvi"].tolist() == [0.0, 4.0, 23.0, 34.0]
    assert "year" not in values.columns


@pytest.mark.geo
def test_prepare_step_returns_the_webs_log_line(tmp_path, write_tif):
    d = _raster_def(tmp_path, write_tif, years=(2016, 2017))
    run_dir = tmp_path / "task-abcdef12"
    _c3_weights(run_dir, [13, 13, 4])
    ctx = custom.Context(run_dir=run_dir, cohort_parquet=run_dir / "input.parquet")
    records = custom.prepare_step(custom.c4_step_for(R), ctx, definitions={R: d})
    assert records == [{"level": "info", "source": f"c4_{R}", "msg":
                        "materialised 4 (grid_id, year) rows from 2 raster(s), band(s) 1; "
                        "nodata per column: ndvi 0"}]
    assert custom.prepare_step(custom.c3_step_for(d), ctx, definitions={R: d}) == []
    assert custom.prepare_step(custom.c4_step_for(A), ctx, definitions={A: TRACT_DEF}) == []


@pytest.mark.geo
def test_c4_yearly_raster_render_stacks_one_table_per_year(rendered, tmp_path, write_tif):
    import pandas as pd
    d = _raster_def(tmp_path, write_tif, years=(2016, 2017))
    run_dir = tmp_path / "task-abcdef12"
    _c3_weights(run_dir, [13])
    cfg, _ = rendered(custom.c4_step_for(R), [R], {R: d}, run_dir=run_dir)
    assert cfg["linkage_pattern"] == "yearly_areal"
    assert cfg["exposure"]["year_col"] == "year"
    assert cfg["time"]["years"] == [2016, 2017]
    values = pd.read_parquet(cfg["exposure"]["file"]).sort_values("year")
    assert values[["year", "ndvi"]].values.tolist() == [[2016, 23.0], [2017, 123.0]]


def _multi_band_def(tmp_path, write_tif, *, years=(None,)):
    """Three bands per file: band b holds row*10 + col + 1000*(b-1)
    (+ 100 per year), so a value says which cell, band and year it came from."""
    d = _raster_def(tmp_path, write_tif, years=years)
    for i, r in enumerate(d["rasters"]):
        base = np.array([[rr * 10 + c + i * 100 for c in range(5)] for rr in range(4)]).astype(float)
        stack = np.stack([base, base + 1000, base + 2000])
        stack[2, 0, 0] = -9999                           # nodata in band 3 only
        write_tif(r["path"], stack, nodata=-9999)
    d.update({"value_cols": ["no2", "pm25"], "bands": [3, 1], "band": 3,
              "band_nodata": [-9999.0, -9999.0]})
    return d


@pytest.mark.geo
def test_c4_multi_band_raster_writes_one_column_per_band(rendered, tmp_path, write_tif):
    import pandas as pd
    d = _multi_band_def(tmp_path, write_tif)
    run_dir = tmp_path / "task-abcdef12"
    _c3_weights(run_dir, [0, 4, 13])
    cfg, _ = rendered(custom.c4_step_for(R), [R], {R: d}, run_dir=run_dir)
    assert cfg["exposure"]["value_cols"] == ["no2", "pm25"]
    values = pd.read_parquet(cfg["exposure"]["file"]).sort_values("grid_id")
    assert list(values.columns) == ["grid_id", "no2", "pm25"]
    assert np.isnan(values["no2"].iloc[0])                  # band 3 nodata at (0,0)
    assert values["no2"].iloc[1:].tolist() == [2004.0, 2023.0]
    assert values["pm25"].tolist() == [0.0, 4.0, 23.0]      # band 1 keeps its (0,0)


@pytest.mark.geo
def test_c4_yearly_multi_band_raster_stacks_every_band_per_year(rendered, tmp_path, write_tif):
    import pandas as pd
    d = _multi_band_def(tmp_path, write_tif, years=(2016, 2017))
    run_dir = tmp_path / "task-abcdef12"
    _c3_weights(run_dir, [13])
    cfg, _ = rendered(custom.c4_step_for(R), [R], {R: d}, run_dir=run_dir)
    values = pd.read_parquet(cfg["exposure"]["file"]).sort_values("year")
    assert values[["year", "no2", "pm25"]].values.tolist() == [
        [2016, 2023.0, 23.0], [2017, 2123.0, 123.0]]


@pytest.mark.geo
def test_a_definition_from_before_multi_band_reads_as_one_band(tmp_path, write_tif):
    d = _raster_def(tmp_path, write_tif)
    d.pop("bands", None)
    d.pop("band_nodata", None)
    d["grid"]["nodata"] = -5.0
    assert custom.raster_bands(d) == (["ndvi"], [1], [-5.0])


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------

def _diagnosis_setup(tmp_path, touched, covered):
    """A run whose C3 weights touched ``touched`` and whose CSV covers ``covered``."""
    import pandas as pd
    run_dir = tmp_path / "task-diag0001"
    (run_dir / "output").mkdir(parents=True)
    pd.DataFrame({"geoid": range(len(touched)), "GEOID10": touched,
                  "value": [1.0] * len(touched)}).to_parquet(
        run_dir / "output" / "c3_tract_us.parquet", index=False)
    values = tmp_path / "values.csv"
    values.write_text("tract,v\n" + "".join(f"{c},0.5\n" for c in covered))
    return run_dir, {**TRACT_DEF, "values_path": str(values)}


def test_diagnosis_names_the_regions_when_nothing_overlaps(tmp_path):
    """The user's actual report: a Leon County file against a nationwide cohort,
    "0.0% linked" and no idea why."""
    touched = ["06037101110", "36061000100", "39035101100", "48201100000"]
    run_dir, definition = _diagnosis_setup(tmp_path, touched, ["12073000200", "12073000301"])
    msg = custom.coverage_diagnosis(run_dir, A, definition)
    assert "touch 4 distinct Tract codes across 4 states" in msg
    assert "06 (1)" in msg and "36 (1)" in msg
    assert "covers 2 codes across 1 state: 12 (2)" in msg
    assert "0 in common" in msg
    assert "does not cover where this cohort lives" in msg


def test_diagnosis_reports_partial_overlap(tmp_path):
    touched = ["12073000200", "12073000301", "06037101110"]
    run_dir, definition = _diagnosis_setup(tmp_path, touched, ["12073000200", "12073000301"])
    msg = custom.coverage_diagnosis(run_dir, A, definition)
    assert "2 in common" in msg
    assert "outside the file's geographies get no value" in msg


def test_diagnosis_never_raises(tmp_path):
    run_dir = tmp_path / "task-nofiles"
    run_dir.mkdir()
    msg = custom.coverage_diagnosis(run_dir, A, {**TRACT_DEF, "values_path": "/nope.csv"})
    assert msg.startswith(f"{A}: could not compare geographies")


@pytest.mark.geo
def test_raster_diagnosis_reports_nodata_share(tmp_path, write_tif):
    import pandas as pd
    d = _raster_def(tmp_path, write_tif)
    run_dir = tmp_path / "task-diag"
    (run_dir / "output").mkdir(parents=True)
    pd.DataFrame({"grid_id": [1, 2, 3, 4], "ndvi": [1.0, np.nan, np.nan, np.nan]}).to_parquet(
        run_dir / "output" / f"values_{R}.parquet", index=False)
    msg = custom.coverage_diagnosis(run_dir, R, d)
    assert "75% of the 4 raster cells" in msg
    assert "has no data where this cohort lives" in msg


@pytest.mark.geo
def test_raster_diagnosis_reports_each_columns_nodata_share(tmp_path, write_tif):
    import pandas as pd
    d = _multi_band_def(tmp_path, write_tif)
    run_dir = tmp_path / "task-diag"
    (run_dir / "output").mkdir(parents=True)
    pd.DataFrame({"grid_id": [1, 2, 3, 4], "no2": [1.0, np.nan, np.nan, np.nan],
                  "pm25": [1.0, 2.0, 3.0, np.nan]}).to_parquet(
        run_dir / "output" / f"values_{R}.parquet", index=False)
    msg = custom.coverage_diagnosis(run_dir, R, d)
    assert "no2 75%, pm25 25%" in msg and "4 raster cells" in msg
    assert "has no data where this cohort lives" in msg


def test_diagnostics_report_the_linkage_rate_and_diagnose_low_columns(tmp_path):
    import pandas as pd
    run_dir, definition = _diagnosis_setup(tmp_path, ["06037101110"], ["12073000200"])
    pd.DataFrame({"PATID": ["P1", "P1", "P2"], "geoid": [0, 1, 2],
                  "greenness": [0.4, np.nan, np.nan], "other": [1, 2, 3]}).to_parquet(
        run_dir / "output" / f"c4_{A}.parquet", index=False)
    records = custom.diagnostics(run_dir, [A, C], {A: definition, C: YEARLY_DEF})
    assert [r["source"] for r in records] == ["merge"] * 3
    assert records[0] == {"level": "warning", "source": "merge",
                          "msg": f"{A}.greenness: 33.3% of episodes linked (1/3)"}
    assert records[1] == {"level": "warning", "source": "merge",
                          "msg": f"{A}: value column 'heat' missing from output"}
    assert records[2]["level"] == "warning" and "0 in common" in records[2]["msg"]


def test_diagnostics_at_full_linkage_are_info_only(tmp_path):
    import pandas as pd
    run_dir = tmp_path / "run"
    (run_dir / "output").mkdir(parents=True)
    pd.DataFrame({"geoid": [0, 1], "greenness": [0.4, 0.5], "heat": [80.0, 81.0]}).to_parquet(
        run_dir / "output" / f"c4_{A}.parquet", index=False)
    records = custom.diagnostics(run_dir, [A], {A: TRACT_DEF})
    assert [r["msg"] for r in records] == [f"{A}.greenness: 100.0% of episodes linked (2/2)",
                                           f"{A}.heat: 100.0% of episodes linked (2/2)"]
    assert {r["level"] for r in records} == {"info"}


# --------------------------------------------------------------------------
# provisioned boundaries
# --------------------------------------------------------------------------

def test_provisioned_boundaries_marks_missing_data_unavailable(tmp_path):
    root = tmp_path / "data-root"
    (root / "TRACT").mkdir(parents=True)
    (root / "TRACT" / "something.shp").write_text("x")
    (root / "BG").mkdir()                       # present but empty -> unprovisioned
    (root / "ZCTA5").mkdir()
    (root / "ZCTA5" / ".gitkeep").write_text("")  # only the skeleton -> unprovisioned
    by_boundary = {b["boundary"]: b for b in custom.provisioned_boundaries(root)}
    assert by_boundary["Tract"]["available"] is True
    assert by_boundary["BG"]["available"] is False
    assert by_boundary["ZCTA5"]["available"] is False
    assert by_boundary["County"]["available"] is False   # dir absent entirely
    assert by_boundary["ZCTA5"]["join_col"] == "ZCTA5CE10"
    assert by_boundary["Tract"]["key_len"] == 11
