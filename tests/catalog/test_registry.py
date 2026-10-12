"""spacescans.catalog variables/variable/group/experiments against the web registry."""
from __future__ import annotations

import copy
import hashlib
import json
from importlib import resources

import pytest

from spacescans import catalog
from spacescans.catalog import CatalogError, validate_payload


def _sha(name: str) -> str:
    return hashlib.sha256(resources.files("spacescans.catalog").joinpath(name).read_bytes()).hexdigest()


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def test_catalog_files_are_byte_copies_of_the_web(web_parity):
    files = web_parity["files"]
    assert _sha("variables.json") == files["variable_metadata.json"]
    assert _sha("variables.schema.json") == files["variable_metadata.schema.json"]
    assert _sha("features.json") == files["feature_dictionary.json"]


def test_payload_equals_the_web_load_variables(web_parity):
    got = catalog.payload()
    want = web_parity["registry"]["payload"]
    assert got == want
    # same order too (the dispatcher groups in catalog file order)
    assert json.dumps(got) == json.dumps(want)


def test_overlay_payload_equals_the_web_task_overlay(web_parity):
    got = catalog.payload(overlay=web_parity["custom_definitions"])
    want = web_parity["registry"]["overlay_payload"]
    assert json.dumps(got) == json.dumps(want)


def test_variables_wrap_every_entry_in_order(web_parity):
    entries = web_parity["registry"]["payload"]["variables"]
    got = catalog.variables()
    assert list(got) == list(entries)
    for key, var in got.items():
        entry = entries[key]
        assert var.key == key
        assert dict(var.raw) == entry
        assert var.experiment == entry["experiment"]
        assert var.label == entry["label"]
        assert var.boundary == entry["boundary"]
        assert var.value_cols == tuple(entry["value_cols"])
        assert var.coverage_years == tuple(entry["coverage_years"])
        assert var.spatial_method == entry.get("spatial_method")
        assert var.ontology_id == entry.get("ontology_id")
        assert var.temporal == entry.get("temporal")


def test_variables_with_overlay_append_custom_entries(web_parity):
    overlay = web_parity["custom_definitions"]
    got = catalog.variables(overlay=overlay)
    assert list(got) == list(web_parity["registry"]["payload"]["variables"]) + list(overlay)
    rs = got["custom_rs"]
    assert rs.experiment == "custom" and rs.ontology_id is None
    assert rs.raw["grid_hash"] == "g1aaaa11"   # private manifest fields survive


def test_variable_lookup_and_unknown_key():
    ndi = catalog.variable("ndi")
    assert ndi.experiment == "bg_ndi_wi" and ndi.boundary == "BG" and ndi.value_cols == ("ndi",)
    with pytest.raises(KeyError) as info:
        catalog.variable("does_not_exist")
    assert info.value.args == ("does_not_exist",)
    assert catalog.variable("custom_x", overlay={"custom_x": {"experiment": "custom"}}).key == "custom_x"


def test_experiments_equal_the_web_list(web_parity):
    assert catalog.experiments() == web_parity["registry"]["experiments"]
    assert catalog.experiments(overlay=web_parity["custom_definitions"]) == (
        web_parity["registry"]["experiments"] + ["custom"])


def test_group_equals_the_web_variables_by_experiment(group_record, web_parity):
    overlay = web_parity["custom_definitions"] if group_record["overlay"] else None
    if "error" in group_record:
        with pytest.raises(KeyError) as info:
            catalog.group(group_record["keys"], overlay=overlay)
        assert _err(info.value) == group_record["error"]
    else:
        got = catalog.group(group_record["keys"], overlay=overlay)
        assert json.dumps(got) == json.dumps(group_record["result"])


def test_group_rejects_a_bare_string():
    with pytest.raises(TypeError):
        catalog.group("ndi")


def test_returned_objects_are_copies():
    var = catalog.variable("ndi")
    var.raw["value_cols"].append("junk")
    catalog.payload()["variables"]["ndi"]["label"] = "junk"
    assert catalog.variable("ndi").raw["value_cols"] == ["ndi"]
    assert catalog.variable("ndi").label != "junk"
    assert len({catalog.variable("ndi"), catalog.variable("ndi")}) == 1   # hashable


def test_overlay_must_be_a_mapping_of_mappings():
    with pytest.raises(CatalogError):
        catalog.variables(overlay=["custom_a"])
    with pytest.raises(CatalogError):
        catalog.variables(overlay={"custom_a": "not a definition"})


# ---------------------------------------------------------------------------
# validation: the stdlib validator agrees with jsonschema on the schema
# ---------------------------------------------------------------------------

def _shipped() -> dict:
    return json.loads(resources.files("spacescans.catalog").joinpath("variables.json").read_text())


def _mutations():
    def entry(fn):
        def apply(p):
            fn(p["variables"]["ndi"])
        return apply

    yield "ok", lambda p: None
    yield "missing label", entry(lambda e: e.pop("label"))
    yield "unknown field", entry(lambda e: e.update(extra=1))
    yield "bad boundary", entry(lambda e: e.update(boundary="Parcel"))
    yield "boundary not str", entry(lambda e: e.update(boundary=5))
    yield "bad region", entry(lambda e: e.update(coverage_region="EU"))
    yield "bad spatial_method", entry(lambda e: e.update(spatial_method="raster"))
    yield "bad temporal", entry(lambda e: e.update(temporal="daily"))
    yield "bad variable_type", entry(lambda e: e.update(variable_type="binary"))
    yield "label too long", entry(lambda e: e.update(label="x" * 81))
    yield "label at limit", entry(lambda e: e.update(label="x" * 80))
    yield "empty label", entry(lambda e: e.update(label=""))
    yield "description too long", entry(lambda e: e.update(description="d" * 401))
    yield "one year", entry(lambda e: e.update(coverage_years=[2012]))
    yield "three years", entry(lambda e: e.update(coverage_years=[2012, 2013, 2014]))
    yield "year too early", entry(lambda e: e.update(coverage_years=[1899, 2000]))
    yield "year as string", entry(lambda e: e.update(coverage_years=["2012", 2013]))
    yield "year as integral float", entry(lambda e: e.update(coverage_years=[2012.0, 2013]))
    yield "year as fraction", entry(lambda e: e.update(coverage_years=[2012.5, 2013]))
    yield "year as bool", entry(lambda e: e.update(coverage_years=[True, 2013]))
    yield "no value cols", entry(lambda e: e.update(value_cols=[]))
    yield "empty value col", entry(lambda e: e.update(value_cols=[""]))
    yield "value cols not list", entry(lambda e: e.update(value_cols="ndi"))
    yield "unit non-ascii", entry(lambda e: e.update(display_unit="µg/m³"))
    yield "unit empty", entry(lambda e: e.update(display_unit=""))
    yield "unit too long", entry(lambda e: e.update(display_unit="u" * 51))
    yield "experiment pattern", entry(lambda e: e.update(experiment="Bg-NDI"))
    yield "empty ontology id", entry(lambda e: e.update(ontology_id=""))
    yield "empty data source", entry(lambda e: e.update(data_source=""))
    yield "entry not object", lambda p: p["variables"].update(ndi=[1])
    yield "bad key", lambda p: p["variables"].update({"Bad-Key": p["variables"]["walkability"]})
    yield "no variables", lambda p: p.update(variables={})
    yield "extra top-level", lambda p: p.update(extra=True)
    yield "schema version 2", lambda p: p.update(schema_version=2)
    yield "schema version str", lambda p: p.update(schema_version="1")
    yield "missing schema version", lambda p: p.pop("schema_version")


@pytest.mark.parametrize("name,mutate", list(_mutations()), ids=[n for n, _ in _mutations()])
def test_validator_agrees_with_jsonschema(name, mutate):
    jsonschema = pytest.importorskip("jsonschema")
    payload = _shipped()
    mutate(payload)
    try:
        jsonschema.validate(copy.deepcopy(payload), catalog.schema())
        schema_ok = True
    except jsonschema.ValidationError:
        schema_ok = False
    try:
        validate_payload(payload)
        ours_ok = True
    except CatalogError:
        ours_ok = False
    assert ours_ok == schema_ok, f"{name}: jsonschema says {schema_ok}, validator says {ours_ok}"


def test_unknown_experiment_is_rejected():
    payload = _shipped()
    payload["variables"]["ndi"]["experiment"] = "no_such_runner"
    with pytest.raises(CatalogError, match="unknown experiment 'no_such_runner'"):
        validate_payload(payload)
    validate_payload(payload, experiments=["no_such_runner", "bg_ndi_wi", "zcta5_cbp",
                                           "tiger_proximity", "nhd_bluespace", "noise", "vnl",
                                           "temis", "fara_tract", "acag", "faqsd"])
