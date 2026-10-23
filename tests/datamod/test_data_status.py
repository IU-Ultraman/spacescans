"""spacescans.data.status(): layout rules, receipts and next steps, offline."""
from __future__ import annotations

from pathlib import Path

import pytest

from spacescans.data import (
    UsageError,
    cloud_sync_warning,
    load_manifest,
    resolve_data_dir,
    status,
)
from spacescans.data.receipts import write_receipt


def _place_county(root: Path, h, *, skip: str | None = None) -> None:
    base = root / "County/C3/tl_2010_us_county10"
    base.mkdir(parents=True, exist_ok=True)
    for name, data in (("tl_2010_us_county10.shp", h.SHP), ("tl_2010_us_county10.dbf", b"d"),
                       ("tl_2010_us_county10.prj", b"p")):
        if name != skip:
            (base / name).write_bytes(data)


def _place_panel(root: Path, data: bytes) -> Path:
    p = root / "NDI/C4/panel.Rda"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_empty_data_dir(mini_manifest, data_dir, archive, h):
    rep = status(data_dir=data_dir, manifest=mini_manifest)
    v = {x.variable: x for x in rep.variables}
    assert v["tiger_proximity"].status == "missing"
    assert v["tiger_proximity"].next == "spacescans data fetch tiger_proximity"
    assert v["ndi"].status == "missing"
    assert v["ndi"].download_bytes == len(archive.data) + len(h.PANEL)
    # A file not yet on the mirror cannot be fetched: a manual step.
    assert v["fara_tract"].status == "action_required"
    fara = rep.dataset("fara_labels")
    assert fara.fetchable is False and fara.download_bytes == 0
    assert any("copy it from a spacescans-web checkout" in i for i in fara.instructions)
    # Provider data: instructions and terms, never a fetch command.
    vnl = rep.dataset("vnl")
    assert vnl.status == "action_required" and vnl.terms["id"] == "eog-vnl"
    assert vnl.instructions == ["Download it from EOG yourself."]
    # The county archive is shared by two variables: counted once.
    assert rep.download_bytes == len(archive.data) + len(h.PANEL)
    fetch_step = rep.next_steps[0]
    assert fetch_step["action"] == "fetch"
    assert fetch_step["variables"] == ["tiger_proximity", "ndi"]
    manual = {s["dataset"] for s in rep.next_steps if s["action"] == "manual"}
    assert manual == {"fara_labels", "vnl"}
    assert not rep.ready
    assert any("does not exist yet" in w for w in rep.warnings)


def test_placed_without_receipts_is_unverified_then_deep_verifies_bare_files(mini_manifest, data_dir, h):
    _place_county(data_dir, h)
    _place_panel(data_dir, h.PANEL)
    rep = status(["ndi"], data_dir=data_dir, manifest=mini_manifest)
    assert rep.dataset("county_boundaries").status == "unverified"
    assert rep.dataset("ndi_panel").status == "unverified"
    assert rep.variable("ndi").usable and rep.ready
    deep = status(["ndi"], data_dir=data_dir, manifest=mini_manifest, deep=True)
    assert deep.dataset("ndi_panel").status == "ready"           # sha256 matched the manifest
    assert deep.dataset("county_boundaries").status == "unverified"  # no per-file digests to check


def test_partial_tree_is_incomplete(mini_manifest, data_dir, h):
    _place_county(data_dir, h, skip="tl_2010_us_county10.dbf")
    d = status(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest).dataset("county_boundaries")
    assert d.status == "incomplete"
    assert d.missing == ["County/C3/tl_2010_us_county10/tl_2010_us_county10.dbf"]
    assert (d.present, d.expected) == (2, 3)
    assert d.fetchable and d.next == "spacescans data fetch --dataset county_boundaries"


def test_wrong_size_bare_file_is_corrupt(mini_manifest, data_dir, h):
    _place_panel(data_dir, h.PANEL[:-1])
    d = status(datasets=["ndi_panel"], data_dir=data_dir, manifest=mini_manifest).dataset("ndi_panel")
    assert d.status == "corrupt" and not d.usable
    assert "102,399 bytes, expected 102,400" in d.problems[0]


def test_same_size_but_different_bytes_is_found_only_by_deep(mini_manifest, data_dir, h):
    _place_panel(data_dir, bytes(reversed(h.PANEL)))
    assert status(datasets=["ndi_panel"], data_dir=data_dir,
                  manifest=mini_manifest).dataset("ndi_panel").status == "unverified"
    deep = status(datasets=["ndi_panel"], data_dir=data_dir, manifest=mini_manifest, deep=True)
    assert deep.dataset("ndi_panel").status == "corrupt"


def test_receipts_make_ready_and_catch_changes(mini_manifest, data_dir, h):
    from spacescans.data.manifest import coerce_manifest

    man = coerce_manifest(mini_manifest)
    _place_county(data_dir, h)
    files = []
    for name in ("tl_2010_us_county10.shp", "tl_2010_us_county10.dbf", "tl_2010_us_county10.prj"):
        rel = f"County/C3/tl_2010_us_county10/{name}"
        data = (data_dir / rel).read_bytes()
        files.append((rel, len(data), h.sha256(data)))
    art = man.artifacts["county_test_v1.tar.gz"]
    write_receipt(data_dir, art, files=files, source="test", origin="fetch", data_release="2099.01")
    assert status(["tiger_proximity"], data_dir=data_dir, manifest=man).variable("tiger_proximity").status == "ready"
    # Same size, different content: only the deep check sees it.
    shp = data_dir / "County/C3/tl_2010_us_county10/tl_2010_us_county10.shp"
    shp.write_bytes(b"X" * len(h.SHP))
    assert status(["tiger_proximity"], data_dir=data_dir, manifest=man).dataset("county_boundaries").status == "ready"
    deep = status(["tiger_proximity"], data_dir=data_dir, manifest=man, deep=True).dataset("county_boundaries")
    assert deep.status == "corrupt" and "sha256 differs" in deep.problems[0]
    # A deleted file is caught by the quick check.
    shp.unlink()
    d = status(["tiger_proximity"], data_dir=data_dir, manifest=man).dataset("county_boundaries")
    assert d.status == "corrupt"


def test_receipt_for_another_release_is_ignored(mini_manifest, data_dir, h, archive):
    from spacescans.data.manifest import coerce_manifest

    man = coerce_manifest(mini_manifest)
    _place_county(data_dir, h)
    import dataclasses
    old = dataclasses.replace(man.artifacts[archive.name], sha256="0" * 64)
    write_receipt(data_dir, old, files=[], source="test", origin="fetch", data_release="1999.01")
    d = status(["tiger_proximity"], data_dir=data_dir, manifest=man).dataset("county_boundaries")
    assert d.status == "unverified"
    assert any("different version" in w for w in d.warnings)


def test_vnl_one_file_per_year(mini_manifest, data_dir):
    vdir = data_dir / "VNL/C3"
    vdir.mkdir(parents=True)
    for y in (2013, 2014):
        (vdir / f"VNL_v21_npp_{y}_global_vcmslcfg_c202205302300.average_masked.dat.tif").write_bytes(b"t")
    d = status(["vnl"], data_dir=data_dir, manifest=mini_manifest).dataset("vnl")
    assert d.status == "incomplete" and d.missing == ["VNL/C3: no file for year 2015"]
    (vdir / "VNL_v21_npp_2015_global_vcmslcfg_c202205302300.average_masked.dat.tif").write_bytes(b"t")
    assert status(["vnl"], data_dir=data_dir, manifest=mini_manifest).dataset("vnl").status == "ready"
    # A second product for the same year would be read too: not usable.
    (vdir / "VNL_v21_npp_2014_global_vcmcfg_c202205302300.average_masked.dat.tif").write_bytes(b"t")
    (vdir / "other.tif").write_bytes(b"t")
    d = status(["vnl"], data_dir=data_dir, manifest=mini_manifest).dataset("vnl")
    assert d.status == "incomplete"
    assert "2 files for year 2014" in d.problems[0]
    assert any("other.tif" in w for w in d.warnings)


def test_alternatives_take_the_best(data_dir):
    gdb = data_dir / "NHD/C4/NHDPlus_H_National_Release_2_GDB.gdb"
    gdb.mkdir(parents=True)
    (gdb / "a0000001.gdbtable").write_bytes(b"x")
    rep = status(["nhd_bluespace"], data_dir=data_dir)
    v = rep.variable("nhd_bluespace")
    assert v.status == "ready" and v.chosen == ["nhd_gdb_raw"]
    assert rep.next_steps == []


def test_daily_rule_with_known_gap(data_dir, h):
    man = h.build_manifest({"type": "http", "url": "http://127.0.0.1:9"}, type("A", (), {
        "name": "a.tar.gz", "data": b"x", "sha256": "0" * 64, "payload_bytes": 1, "files": 1})())
    man["datasets"]["uv"] = {
        "title": "uv", "acquire": "provider-manual", "provider": "KNMI", "license": "x",
        "redistribution": "not granted", "attribution": "credit", "instructions": ["get it"],
        "layout": [{"rule": "daily", "path": "TEMIS/C4/raw/{var}/{year}/{var}{yyyymmdd}.hdf",
                    "vars": {"var": ["uvief"]}, "years": [2016], "known_gaps": ["20160229"],
                    "magic": "0e031301"}]}
    man["variables"]["temis"] = {"requires": ["uv"]}
    d = data_dir / "TEMIS/C4/raw/uvief/2016"
    d.mkdir(parents=True)
    import datetime as dt
    day = dt.date(2016, 1, 1)
    while day.year == 2016:
        if day != dt.date(2016, 2, 29):
            (d / f"uvief{day:%Y%m%d}.hdf").write_bytes(bytes.fromhex("0e031301") + b"rest")
        day += dt.timedelta(days=1)
    rep = status(["temis"], data_dir=data_dir, manifest=man, deep=True)
    assert rep.dataset("uv").status == "ready" and rep.dataset("uv").expected == 365
    (d / "uvief20160704.hdf").unlink()
    (d / "uvief20160705.hdf").write_bytes(b"<html>")
    rep = status(["temis"], data_dir=data_dir, manifest=man, deep=True)
    u = rep.dataset("uv")
    assert u.status == "corrupt"                      # the bad signature
    assert u.missing == ["TEMIS/C4/raw/uvief/2016/: 1 of 365 days missing, e.g. uvief20160704.hdf"]


def test_reference_root_layout_matches_the_web_data_root():
    """Read-only check against the project's reference data root, when present."""
    root = Path("/Users/xai/Desktop/spacescans-all/spacescans-web/pipeline-data")
    if not root.is_dir():
        pytest.skip("reference data root not on this machine")
    rep = status(data_dir=root)
    assert all(v.usable for v in rep.variables), [(v.variable, v.status) for v in rep.variables]


def test_data_dir_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("SPACESCANS_DATA_DIR", raising=False)
    with pytest.raises(UsageError, match="no data directory given"):
        resolve_data_dir()
    monkeypatch.setenv("SPACESCANS_DATA_DIR", str(tmp_path))
    assert resolve_data_dir() == tmp_path.resolve()
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_data_dir("~/ss-data") == (tmp_path / "ss-data").resolve()


def test_cloud_sync_warning():
    home = Path.home()
    assert cloud_sync_warning(home / "Library/CloudStorage/OneDrive-IU/ss-data")
    assert cloud_sync_warning(home / "Library/Mobile Documents/com~apple~CloudDocs/x")
    assert cloud_sync_warning(Path("/Users/someone/OneDrive - Indiana University/data"))
    assert cloud_sync_warning(Path("/Volumes/External/ss-data")) is None


def test_status_never_writes(mini_manifest, data_dir, h):
    _place_county(data_dir, h)
    before = sorted(p.relative_to(data_dir) for p in data_dir.rglob("*"))
    status(data_dir=data_dir, manifest=mini_manifest, deep=True)
    assert sorted(p.relative_to(data_dir) for p in data_dir.rglob("*")) == before


def test_unknown_variable(mini_manifest, data_dir):
    with pytest.raises(UsageError) as e:
        status(["ndi", "nope", "nada"], data_dir=data_dir, manifest=mini_manifest)
    assert e.value.details["unknown_variables"] == ["nope", "nada"]
    assert e.value.kind == "config"


def test_real_manifest_statuses_for_an_empty_dir(data_dir):
    rep = status(data_dir=data_dir)
    st = {v.variable: v.status for v in rep.variables}
    # One fetch from the mirror away (the value tables went up on 2026-10-23).
    for key in ("noise", "tiger_proximity", "nhd_bluespace", "ndi", "walkability", "cbp_zcta5", "fara_tract"):
        assert st[key] == "missing", key
    # Each needs a provider download (terms to accept, or a manual step).
    for key in ("vnl", "temis", "acag", "faqsd"):
        assert st[key] == "action_required", key
    man = load_manifest()
    total = sum(a.bytes for a in man.artifacts.values() if a.available)
    assert rep.dataset("bg_boundaries").download_bytes == 1336811339
    assert rep.download_bytes <= total + 10 ** 11


def test_partly_mirrored_dataset_still_suggests_a_fetch(mini_manifest, data_dir, archive):
    # One file of the dataset is on the mirror, the other is not uploaded yet.
    mini_manifest["datasets"]["fara_labels"]["artifacts"] = ["labels.csv", "panel.Rda"]
    mini_manifest["datasets"]["ndi_panel"]["artifacts"] = [archive.name]
    del mini_manifest["datasets"]["county_boundaries"]
    mini_manifest["variables"] = {"fara_tract": {"requires": ["fara_labels"]}}
    del mini_manifest["boundaries"]
    rep = status(["fara_tract"], data_dir=data_dir, manifest=mini_manifest)
    d = rep.dataset("fara_labels")
    assert d.status == "action_required" and not d.fetchable and d.fetch_helps
    assert d.next.startswith("spacescans data fetch --dataset fara_labels; then not yet on the mirror")
    assert rep.variable("fara_tract").next.startswith("spacescans data fetch fara_tract; then manual")
    assert rep.next_steps[0]["variables"] == ["fara_tract"]
