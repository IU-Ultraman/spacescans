"""mirror_check() against the local SharePoint emulation and an HTTP mirror (no real network)."""
from __future__ import annotations

import json

import pytest

from spacescans.cli import main
from spacescans.data import UsageError, load_manifest, mirror_check

LABELS = b"a,b\n1,2\n"


def _write(tmp_path, man):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(man, indent=2) + "\n")
    return p


def test_pending_file_not_uploaded_yet_is_reported_not_flipped(share_manifest, server, tmp_path):
    path = _write(tmp_path, share_manifest)
    before = path.read_text()
    rep = mirror_check(manifest=path, write_manifest=path)
    assert [c.artifact for c in rep.checks] == ["labels.csv"]   # pending_upload only
    c = rep.checks[0]
    assert not c.passed and not c.reachable and c.http_status == 404
    assert "not on the mirror" in c.detail
    assert rep.not_reachable == ["labels.csv"] and rep.updated == [] and not rep.ok
    assert path.read_text() == before
    # Every request is a HEAD or a small range read.
    assert all(r["method"] == "HEAD" or "range" in r["headers"] for r in server.downloads())


def test_uploaded_file_passes_and_is_flipped(share_manifest, server, tmp_path):
    server.files["labels.csv"] = LABELS
    path = _write(tmp_path, share_manifest)
    rep = mirror_check(manifest=path, write_manifest=path)
    c = rep.checks[0]
    assert c.passed and c.size_ok and c.sha256_ok and c.remote_bytes == 8 and c.bytes_read == 8
    assert rep.updated == ["labels.csv"] and rep.ok
    m = load_manifest(path)
    assert m.artifacts["labels.csv"].status == "available"
    assert m.artifacts["panel.Rda"].status == "available"
    gets = [r for r in server.downloads() if r["method"] == "GET"]
    assert gets and all(r["headers"]["range"] == "bytes=0-7" for r in gets)


def test_wrong_bytes_and_html_pages_never_pass(share_manifest, server, tmp_path):
    server.files["labels.csv"] = b"a,b\n9,9\n"            # same size, other content
    rep = mirror_check(manifest=share_manifest)
    assert not rep.checks[0].passed and rep.checks[0].sha256_ok is False
    server.files["labels.csv"] = LABELS
    server.behaviour.html_once = True                     # one sign-in page (the HEAD) ...
    rep = mirror_check(manifest=share_manifest)
    assert rep.checks[0].passed                           # ... the range read still sees the file

    class AlwaysHTML(type(server.behaviour)):             # every answer is a sign-in page
        html_once = property(lambda self: True, lambda self, v: None)

    server.httpd.behaviour = AlwaysHTML()
    rep = mirror_check(manifest=share_manifest)
    assert not rep.checks[0].passed and "HTML page" in rep.checks[0].detail


def test_large_files_are_checked_by_size_with_a_one_byte_read(mini_manifest, server, archive):
    rep = mirror_check([archive.name, "panel.Rda"], manifest=mini_manifest, max_read=100)
    by = {c.artifact: c for c in rep.checks}
    for c in by.values():
        assert c.passed and c.sha256_ok is None and c.bytes_read == 1, c.to_dict()
    ranges = [r["headers"].get("range") for r in server.downloads() if r["method"] == "GET"]
    assert ranges == ["bytes=0-0", "bytes=0-0"]
    server.files["panel.Rda"] = b"short"
    assert not mirror_check(["panel.Rda"], manifest=mini_manifest, max_read=100).checks[0].size_ok


def test_a_server_that_ignores_range_still_reads_at_most_the_file(mini_manifest, server):
    server.files["labels.csv"] = LABELS
    server.behaviour.ignore_range = True
    rep = mirror_check(manifest=mini_manifest)
    assert rep.checks[0].passed and rep.checks[0].bytes_read == 8


def test_folder_mirror_and_limits(mini_manifest, tmp_path):
    web = tmp_path / "pipeline-data"
    (web / "FARA/C4").mkdir(parents=True)
    (web / "FARA/C4/labels.csv").write_bytes(LABELS)
    rep = mirror_check(manifest=mini_manifest, mirror=web)
    assert rep.checks[0].passed and rep.checks[0].method == "folder"
    with pytest.raises(UsageError, match="capped"):
        mirror_check(manifest=mini_manifest, max_read=2 * 1024 * 1024)
    with pytest.raises(UsageError, match="unknown artifact"):
        mirror_check(["nope.tar.gz"], manifest=mini_manifest)


def test_unreachable_mirror_is_a_report_not_an_exception(mini_manifest):
    mini_manifest["mirrors"]["test"]["url"] = "http://127.0.0.1:9/closed"
    rep = mirror_check(manifest=mini_manifest)
    assert not rep.checks[0].passed and "network error" in rep.checks[0].detail


def test_packaged_manifest_lists_the_seven_pending_artifacts():
    m = load_manifest()
    pending = sorted(n for n, a in m.artifacts.items() if a.status == "pending_upload")
    assert pending == sorted([
        "acag_template.tif", "cbp_nationwide_0519.Rda", "epawalkind_nationwide_2016_2021.Rda",
        "ndi_bg_acs5_2008_2012_to_2020_2024_nationwide_summaryfile_xgboost.rds",
        "temis_template.tif", "varnameCountRemoved.csv", "zbp_nationwide_0521.Rda"])


def test_cli_mirror_check(share_manifest, server, tmp_path, monkeypatch, capsys):
    path = _write(tmp_path, share_manifest)
    import spacescans.data._mirror as mod

    real = mod.mirror_check
    monkeypatch.setattr(mod, "mirror_check", lambda *a, **k: real(*a, manifest=path, **k))
    assert main(["data", "mirror-check"]) == 1
    assert "NOT ON MIRROR" in capsys.readouterr().out
    server.files["labels.csv"] = LABELS
    assert main(["data", "mirror-check", "--write-manifest", str(path), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["updated"] == ["labels.csv"] and out["reachable"] == ["labels.csv"]
