"""verify() and the `spacescans data status|fetch|verify` CLI."""
from __future__ import annotations

import json

import pytest

from spacescans.cli import main
from spacescans.data import fetch, verify


def _write_manifest(tmp_path, manifest) -> str:
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(manifest))
    return str(p)


@pytest.fixture
def use_manifest(monkeypatch, tmp_path, mini_manifest):
    """Point the packaged-manifest loader at the test manifest for CLI runs."""
    import spacescans.data.manifest as m

    path = _write_manifest(tmp_path, mini_manifest)
    monkeypatch.setattr(m, "_packaged_manifest", lambda: m.load_manifest(path))
    return mini_manifest


def test_verify_hashes_receipted_files(mini_manifest, data_dir, archive, h, server, tmp_path):
    fetch(["tiger_proximity", "ndi"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    rep = verify(["ndi"], data_dir=data_dir, manifest=mini_manifest)
    assert rep.ok and rep.corrupt == []
    states = {a["name"]: a["state"] for a in rep.artifacts()}
    assert states == {archive.name: "verified", "panel.Rda": "verified"}
    (data_dir / "NDI/C4/panel.Rda").write_bytes(bytes(reversed(h.PANEL)))   # same size
    assert verify(["ndi"], data_dir=data_dir, manifest=mini_manifest, deep=False).ok
    bad = verify(["ndi"], data_dir=data_dir, manifest=mini_manifest)
    assert not bad.ok and bad.corrupt == ["ndi_panel"]
    # A loose archive checked against the manifest.
    good = tmp_path / archive.name
    good.write_bytes(archive.data)
    checks = verify(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, archives=[good]).archives
    assert checks[0].ok and checks[0].detail == "sha256 matches"


def test_cli_status_json_and_exit_codes(use_manifest, data_dir, capsys):
    rc = main(["data", "status", "tiger_proximity", "--data-dir", str(data_dir), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ready"] is False
    assert out["variables"][0]["status"] == "missing"
    assert out["next_steps"][0]["command"] == "spacescans data fetch tiger_proximity"


def test_cli_status_table(use_manifest, data_dir, capsys):
    rc = main(["data", "status", "ndi", "vnl", "--data-dir", str(data_dir)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "spacescans data fetch ndi --data-dir" in out
    assert "terms: account required (https://example.org/licence.pdf)" in out
    assert "Download it from EOG yourself." in out


def test_cli_fetch_then_status_then_verify(use_manifest, data_dir, server, capsys, monkeypatch):
    import spacescans.data._fetch as f

    monkeypatch.setattr(f, "HEADROOM", 0)
    rc = main(["data", "fetch", "tiger_proximity", "--data-dir", str(data_dir)])
    err = capsys.readouterr()
    assert rc == 0, err
    assert "all requested data is in place." in err.out
    assert main(["data", "status", "tiger_proximity", "--data-dir", str(data_dir)]) == 0
    assert main(["data", "verify", "tiger_proximity", "--data-dir", str(data_dir)]) == 0
    assert "verify: ok" in capsys.readouterr().out


def test_cli_fetch_dry_run(use_manifest, data_dir, server, capsys):
    rc = main(["data", "fetch", "ndi", "fara_tract", "--data-dir", str(data_dir), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 1                                   # a manual step remains
    assert "county_test_v1.tar.gz" in out and "panel.Rda" in out
    assert "needs a manual step" in out and "fara_labels" in out
    assert server.requests == []


def test_cli_errors(use_manifest, data_dir, capsys, monkeypatch):
    assert main(["data", "status", "nope", "--data-dir", str(data_dir)]) == 2
    assert "unknown variable" in capsys.readouterr().err
    monkeypatch.delenv("SPACESCANS_DATA_DIR", raising=False)
    assert main(["data", "status", "ndi"]) == 2
    assert "no data directory given" in capsys.readouterr().err
    assert main(["data", "fetch", "vnl", "--data-dir", str(data_dir), "--accept-terms", "bogus"]) == 2


def test_cli_network_error_json(use_manifest, data_dir, server, capsys, archive, monkeypatch):
    import spacescans.data._fetch as f

    monkeypatch.setattr(f, "HEADROOM", 0)
    monkeypatch.setattr(f.time, "sleep", lambda s: None)   # resolved at call time
    server.behaviour.fail_status = [503] * 20
    rc = main(["data", "fetch", "tiger_proximity", "--data-dir", str(data_dir), "--json"])
    cap = capsys.readouterr()
    assert rc == 1
    out = json.loads(cap.out)
    assert out["ok"] is False and out["error"]["kind"] == "network"
    assert out["report"]["items"][0]["outcome"] == "failed"
