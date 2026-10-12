"""plan()/recipe() reproduce each web runner: steps, rendered YAML, cache keys, merge tables."""
from __future__ import annotations

import copy
import dataclasses
import json
import pickle

import pytest

from spacescans import catalog
from spacescans.catalog import recipes
from spacescans.catalog.recipes import (
    COHORT_STR_COLUMNS,
    Recipe,
    Step,
    legacy_cache_key,
    plan,
    recipe,
)


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _plan_for(record, parity, helpers):
    return plan(record["experiment"], record["keys"],
                overlay=helpers.scenario_overlay(record, parity))


# ---------------------------------------------------------------------------
# against the golden file (what the web's runners did)
# ---------------------------------------------------------------------------

def test_plan_reproduces_the_web_runner(plan_record, web_parity, helpers):
    if "error" in plan_record:
        with pytest.raises(Exception) as info:
            _plan_for(plan_record, web_parity, helpers)
        assert _err(info.value) == plan_record["error"]
        return
    steps = _plan_for(plan_record, web_parity, helpers)
    got = [{"name": s.name, "template": s.template, "is_c3": s.is_c3} for s in steps]
    assert got == plan_record["steps"]


def test_rendered_yaml_matches_the_web(planned_record, web_parity, helpers):
    steps = _plan_for(planned_record, web_parity, helpers)
    for combo, hashes in planned_record["renders"].items():
        got = {}
        for step in steps:
            text = helpers.normalised_render(step, task_name=web_parity["task_name"],
                                             combo=combo, root=helpers.render_root)
            got[step.name] = helpers.sha256_text(text)
        assert got == hashes, f"{combo}: rendered config differs from the web's"


def test_cache_keys_match_the_web(planned_record, web_parity, helpers):
    steps = _plan_for(planned_record, web_parity, helpers)
    for combo, keys in planned_record["cache_keys"].items():
        buf, res = (int(x) for x in combo.split("/"))
        got = {s.name: legacy_cache_key(s, web_parity["input_sha256"], buffer_m=buf,
                                        raster_res_m=res)
               for s in steps if s.is_c3}
        assert got == keys, combo


def test_runner_tables_match_the_web(runner_name, web_parity, helpers):
    web = web_parity["runners"][runner_name]
    rec = recipe(runner_name)
    assert web["experiment_key"] == rec.experiment
    assert rec.output_map(web["merge_keys"]) == web["parquet_map"]
    assert list(COHORT_STR_COLUMNS[rec.cohort_variant]) == web["cohort_str_columns"]
    assert list(rec.episode_grouping_modules) == web["episode_grouping_modules"]
    # C3 cache meta: the "boundary" written and whether raster_res_m is
    c3_steps = {}
    for record in web_parity["plans"]:
        if record["experiment"] == runner_name and "steps" in record:
            for step in _plan_for(record, web_parity, helpers):
                if step.is_c3:
                    c3_steps.setdefault(step.name, step)
    assert set(c3_steps) == set(web["cache_meta"])
    for name, meta in web["cache_meta"].items():
        assert c3_steps[name].meta_boundary == meta["boundary"], name
        assert c3_steps[name].meta_raster_res == meta["raster_res_m"], name


def test_faqsd_rename_matches_the_web_rewrite(web_parity):
    rec = recipe("faqsd")
    before = ["PATID", "geoid", "o3", "pm25", "other"]
    after = [rec.renames.get(c, c) for c in before]
    assert after == web_parity["runners"]["faqsd"]["faqsd_columns_after_merge"]
    assert rec.rename_in_place
    assert [r.experiment for r in recipes.all_recipes().values() if r.renames or r.rename_in_place] == [
        "faqsd"]


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------

def test_one_recipe_per_runner_in_catalog_order():
    assert list(recipes.all_recipes()) == catalog.experiments() + ["custom"]
    assert len(recipes.all_recipes()) == 11


def test_recipes_accept_exactly_their_catalog_variables():
    groups = catalog.group(list(catalog.variables()))
    for name, rec in recipes.all_recipes().items():
        if rec.dynamic:
            assert rec.variables == ()
            continue
        assert list(rec.variables) == groups[name], name


def test_bindings_name_steps_that_run_earlier():
    for name, rec in recipes.all_recipes().items():
        if rec.dynamic:
            continue
        for key in rec.variables:
            seen = []
            for step in plan(name, [key]):
                for producer in step.bind.values():
                    assert producer in seen, f"{name}.{step.name} reads {producer} before it runs"
                seen.append(step.name)
            # the merge reads the last step's output
            assert rec.output_for(key) == f"{seen[-1]}.parquet"


def test_c3_before_c4_and_shared_c3_once():
    steps = plan("bg_ndi_wi", ["walkability", "ndi", "walkability"])
    assert [s.name for s in steps] == ["c3_bg", "c3_bg_2020", "c4_ndi", "c4_wi"]
    kinds = [s.kind for s in steps]
    assert kinds == sorted(kinds)                 # every c3 before every c4
    assert [s.name for s in plan("bg_ndi_wi", ["walkability"])] == ["c3_bg", "c4_wi"]


def test_shared_steps_are_the_same_object_across_runners():
    fara = recipe("fara_tract").steps["c3_tract_us"]
    assert recipe("faqsd").steps["c3_tract_us"] is fara
    # custom reuses the tag (cache sharing) but renders with its own time block
    custom = recipe("custom").steps["c3_tract_us"]
    assert (custom.template, custom.cache_tag, custom.uses_raster_res) == (
        fara.template, fara.cache_tag, fara.uses_raster_res)
    assert custom.time_block == "always" and fara.time_block == "if_present"
    assert recipe("custom").steps["c3_county_us"].cache_tag == recipe("zcta5_cbp").steps[
        "c3_county"].cache_tag == "COUNTY"


def test_cache_tags():
    tags = {s.name: s.cache_tag for r in recipes.all_recipes().values() for s in r.steps.values()
            if s.is_c3}
    assert tags == {
        "c3_bg": "BG", "c3_bg_2020": "BG2020", "c3_zcta5": "ZCTA5", "c3_county": "COUNTY",
        "c3_tiger_roads": "BG_TIGER2", "c3_nhd_bluespace": "BG_NHD", "c3_noise": "BG_NOISE",
        "c3_vnl": "BG_VNL1", "c3_temis": "BG_TEMIS", "c3_tract_us": "TRACT_FARA",
        "c3_acag": "BG_ACAG", "c3_county_us": "COUNTY",
    }
    raster = {s.name for r in recipes.all_recipes().values() for s in r.steps.values()
              if s.uses_raster_res}
    assert raster == {"c3_bg", "c3_bg_2020", "c3_zcta5", "c3_county", "c3_tract_us",
                      "c3_county_us"}


def test_custom_raster_steps():
    defs = {
        "custom_r": {"geometry": "raster", "grid_hash": "abcd1234", "join_col": "grid_id",
                     "key_col": "grid_id", "value_cols": ["v"], "year_col": None,
                     "rasters": [{"path": "/x/static.tif", "year": None}]},
    }
    c3, c4 = plan("custom", ["custom_r"], overlay=defs)
    assert (c3.name, c3.cache_tag, c3.uses_raster_res) == (
        "c3_customgrid_abcd1234", "CUSTOMGRID_abcd1234", False)
    assert c3.fill == {"source.file": "/x/static.tif", "buffer.grid_id_offset": 0}
    assert c4.materialise_values and c4.dataset == "custom_r"
    assert c4.bind == {"source.file": "c3_customgrid_abcd1234",
                       "exposure.file": "values_custom_r"}
    assert c4.fill["linkage_pattern"] == "static_areal"
    assert c4.fill["time.years"] == list(range(2013, 2020))
    assert recipe("custom").output_for("custom_r") == "c4_custom_r.parquet"


def test_legacy_cache_key_refuses_c4_and_formats_like_the_web():
    c3 = recipe("noise").steps["c3_noise"]
    assert legacy_cache_key(c3, "f" * 64, buffer_m=270, raster_res_m=25) == "ffffffff__BG_NOISE__b270m"
    bg = recipe("bg_ndi_wi").steps["c3_bg"]
    assert legacy_cache_key(bg, "0" * 64, buffer_m=270.0, raster_res_m=25) == (
        "00000000__BG__b270.0m__r25m")
    with pytest.raises(ValueError):
        legacy_cache_key(recipe("noise").steps["c4_noise"], "f" * 64, buffer_m=270, raster_res_m=25)


def test_plan_argument_errors():
    with pytest.raises(KeyError, match="no recipe for experiment 'nope'"):
        plan("nope", ["ndi"])
    with pytest.raises(TypeError):
        plan("bg_ndi_wi", "ndi")
    with pytest.raises(ValueError, match="at least one variable"):
        plan("bg_ndi_wi", None)


def test_recipe_data_is_read_only_but_copyable():
    rec = recipe("bg_ndi_wi")
    step = rec.steps["c4_ndi"]
    with pytest.raises(TypeError):
        step.bind["source.file"] = "elsewhere"
    with pytest.raises(TypeError):
        rec.steps["junk"] = step
    with pytest.raises(TypeError):
        rec.renames.update(a="b")
    with pytest.raises(dataclasses.FrozenInstanceError):
        step.name = "x"
    assert copy.deepcopy(step) == step
    assert pickle.loads(pickle.dumps(step)) == step
    assert pickle.loads(pickle.dumps(rec)) == rec
    assert dataclasses.asdict(step)["bind"] == dict(step.bind)
    json.dumps(step.to_dict())
    assert len({step, copy.deepcopy(step)}) == 1
    assert step.is_c3 is False and step.template_relpath == "c4/bg_ndi_demo.yaml"


def test_step_and_recipe_validation():
    with pytest.raises(ValueError):
        Step(name="x", template="c3/x.yaml", kind="c5", cache_tag="X")
    with pytest.raises(ValueError):
        Step(name="x", template="c3/x.yaml", kind="c3")            # C3 without a tag
    with pytest.raises(ValueError):
        Step(name="x", template="c4/x.yaml", kind="c4", cache_tag="X")
    with pytest.raises(ValueError):
        Step(name="x", template="c4/x.yaml", kind="c4", time_block="sometimes")
    c4 = Step(name="c4_x", template="c4/x.yaml", kind="c4")
    with pytest.raises(ValueError):
        Recipe(experiment="e", steps={"c4_x": c4}, needs={"x": ("c3_x",)},
               outputs={"x": "c4_x.parquet"})
    with pytest.raises(ValueError):
        Recipe(experiment="e", steps={"c4_x": c4}, needs={"x": ("c4_x",)}, outputs={})
    with pytest.raises(ValueError):
        Recipe(experiment="e", steps={"c4_x": c4}, needs={}, outputs={}, cohort_variant="nope")


# ---------------------------------------------------------------------------
# live: the web checkout still agrees with the golden file and with us
# ---------------------------------------------------------------------------

def _strip_texts(probe: dict) -> dict:
    out = copy.deepcopy(probe)
    for record in out["plans"]:
        record.pop("texts", None)
    return out


def test_live_web_matches_the_golden_file(live_probe, web_parity):
    live = _strip_texts(live_probe)
    if live["web_commit"] != web_parity["web_commit"]:
        pytest.fail(
            f"spacescans-web is at {live['web_commit']}, the catalog snapshot is "
            f"{web_parity['web_commit']}; a change there must land here too "
            "(regenerate tests/fixtures/catalog/web_parity.json with "
            "tests/catalog/web_probe.py) - or point SPACESCANS_WEB_REPO at that commit")
    assert live == web_parity


def test_live_rendered_text_matches(live_probe, helpers):
    import difflib

    for record in live_probe["plans"]:
        if "error" in record:
            continue
        steps = plan(record["experiment"], record["keys"],
                     overlay=helpers.scenario_overlay(record, live_probe))
        for combo, texts in record["texts"].items():
            for step in steps:
                mine = helpers.normalised_render(step, task_name=live_probe["task_name"],
                                                 combo=combo, root=helpers.render_root)
                web = texts[step.name]
                if mine != web:
                    diff = "".join(difflib.unified_diff(
                        web.splitlines(True), mine.splitlines(True), "web", "catalog"))
                    pytest.fail(f"{helpers.scenario_id(record)} {combo} {step.name}:\n{diff}")
