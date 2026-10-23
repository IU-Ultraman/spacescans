"""The packaged data manifest: schema, published checksums, licence notes."""
from __future__ import annotations

import json

import pytest

from spacescans.data import ManifestError, UsageError, load_manifest
from spacescans.data.manifest import MANIFEST_PATH, SCHEMA_PATH, parse_manifest

# The published SHA256SUMS.txt of the mirror (identical to spacescans-web
# backend/app/data/distribution_manifest.json at 3991c3b).
PUBLISHED = {
    "tiger_roads_filtered_cache_v1.tar.gz": ("d4e6955ffc3fe76ea0a6b09bfc1702785443e237556223d6edcfb99ebaf485db", 1845960820),
    "nhd_features_cache_v1.tar.gz": ("0ac982b266dbf2a0e28c0bda9e37fd0fe1a6e0c60050eec84e5d350c0604efad", 38471084940),
    "bg_boundaries_v1.tar.gz": ("387283ce83e8a6b3296cd0eb1e1641bb2ecf15dee74a988fa0f1ef6117ba6b17", 1336811339),
    "tract_boundaries_v1.tar.gz": ("1eb11e0abb10885bf3437e63bff8a130a0565d763e8876dc3b048a902e752952", 355586942),
    "county_boundaries_v1.tar.gz": ("657c6983048acc0a819cfb81045feb8174aaba50dcde7ff573162a065f4578eb", 74463271),
    "zcta5_boundaries_v1.tar.gz": ("98784e0c1dfb6ad9a75786080216bcf6e853d51d9670401e1432e75faa391530", 524188995),
    "noise_v1.tar.gz": ("e93e63aa81ca069d00b28ff8b9894f5c3b4563e9de7417e4c2d9f6c73afc31f9", 1191982269),
    "fara_nationwide_2010_2019_interpolated.Rda": ("4d03fc173de6a67bc7885c42173f033f0f6b6d13d487a263874abdba43f4517e", 447630044),
}
# Value tables committed in spacescans-web, on the mirror since 2026-10-23.
VALUE_TABLES = {
    "ndi_bg_acs5_2008_2012_to_2020_2024_nationwide_summaryfile_xgboost.rds": "NDI/C4",
    "epawalkind_nationwide_2016_2021.Rda": "Walkability/C4",
    "cbp_nationwide_0519.Rda": "Community_Organization_Density/C4",
    "zbp_nationwide_0521.Rda": "Community_Organization_Density/C4",
    "varnameCountRemoved.csv": "FARA/C4",
}
CATALOG = ["ndi", "walkability", "cbp_zcta5", "tiger_proximity", "nhd_bluespace", "noise",
           "vnl", "temis", "fara_tract", "acag", "faqsd"]


def test_manifest_matches_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(json.loads(MANIFEST_PATH.read_text())))
    assert not errors, [e.message for e in errors[:5]]


def test_mirror_archives_carry_the_published_checksums():
    m = load_manifest()
    available = {n: a for n, a in m.artifacts.items() if a.status == "available"}
    assert set(available) == set(PUBLISHED) | set(VALUE_TABLES) | {"acag_template.tif", "temis_template.tif"}
    for name, (sha, size) in PUBLISHED.items():
        assert (available[name].sha256, available[name].bytes) == (sha, size), name
    # extracted_bytes is the web manifest's value; payload_bytes leaves out
    # the AppleDouble members extraction skips (county -1,056 B etc.).
    a = m.artifacts["county_boundaries_v1.tar.gz"]
    assert a.extracted_bytes - a.payload_bytes == 1056
    assert m.artifacts["zcta5_boundaries_v1.tar.gz"].extracted_bytes - m.artifacts["zcta5_boundaries_v1.tar.gz"].payload_bytes == 1272
    assert m.artifacts["noise_v1.tar.gz"].extracted_bytes - m.artifacts["noise_v1.tar.gz"].payload_bytes == 636


def test_web_committed_value_tables_are_bare_mirror_entries():
    m = load_manifest()
    for name, dest in VALUE_TABLES.items():
        a = m.artifacts[name]
        assert a.kind == "bare" and a.dest == dest
        assert a.status == "available" and not a.interim
        assert len(a.sha256) == 64 and a.bytes > 0


def test_every_catalog_variable_maps_to_datasets():
    m = load_manifest()
    assert list(m.variables) == CATALOG
    for key, need in m.variables.items():
        assert need.requires, key
        for req in need.requires:
            for ds in req.any_of:
                assert ds in m.datasets
    assert m.boundaries == {"BG": "bg_boundaries", "Tract": "tract_boundaries",
                            "ZCTA5": "zcta5_boundaries", "County": "county_boundaries"}


def test_provider_licence_notes():
    m = load_manifest()
    vnl, acag, faqsd, temis = (m.datasets[k] for k in ("vnl", "acag", "faqsd", "temis_uv_raw"))
    assert "CC BY 4.0" in vnl.license and "EOG" in vnl.license
    assert "attribution" in vnl.redistribution
    assert vnl.terms.id == "eog-vnl" and vnl.terms.account is True
    assert vnl.terms.url.endswith("EOG_products_CC_License.pdf")
    assert "Elvidge" in vnl.attribution
    assert "global product only" in acag.license
    assert "not confirmed" in acag.redistribution
    assert faqsd.license == "US federal public domain"
    assert "not granted" in temis.license.lower() or "not granted" in temis.redistribution
    for ds in m.datasets.values():
        if ds.acquire != "mirror":
            assert ds.instructions, ds.key
            assert ds.layout, ds.key
    # Provider data are never mirrored. FAQSD, TEMIS and ACAG have scripted
    # downloads, which run only with their terms accepted in the call; VNL
    # needs an EOG login, so it stays manual.
    for key in ("vnl", "acag", "faqsd", "temis_uv_raw"):
        assert not m.datasets[key].artifacts
        assert m.datasets[key].terms is not None
    assert m.datasets["vnl"].acquire == "provider-manual" and not m.datasets["vnl"].scripted
    for key, method in (("acag", "box"), ("faqsd", "files"), ("temis_uv_raw", "daily")):
        assert m.datasets[key].acquire == "provider-scripted" and m.datasets[key].scripted
        assert m.datasets[key].download.method == method
    assert m.datasets["faqsd"].download.range is False and m.datasets["faqsd"].download.gunzip
    assert m.datasets["temis_uv_raw"].download.url.startswith("https://d1qb6yzwaaq4he.cloudfront.net/")


def test_no_data_files_ship_in_the_package():
    pkg = MANIFEST_PATH.parent
    shipped = sorted(p.name for p in pkg.iterdir() if p.is_file() and not p.name.endswith(".py"))
    assert shipped == ["manifest.json", "manifest.schema.json"]


def test_cross_reference_errors_are_caught():
    raw = json.loads(MANIFEST_PATH.read_text())
    raw["variables"]["ndi"]["requires"].append("no_such_dataset")
    with pytest.raises(ManifestError, match="unknown dataset 'no_such_dataset'"):
        parse_manifest(raw)
    raw = json.loads(MANIFEST_PATH.read_text())
    raw["artifacts"]["noise_v1.tar.gz"]["sha256"] = "XYZ"
    with pytest.raises(ManifestError, match="sha256"):
        parse_manifest(raw)
    raw = json.loads(MANIFEST_PATH.read_text())
    raw["datasets"]["vnl"]["layout"][0]["path"] = "../escape.tif"
    with pytest.raises(ManifestError, match="relative path"):
        parse_manifest(raw)


def test_template_expansion():
    m = load_manifest()
    paths = [p for p, _ in m.expand("BG/C3/tl_2010_{state}_bg10/tl_2010_{state}_bg10.{ext}",
                                    {"state": "@states51", "ext": "@shapefile_parts"})]
    assert len(paths) == 51 * 4
    assert paths[0] == "BG/C3/tl_2010_01_bg10/tl_2010_01_bg10.shp"
    assert m.resolve_values("@years_2013_2019") == [str(y) for y in range(2013, 2020)]


def test_unknown_names_are_usage_errors():
    m = load_manifest()
    with pytest.raises(UsageError, match="unknown variable"):
        m.variable("pm99")
