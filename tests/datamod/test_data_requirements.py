"""data.requirements() (#16), prepare_templates() and the CLI for both."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spacescans.cli import main
from spacescans.data import (
    DataCorruptError,
    UsageError,
    load_manifest,
    prepare_templates,
    requirements,
    status,
)
from spacescans.data.receipts import read_receipt

MANIFEST_JSON = Path(__file__).resolve().parents[2] / "src/spacescans/data/manifest.json"
ACAG_SHA = "5530456d10d2888745c158bce81b8eade3315338ead847d8112950fc0d15cc6e"
TEMIS_SHA = "49fcbfe28ffb95f4b476f1481cdccce1fd678ed53fd365ad1a62875f125d2847"


def test_requirements_follow_the_manifest():
    req = requirements(["ndi", "walkability", "tiger_proximity", "faqsd", "temis"])
    assert list(req) == ["ndi", "walkability", "tiger_proximity", "faqsd", "temis"]
    ndi = req["ndi"]
    assert ndi.requires == [["bg_boundaries"], ["ndi_panel"]] and ndi.default == ["bg_boundaries", "ndi_panel"]
    assert ndi.artifacts == ["bg_boundaries_v1.tar.gz",
                             "ndi_bg_acs5_2008_2012_to_2020_2024_nationwide_summaryfile_xgboost.rds"]
    assert ndi.pending == []                               # on the mirror since 2026-10-23
    assert ndi.download_bytes == 1336811339 + 39439111 and ndi.provider == [] and ndi.terms == []
    tiger = req["tiger_proximity"]
    assert tiger.requires == [["tiger_roads_cache", "tiger_roads_raw"], ["county_boundaries"]]
    assert tiger.default == ["tiger_roads_cache", "county_boundaries"]
    assert set(tiger.needs) == {"tiger_roads_cache", "tiger_roads_raw", "county_boundaries"}
    assert tiger.needs["tiger_roads_raw"].acquire == "provider-manual"
    faqsd = req["faqsd"]
    assert faqsd.provider == ["faqsd"] and faqsd.terms == ["epa-faqsd"]
    assert faqsd.artifacts == ["tract_boundaries_v1.tar.gz"]
    assert faqsd.needs["faqsd"].acquire == "provider-scripted"
    temis = req["temis"]
    assert temis.default == ["temis_template", "temis_uv_raw"]
    assert temis.needs["temis_template"].generate == ["temis_template.tif"]
    # Shared artifacts once across variables (bg for ndi + walkability).
    assert req.artifacts.count("bg_boundaries_v1.tar.gz") == 1
    assert req.terms == ["epa-faqsd", "knmi-temis"]
    json.dumps(req.to_dict())                              # JSON-ready for the bridge


def test_requirements_all_and_unknown():
    req = requirements()
    assert list(req) == list(load_manifest().variables)
    # map_data.md section 1b: download / on-disk sizes per variable (GB)
    assert round(req["noise"].download_bytes / 1e9, 2) == 1.19
    assert round(req["nhd_bluespace"].disk_bytes / 1e9, 2) == 50.35
    assert round(req["fara_tract"].download_bytes / 1e9, 2) == 0.80
    with pytest.raises(UsageError) as e:
        requirements(["ndi", "pm99", "nope"])
    assert e.value.kind == "config" and e.value.details["unknown_variables"] == ["pm99", "nope"]
    assert list(requirements("noise")) == ["noise"]


def test_cli_requirements(capsys):
    assert main(["data", "requirements", "acag", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["variables"]["acag"]["terms"] == ["acag-v5na05"]
    assert out["variables"]["acag"]["pending"] == []
    assert main(["data", "requirements", "noise"]) == 0
    assert "noise_rasters" in capsys.readouterr().out


# --------------------------------------------------------------------------- templates

def test_prepare_templates_reproduce_the_published_bytes(data_dir, capsys):
    pytest.importorskip("rasterio")
    rep = prepare_templates(data_dir)
    assert rep.ok and [(i.name, i.result) for i in rep.items] == [
        ("acag_template.tif", "generated"), ("temis_template.tif", "generated")]
    assert {i.name: i.sha256 for i in rep.items} == {"acag_template.tif": ACAG_SHA,
                                                      "temis_template.tif": TEMIS_SHA}
    assert (data_dir / "ACAG/C3/acag_template.tif").stat().st_size == 233766
    assert (data_dir / "TEMIS/C3/temis_template.tif").stat().st_size == 2076126
    assert read_receipt(data_dir, "acag_template.tif")["origin"] == "prepare"
    st = status(datasets=["acag_template", "temis_template"], data_dir=data_dir, deep=True)
    assert st.dataset("acag_template").status == "ready"
    assert st.dataset("temis_template").status == "ready"
    assert not list((data_dir / ".spacescans").rglob("tmp*"))
    again = prepare_templates(data_dir)
    assert [i.result for i in again.items] == ["present", "present"]
    assert main(["data", "prepare", "templates", "--data-dir", str(data_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_prepare_refuses_bytes_that_differ(data_dir, monkeypatch):
    pytest.importorskip("rasterio")
    from dataclasses import replace

    from spacescans.data import _prepare

    spec = _prepare.TEMPLATES["temis_template.tif"]
    monkeypatch.setitem(_prepare.TEMPLATES, "temis_template.tif", replace(spec, options={}))
    with pytest.raises(DataCorruptError) as e:
        prepare_templates(data_dir, names=["temis_template.tif"])
    assert e.value.report.items[0].result == "mismatch"
    assert not (data_dir / "TEMIS/C3/temis_template.tif").exists()
    with pytest.raises(UsageError, match="not a template"):
        prepare_templates(data_dir, names=["noise_v1.tar.gz"])


def test_templates_on_the_mirror_are_fetched(data_dir):
    st = status(["acag"], data_dir=data_dir)
    d = st.dataset("acag_template")
    assert d.status == "missing" and d.fetchable and d.generate == []
    assert d.next == "spacescans data fetch --dataset acag_template"
    assert st.variable("acag").status == "action_required"        # the ACAG rasters: provider terms


def test_status_suggests_prepare_for_templates(data_dir):
    # A template that is not on the mirror (as before 2026-10-23) is generated locally.
    raw = json.loads(MANIFEST_JSON.read_text())
    raw["artifacts"]["acag_template.tif"]["status"] = "pending_upload"
    raw["artifacts"]["acag_template.tif"]["interim"] = "run spacescans data prepare templates"
    st = status(["acag"], data_dir=data_dir, manifest=raw)
    d = st.dataset("acag_template")
    assert d.status == "action_required" and d.generate == ["acag_template.tif"]
    assert d.next.startswith("spacescans data prepare templates")
    step = next(s for s in st.next_steps if s.get("dataset") == "acag_template")
    assert step["command"] == "spacescans data prepare templates"
    v = st.variable("acag")
    assert v.status == "action_required"


def test_reference_templates_match_the_manifest():
    """Read-only: the web's committed templates carry the manifest's sha256."""
    from pathlib import Path

    from spacescans.data._status import sha256_file

    root = Path("/Users/xai/Desktop/spacescans-all/spacescans-web/pipeline-data")
    if not root.is_dir():
        pytest.skip("reference data root not on this machine")
    assert sha256_file(root / "ACAG/C3/acag_template.tif") == ACAG_SHA
    assert sha256_file(root / "TEMIS/C3/temis_template.tif") == TEMIS_SHA
