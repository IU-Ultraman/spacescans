"""The 25 packaged templates are byte-for-byte the web's configs/ (minus 3 unused)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from spacescans import catalog
from spacescans.catalog import recipes

USED = {
    "c3/acag_grid_demo.yaml", "c3/bg_us_2020_demo.yaml", "c3/bg_us_demo.yaml",
    "c3/county_us_demo.yaml", "c3/custom_grid.yaml", "c3/nhd_demo.yaml",
    "c3/noise_grid_demo.yaml", "c3/temis_grid_demo.yaml", "c3/tiger_roads_demo.yaml",
    "c3/tract_us_demo.yaml", "c3/vnl_grid_demo.yaml", "c3/zcta5_us_demo.yaml",
    "c4/acag_demo.yaml", "c4/bg_ndi_demo.yaml", "c4/bg_wi_demo.yaml",
    "c4/county_cbp_demo.yaml", "c4/custom_areal.yaml", "c4/nhd_bluespace_demo.yaml",
    "c4/noise_demo.yaml", "c4/temis_demo.yaml", "c4/tiger_roads_demo.yaml",
    "c4/tract_faqsd_demo.yaml", "c4/tract_fara_demo.yaml", "c4/vnl_demo.yaml",
    "c4/zcta5_cbp_demo.yaml",
}
UNUSED = {"c3/bg_fl_leon_demo.yaml", "c3/noise_grid_conus_no_bg_geoid.yaml",
          "c4/noise_conus_no_bg_geoid.yaml"}


def test_exactly_the_25_used_templates_are_packaged():
    assert len(USED) == 25
    assert set(catalog.templates()) == USED
    assert catalog.templates() == sorted(USED)


def test_template_hashes_equal_the_web_configs(web_parity):
    web = web_parity["templates"]
    assert set(web) == USED | UNUSED          # the web snapshot had all 28
    # R1-F1 deliberate difference: custom_grid.yaml gained buffer.grid_weights_nodata_free (the
    # custom-grid C3 now weights a nodata-free surrogate of the grid), so its hash no longer
    # matches web 0.2.7's config. The integrated web ships the same updated config. Every other
    # template still matches the web snapshot byte for byte.
    f1 = recipes.CUSTOM_GRID_TEMPLATE
    pkg = catalog.template_hashes()
    assert pkg[f1] != web[f1], "custom_grid.yaml should differ from web 0.2.7 (R1-F1)"
    assert {r: h for r, h in pkg.items() if r != f1} == \
        {rel: web[rel] for rel in USED if rel != f1}


def test_every_template_is_used_by_a_recipe():
    used = {s.template for r in recipes.all_recipes().values() for s in r.steps.values()}
    used |= {recipes.CUSTOM_C4_TEMPLATE, recipes.CUSTOM_GRID_TEMPLATE}
    assert used == USED


def test_template_access():
    cfg = catalog.template("c3/bg_us_demo.yaml")
    assert cfg["name"] == "bg_us_demo" and cfg["linkage_pattern"] == "boundary_overlap_fast"
    assert len(cfg["source"]["file"]) == 51        # 50 states + DC
    text = catalog.template_text("c4/acag_demo.yaml")
    assert "acag_multi" in text and catalog.template_bytes("c4/acag_demo.yaml") == text.encode()


@pytest.mark.parametrize("bad", ["c3/bg_fl_leon_demo.yaml", "../variables.json",
                                 "c3/../../catalog/variables.json", "bg_us_demo.yaml", ""])
def test_unknown_or_traversing_names_are_refused(bad):
    with pytest.raises(KeyError):
        catalog.template_bytes(bad)


def test_templates_dir_holds_the_same_bytes():
    root = catalog.templates_dir()
    on_disk = sorted(f"{p.parent.name}/{p.name}" for p in root.glob("c[34]/*.yaml"))
    assert on_disk == catalog.templates()
    for rel in on_disk:
        assert (root / rel).read_bytes() == catalog.template_bytes(rel)


def test_init_config_starter_templates_are_untouched():
    from importlib import resources
    starter = resources.files("spacescans.resources.configs.templates")
    names = sorted(f"{k}/{p.name}" for k in ("c3", "c4")
                   for p in starter.joinpath(k).iterdir() if p.name.endswith(".yaml"))
    assert names == ["c3/county.yaml", "c3/zcta5.yaml", "c4/cbp_fallback.yaml", "c4/zbp.yaml"]


def test_templates_byte_identical_to_live_web_checkout(web_repo: Path):
    configs = web_repo / "configs"
    for rel in catalog.templates():
        assert catalog.template_bytes(rel) == (configs / rel).read_bytes(), rel
    web_all = {f"{p.parent.name}/{p.name}" for p in configs.glob("c[34]/*.yaml")}
    assert web_all - set(catalog.templates()) == UNUSED, (
        "the web has templates the catalog does not ship: "
        f"{sorted(web_all - set(catalog.templates()) - UNUSED)}")
    for name in ("variable_metadata.json", "variable_metadata.schema.json",
                 "feature_dictionary.json"):
        web_file = web_repo / "backend" / "app" / "data" / name
        ours = {"variable_metadata.json": "variables.json",
                "variable_metadata.schema.json": "variables.schema.json",
                "feature_dictionary.json": "features.json"}[name]
        from importlib import resources
        mine = resources.files("spacescans.catalog").joinpath(ours).read_bytes()
        assert hashlib.sha256(mine).digest() == hashlib.sha256(web_file.read_bytes()).digest(), name
