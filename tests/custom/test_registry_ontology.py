"""Registered datasets annotated with ontology concepts (dataset and variable level).

A dataset's ``ontology_id`` is the field a catalog entry carries (the web places catalog
entries in its ontology tree by it); each variable may carry its own concept when the
variables measure different things. Both are checked against the bundled index
(``spacescans.catalog.concept``) before anything is stored.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spacescans import catalog, custom
from spacescans.custom.definition import check_definition

PM25 = "000102_2"           # Particulate_Matter_PM2.5
NATURAL = "000094_2"        # Natural_Environment_Exposome
SOCIAL = "000295"           # Social_Environment_Exposome


@pytest.fixture
def root(tmp_path):
    return tmp_path / "datasets"


@pytest.fixture
def table(tmp_path) -> Path:
    path = tmp_path / "county.csv"
    path.write_text("GEOID10,pm25,unemployment\n18097000100,1.5,4.0\n18097000200,2.0,5.5\n",
                    encoding="utf-8")
    return path


def _register(path, root, **kw):
    kw.setdefault("variables", ["pm25", "unemployment"])
    return custom.register(path, name="County measures", kind="polygon_table", temporal="static",
                           key={"boundary": "tract", "column": "GEOID10"}, root=root, **kw)


def test_dataset_and_variable_concepts_are_recorded(table, root):
    rec = _register(table, root, metadata={"ontology_id": catalog.ROOT_CONCEPT},
                    variables=[{"name": "pm25", "unit": "ug/m3", "ontology_id": PM25},
                               {"name": "unemployment", "ontology_id": SOCIAL}])
    assert rec.ontology_id == catalog.ROOT_CONCEPT
    assert rec.variables == [
        {"name": "pm25", "unit": "ug/m3", "description": None, "ontology_id": PM25},
        {"name": "unemployment", "unit": None, "description": None, "ontology_id": SOCIAL}]
    stored = json.loads((rec.directory / "dataset.json").read_text())
    assert stored["ontology_id"] == catalog.ROOT_CONCEPT
    assert stored["variables"][0]["ontology_id"] == PM25
    assert list(stored).index("ontology_id") == list(stored).index("spacescans_version") + 1
    assert custom.get_dataset("county measures", root) == rec
    assert custom.DatasetRecord.from_dict(rec.to_dict(), root=root) == rec


def test_the_definition_and_its_catalog_entry_carry_the_concepts(table, root):
    rec = _register(table, root, metadata={"ontology_id": catalog.ROOT_CONCEPT},
                    variables=[{"name": "pm25", "ontology_id": PM25}, "unemployment"])
    definition = rec.definition()
    assert definition["ontology_id"] == catalog.ROOT_CONCEPT
    # a variable without its own concept takes the dataset's
    assert definition["value_ontology_ids"] == {"pm25": PM25,
                                                "unemployment": catalog.ROOT_CONCEPT}
    entry = custom.catalog_entry(definition)
    assert entry["ontology_id"] == catalog.ROOT_CONCEPT
    catalog.validate_payload({"schema_version": 1, "variables": {rec.variable_key: entry}})


def test_a_dataset_without_concepts_defines_no_ontology_id(table, root):
    rec = _register(table, root)
    assert rec.ontology_id is None
    assert all("ontology_id" not in v for v in rec.variables)
    definition = rec.definition()
    assert "ontology_id" not in definition
    assert definition["value_ontology_ids"] == {}
    assert "ontology_id" not in custom.catalog_entry(definition)


def test_variable_concepts_alone_need_no_dataset_concept(table, root):
    rec = _register(table, root, variables=[{"name": "pm25", "ontology_id": PM25},
                                            "unemployment"])
    definition = rec.definition()
    assert "ontology_id" not in definition
    assert definition["value_ontology_ids"] == {"pm25": PM25}


@pytest.mark.parametrize("bad", ["PM2.5", "SPACESCANS_VC_pm25", "000296"])
def test_unknown_dataset_concepts_are_refused_and_nothing_is_stored(table, root, bad):
    with pytest.raises(custom.DatasetError, match="ontology concept"):
        _register(table, root, metadata={"ontology_id": bad})
    assert custom.list_datasets(root) == []


def test_an_unknown_concept_names_close_matches(table, root):
    with pytest.raises(custom.DatasetError) as err:
        _register(table, root, metadata={"ontology_id": "PM2.5"})
    assert PM25 in str(err.value)


def test_unknown_variable_concepts_are_refused(table, root):
    with pytest.raises(custom.DatasetError, match="pm25.*ontology concept"):
        _register(table, root, variables=[{"name": "pm25", "ontology_id": "nope"}])
    with pytest.raises(custom.DatasetError, match="must be a string"):
        _register(table, root, metadata={"ontology_id": 102})
    assert custom.list_datasets(root) == []


def test_a_blank_concept_is_no_concept(table, root):
    rec = _register(table, root, metadata={"ontology_id": "  "},
                    variables=[{"name": "pm25", "ontology_id": ""}, "unemployment"])
    assert rec.ontology_id is None
    assert "ontology_id" not in rec.variables[0]


def test_concept_ids_are_trimmed(table, root):
    rec = _register(table, root, metadata={"ontology_id": f" {PM25} "},
                    variables=[{"name": "pm25", "ontology_id": f"{PM25}\n"}, "unemployment"])
    assert rec.ontology_id == PM25
    assert rec.variables[0]["ontology_id"] == PM25


def test_update_sets_changes_and_clears_concepts(table, root):
    rec = _register(table, root)
    rec = custom.update_dataset(rec, metadata={"ontology_id": NATURAL})
    assert rec.ontology_id == NATURAL
    rec = custom.update_dataset(rec, variables=[{"name": "pm25", "ontology_id": PM25}])
    assert rec.variables[0]["ontology_id"] == PM25
    assert rec.definition()["value_ontology_ids"] == {"pm25": PM25, "unemployment": NATURAL}
    rec = custom.update_dataset(rec, metadata={"ontology_id": None},
                                variables=[{"name": "pm25", "ontology_id": None}])
    assert rec.ontology_id is None
    assert "ontology_id" not in rec.variables[0]
    with pytest.raises(custom.DatasetError, match="ontology concept"):
        custom.update_dataset(rec, metadata={"ontology_id": "nope"})
    with pytest.raises(custom.DatasetError, match="ontology concept"):
        custom.update_dataset(rec, variables=[{"name": "pm25", "ontology_id": "nope"}])


def test_an_r_style_definition_round_trip_keeps_the_concepts(table, root):
    rec = _register(table, root, variables=[{"name": "pm25", "ontology_id": PM25},
                                            "unemployment"])
    definition = json.loads(json.dumps(rec.definition()))
    assert check_definition(definition)["value_ontology_ids"] == {"pm25": PM25}
    empty = dict(definition, value_ontology_ids=[])      # jsonlite writes {} as []
    assert check_definition(empty)["value_ontology_ids"] == {}
