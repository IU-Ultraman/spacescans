"""The bundled ontology index: concept lookup and search (spacescans.catalog)."""
from __future__ import annotations

import importlib.util
import json
from importlib import resources
from pathlib import Path

import pytest

from spacescans import catalog
from spacescans.catalog import CatalogError

REPO = Path(__file__).resolve().parents[2]


def _index() -> dict:
    text = resources.files("spacescans.catalog").joinpath("ontology.json").read_text(encoding="utf-8")
    return json.loads(text)


def test_every_catalog_variable_concept_is_in_the_index():
    for var in catalog.variables().values():
        if var.ontology_id is not None:
            assert catalog.concept(var.ontology_id).id == var.ontology_id


def test_concept_lookup_gives_label_parents_and_path():
    pm = catalog.concept("000102_2")
    assert pm.label == "Particulate_Matter_PM2.5"
    assert pm.name == "Particulate Matter PM2.5"
    assert pm.parents == ("000096_2",)
    assert pm.path[0] == "Spatial_and_Contextual_Exposome"
    assert pm.path[-1] == "Particulate_Matter_PM2.5"
    assert "Natural_Environment_Exposome" in pm.path


def test_the_root_is_first_and_has_no_parents():
    first = next(iter(catalog.concepts().values()))
    assert first.id == catalog.ROOT_CONCEPT == "000093_2"
    assert first.parents == ()
    assert first.path == ("Spatial_and_Contextual_Exposome",)


def test_index_holds_concepts_not_the_catalog_result_columns():
    ids = list(catalog.concepts())
    assert not [i for i in ids if i.startswith("SPACESCANS_VC_")]
    assert "SPACESCANS_Walkability" in ids          # an extension concept stays


def test_every_parent_is_in_the_index():
    concepts = catalog.concepts()
    for c in concepts.values():
        assert all(p in concepts for p in c.parents), c.id


def test_unknown_concept_names_close_matches():
    with pytest.raises(CatalogError) as err:
        catalog.concept("PM2.5")
    message = str(err.value)
    assert "PM2.5" in message and "000102_2" in message
    with pytest.raises(CatalogError):
        catalog.concept("")


def test_search_ranks_label_matches_first():
    assert catalog.search_concepts("pm2.5")[0].id == "000102_2"
    assert "000102_2" in [c.id for c in catalog.search_concepts("PM25")]
    assert catalog.search_concepts("noise")[0].id == "000289"
    assert catalog.search_concepts("social environment")[0].id == "000295"
    assert catalog.search_concepts("000289")[0].id == "000289"      # an id finds itself


def test_search_limit_and_blank_text():
    assert len(catalog.search_concepts("air", limit=3)) == 3
    assert catalog.search_concepts("   ") == []
    assert catalog.search_concepts("no such concept anywhere") == []


def test_index_file_records_its_source():
    index = _index()
    assert index["schema_version"] == 1 and index["root"] == "000093_2"
    files = index["source"]["files"]
    assert set(files) == {"frontend/public/ontology/metadata.json",
                          "frontend/public/ontology/parents.json"}
    assert all(len(h) == 64 for h in files.values())


def test_index_matches_the_web_checkout(web_repo):
    """Regenerating from the sibling spacescans-web gives the shipped file (opt-in)."""
    spec = importlib.util.spec_from_file_location("build_ontology_index",
                                                  REPO / "tools" / "build_ontology_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rebuilt = module.build(web_repo)
    shipped = _index()
    assert rebuilt["concepts"] == shipped["concepts"]
    assert rebuilt["source"]["files"] == shipped["source"]["files"]
