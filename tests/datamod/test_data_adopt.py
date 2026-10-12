"""adopt(): receipts for mirror data placed without one (web script, upload page, by hand)."""
from __future__ import annotations

from spacescans.data import adopt, extract_archive, status
from spacescans.data.receipts import read_receipt


def _web_style_placement(root, archive, h):
    """What fetch_distribution.sh leaves: an extracted tree, a bare file, no receipts."""
    arc = root.parent / archive.name
    arc.write_bytes(archive.data)
    root.mkdir(parents=True, exist_ok=True)
    extract_archive(arc, root)
    (root / "NDI/C4").mkdir(parents=True)
    (root / "NDI/C4/panel.Rda").write_bytes(h.PANEL)


def test_adopt_writes_receipts_and_makes_data_ready(mini_manifest, data_dir, archive, h):
    _web_style_placement(data_dir, archive, h)
    before = status(["ndi"], data_dir=data_dir, manifest=mini_manifest)
    assert {d.dataset: d.status for d in before.datasets} == {
        "county_boundaries": "unverified", "ndi_panel": "unverified"}
    dry = adopt(["ndi"], data_dir=data_dir, manifest=mini_manifest, dry_run=True)
    assert [i.result for i in dry.items] == ["would_adopt", "would_adopt"]
    assert not (data_dir / ".spacescans").exists()
    rep = adopt(["ndi"], data_dir=data_dir, manifest=mini_manifest)
    assert rep.ok and [i.result for i in rep.items] == ["adopted", "adopted"]
    r = read_receipt(data_dir, archive.name)
    assert r["origin"] == "adopt" and len(r["files"]) == archive.files
    assert rep.status.variable("ndi").status == "ready"
    again = adopt(["ndi"], data_dir=data_dir, manifest=mini_manifest)
    assert [i.result for i in again.items] == ["has_receipt", "has_receipt"]


def test_adopt_refuses_trees_that_differ_from_the_archive(mini_manifest, data_dir, archive, h):
    _web_style_placement(data_dir, archive, h)
    (data_dir / "County/C3/extra.txt").write_text("not from the archive")
    (data_dir / "NDI/C4/panel.Rda").write_bytes(bytes(reversed(h.PANEL)))   # same size
    rep = adopt(["ndi"], data_dir=data_dir, manifest=mini_manifest)
    res = {i.artifact: (i.result, i.detail) for i in rep.items}
    assert res[archive.name][0] == "mismatch" and "the archive holds 4 files" in res[archive.name][1]
    assert res["panel.Rda"] == ("mismatch", "NDI/C4/panel.Rda: sha256 does not match the manifest")
    assert not rep.ok
    assert read_receipt(data_dir, archive.name) is None and read_receipt(data_dir, "panel.Rda") is None


def test_adopt_reports_missing_data(mini_manifest, data_dir):
    rep = adopt(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest)
    assert [i.result for i in rep.items] == ["missing"]
