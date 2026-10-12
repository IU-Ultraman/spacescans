"""spacescans.data.fetch() against a local Range-capable server (no real network)."""
from __future__ import annotations

import json
import stat

import pytest

from spacescans.data import (
    DataCorruptError,
    DataLock,
    DiskSpaceError,
    LockedError,
    NetworkError,
    NotOnMirrorError,
    UsageError,
    fetch,
    plan,
    status,
)
from spacescans.data.receipts import read_receipt


def _parts(root):
    d = root / ".spacescans" / "downloads"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def test_fetch_downloads_extracts_and_writes_receipts(mini_manifest, data_dir, archive, h, server):
    events = []
    rep = fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest,
                on_event=events.append, **h.fast())
    assert rep.ok and [i.outcome for i in rep.items] == ["downloaded"]
    assert rep.items[0].files == archive.files
    shp = data_dir / "County/C3/tl_2010_us_county10/tl_2010_us_county10.shp"
    assert shp.read_bytes() == h.SHP
    assert stat.S_IMODE(shp.stat().st_mode) == 0o644
    assert not list(data_dir.rglob("._*")) and not list(data_dir.rglob("*.part"))
    assert _parts(data_dir) == []                       # archive deleted after extraction
    r = read_receipt(data_dir, archive.name)
    assert r["sha256"] == archive.sha256 and r["origin"] == "fetch"
    assert len(r["files"]) == archive.files and len(r["skipped"]) == 4
    assert rep.status.variable("tiger_proximity").status == "ready"
    kinds = [e["ev"] for e in events]
    assert kinds[0] == "artifact_start" and "extract" in kinds and kinds[-1] == "artifact_end"


def test_second_fetch_is_a_no_op(mini_manifest, data_dir, h, server):
    fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    n = len(server.downloads())
    rep = fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    assert rep.ok and rep.items == [] and rep.plan.already_usable == ["county_boundaries"]
    assert len(server.downloads()) == n


def test_dropped_connection_resumes_with_range(mini_manifest, data_dir, archive, h, server):
    cut = len(archive.data) // 3
    server.behaviour.drop_after, server.behaviour.drops = cut, 1
    rep = fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    assert rep.ok and rep.items[0].attempts == 2
    first, second = server.downloads()
    assert "range" not in first["headers"]
    start = int(second["headers"]["range"].split("=")[1].rstrip("-"))
    assert 0 < start <= cut                              # resumed, not restarted
    assert rep.items[0].bytes_transferred == len(archive.data)


def test_interrupted_call_resumes_on_the_next_call(mini_manifest, data_dir, archive, h, server):
    cut = len(archive.data) // 2
    server.behaviour.drop_after, server.behaviour.drops = cut, 1
    with pytest.raises(NetworkError) as e:
        fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, max_attempts=1, **h.fast())
    assert e.value.kind == "network" and "re-run to resume" in e.value.message
    assert e.value.report.items[0].outcome == "failed"
    part = data_dir / ".spacescans/downloads" / f"{archive.name}.part"
    meta = json.loads((data_dir / ".spacescans/downloads" / f"{archive.name}.part.json").read_text())
    kept = part.stat().st_size
    assert 0 < kept <= cut and meta["sha256"] == archive.sha256 and meta["attempts"] == 1
    rep = fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    assert rep.ok and rep.items[0].outcome == "resumed"
    assert rep.items[0].bytes_transferred == len(archive.data) - kept
    assert server.downloads()[-1]["headers"]["range"] == f"bytes={kept}-"


def _leave_partial(mini_manifest, data_dir, archive, h, server):
    server.behaviour.drop_after, server.behaviour.drops = len(archive.data) // 2, 1
    with pytest.raises(NetworkError):
        fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, max_attempts=1, **h.fast())


def test_server_that_ignores_range_restarts_from_zero(mini_manifest, data_dir, archive, h, server):
    _leave_partial(mini_manifest, data_dir, archive, h, server)
    server.behaviour.ignore_range = True
    events = []
    rep = fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest,
                on_event=events.append, **h.fast())
    assert rep.ok and rep.status.variable("tiger_proximity").status == "ready"
    assert any(e["ev"] == "range_ignored" for e in events)
    assert rep.items[0].bytes_transferred == len(archive.data)


def test_content_range_with_wrong_total_restarts(mini_manifest, data_dir, archive, h, server):
    _leave_partial(mini_manifest, data_dir, archive, h, server)
    server.behaviour.bogus_total = 1
    events = []
    rep = fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest,
                on_event=events.append, **h.fast())
    assert rep.ok
    assert any(e["ev"] == "retry" and "Content-Range" in e["reason"] for e in events)


def test_server_errors_are_retried_with_backoff(mini_manifest, data_dir, h, server):
    server.behaviour.fail_status = [503, 500]
    sleeps = []
    rep = fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest,
                **h.fast(sleep=sleeps.append))
    assert rep.ok and rep.items[0].attempts == 3
    assert sleeps == [1.0, 2.0]


def test_gives_up_after_max_attempts(mini_manifest, data_dir, h, server):
    server.behaviour.fail_status = [503] * 10
    with pytest.raises(NetworkError) as e:
        fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, max_attempts=3, **h.fast())
    assert "after 3 attempts" in e.value.message and e.value.hint


def test_missing_on_mirror(mini_manifest, data_dir, archive, h, server):
    del server.files[archive.name]
    with pytest.raises(NotOnMirrorError) as e:
        fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    assert e.value.kind == "data_missing"


def test_checksum_mismatch_deletes_the_download(mini_manifest, data_dir, archive, h, server):
    server.files[archive.name] = bytes(reversed(archive.data))
    with pytest.raises(DataCorruptError, match="checksum mismatch"):
        fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    assert _parts(data_dir) == []
    assert not (data_dir / "County").exists()


def test_sharepoint_share_flow(share_manifest, data_dir, archive, h, server):
    rep = fetch(["tiger_proximity"], data_dir=data_dir, manifest=share_manifest, **h.fast())
    assert rep.ok
    share_req = [r for r in server.requests if r["path"].startswith("/:f:/")]
    assert share_req and share_req[0]["method"] == "HEAD"
    dl = server.downloads()[0]
    assert dl["path"].startswith("/personal/tester_example_edu/_layouts/15/download.aspx?SourceUrl=")
    assert dl["path"].endswith("/personal/tester_example_edu/Documents/spacescans-pipeline-data/"
                               + archive.name)
    assert h.COOKIE in dl["headers"]["cookie"]
    receipt = read_receipt(data_dir, archive.name)
    assert receipt["source"] == server.share_url        # never the cookie


def test_sharepoint_resume_uses_range_on_download_aspx(share_manifest, data_dir, archive, h, server):
    cut = len(archive.data) // 2
    server.behaviour.drop_after, server.behaviour.drops = cut, 1
    rep = fetch(["tiger_proximity"], data_dir=data_dir, manifest=share_manifest, **h.fast())
    assert rep.ok
    assert len(server.downloads()) == 2
    assert server.downloads()[-1]["headers"]["range"] == f"bytes={cut}-"


def test_sharepoint_expired_cookie_is_reminted(share_manifest, data_dir, h, server):
    server.behaviour.expire_cookie_once = True
    rep = fetch(["tiger_proximity"], data_dir=data_dir, manifest=share_manifest, **h.fast())
    assert rep.ok
    assert len([r for r in server.requests if r["path"].startswith("/:f:/")]) == 2


def test_html_page_instead_of_the_file_is_retried(share_manifest, data_dir, h, server):
    server.behaviour.html_once = True
    rep = fetch(["tiger_proximity"], data_dir=data_dir, manifest=share_manifest, **h.fast())
    assert rep.ok and rep.items[0].attempts == 2


def test_lock_held_by_another_fetch(mini_manifest, data_dir, h, server):
    data_dir.mkdir(parents=True)
    with DataLock(data_dir):
        with pytest.raises(LockedError) as e:
            fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    assert e.value.kind == "locked" and e.value.details["holder"]["purpose"] == "fetch"
    assert server.downloads() == []
    assert fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, **h.fast()).ok


def test_not_enough_disk_space(mini_manifest, data_dir, h, server):
    class Small:
        free = 1000

    with pytest.raises(DiskSpaceError) as e:
        fetch(["ndi"], data_dir=data_dir, manifest=mini_manifest, **h.fast(disk_usage=lambda p: Small()))
    assert e.value.kind == "disk_space" and e.value.details["free_bytes"] == 1000
    assert server.downloads() == []


def test_dry_run_counts_shared_artifacts_once_and_makes_no_request(mini_manifest, data_dir, archive, h, server):
    rep = fetch(["tiger_proximity", "ndi", "fara_tract", "vnl"], data_dir=data_dir,
                manifest=mini_manifest, dry_run=True)
    assert server.requests == [] and not data_dir.exists()
    p = rep.plan
    assert [i.artifact for i in p.items] == [archive.name, "panel.Rda"]
    assert p.items[0].datasets == ["county_boundaries"]
    assert p.download_bytes == len(archive.data) + len(h.PANEL)
    assert p.disk_bytes == archive.payload_bytes + len(h.PANEL) + len(archive.data)
    assert {m["dataset"] for m in p.manual} == {"fara_labels", "vnl"}
    assert not rep.ok                                     # manual steps remain
    assert plan(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest).items[0].artifact == archive.name


def test_bare_file_is_placed_with_a_receipt(mini_manifest, data_dir, h, server):
    rep = fetch(datasets=["ndi_panel"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    assert rep.ok
    p = data_dir / "NDI/C4/panel.Rda"
    assert p.read_bytes() == h.PANEL and stat.S_IMODE(p.stat().st_mode) == 0o644
    assert read_receipt(data_dir, "panel.Rda")["files"] == [["NDI/C4/panel.Rda", len(h.PANEL), h.sha256(h.PANEL)]]


def test_keep_archives(mini_manifest, data_dir, archive, h, server):
    fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, keep_archives=True, **h.fast())
    assert (data_dir / ".spacescans/archives" / archive.name).read_bytes() == archive.data


def test_pending_upload_comes_from_a_folder_mirror(mini_manifest, data_dir, h, server, tmp_path):
    rep = fetch(["fara_tract"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    assert not rep.ok and rep.items == [] and rep.plan.manual[0]["dataset"] == "fara_labels"
    # A spacescans-web pipeline-data tree: the file sits under its dest folder.
    web = tmp_path / "pipeline-data"
    (web / "FARA/C4").mkdir(parents=True)
    (web / "FARA/C4/labels.csv").write_bytes(b"a,b\n1,2\n")
    rep = fetch(["fara_tract"], data_dir=data_dir, manifest=mini_manifest, mirror=web, **h.fast())
    assert rep.ok and rep.items[0].outcome == "copied"
    assert (data_dir / "FARA/C4/labels.csv").read_bytes() == b"a,b\n1,2\n"
    assert read_receipt(data_dir, "labels.csv")["source"] == str(web.resolve())


def test_folder_mirror_with_a_wrong_file_is_refused(mini_manifest, data_dir, h, tmp_path):
    web = tmp_path / "mirror"
    web.mkdir()
    (web / "labels.csv").write_bytes(b"a,b\n9,9\n")
    with pytest.raises(DataCorruptError, match="does not match"):
        fetch(["fara_tract"], data_dir=data_dir, manifest=mini_manifest, mirror=web, **h.fast())
    assert not (data_dir / "FARA/C4/labels.csv").exists()


def test_mirror_from_the_environment(mini_manifest, data_dir, h, server, monkeypatch):
    mini_manifest["mirrors"]["test"]["url"] = "http://127.0.0.1:9/nothing-here"
    monkeypatch.setenv("SPACESCANS_DATA_MIRROR", server.files_url)
    assert fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, **h.fast()).ok


def test_provider_terms_are_never_accepted_or_downloaded(mini_manifest, data_dir, h, server):
    rep = fetch(["vnl"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    assert rep.items == [] and server.requests == []
    m = rep.plan.manual[0]
    assert m["dataset"] == "vnl" and m["terms"]["id"] == "eog-vnl"
    assert m["instructions"] == ["Download it from EOG yourself."]
    with pytest.raises(UsageError, match="unknown terms id"):
        fetch(["vnl"], data_dir=data_dir, manifest=mini_manifest, accept_terms=["anything"])
    with pytest.raises(UsageError):
        fetch(["vnl"], data_dir=data_dir, manifest=mini_manifest, accept_terms="eog-vnl")
    # Even an explicit acceptance does not make spacescans download provider data.
    rep = fetch(["vnl"], data_dir=data_dir, manifest=mini_manifest, accept_terms=["eog-vnl"], **h.fast())
    assert rep.items == [] and server.requests == []
    assert not (data_dir / ".spacescans/acknowledgements.json").exists()


def test_damaged_data_is_fetched_again(mini_manifest, data_dir, h, server):
    fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    shp = data_dir / "County/C3/tl_2010_us_county10/tl_2010_us_county10.shp"
    shp.write_bytes(b"short")
    assert status(["tiger_proximity"], data_dir=data_dir,
                  manifest=mini_manifest).variable("tiger_proximity").status == "corrupt"
    rep = fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    assert rep.ok and shp.read_bytes() == h.SHP


def test_failed_extraction_keeps_the_verified_archive(mini_manifest, data_dir, archive, h, server, monkeypatch):
    import spacescans.data._fetch as fetch_mod

    real = fetch_mod.extract_archive
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DataCorruptError("disk hiccup")
        return real(*a, **k)

    monkeypatch.setattr(fetch_mod, "extract_archive", flaky)
    with pytest.raises(DataCorruptError):
        fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    assert _parts(data_dir) == [archive.name]            # verified archive kept, no receipt
    assert read_receipt(data_dir, archive.name) is None
    n = len(server.downloads())
    rep = fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    assert rep.ok and len(server.downloads()) == n        # re-extracted, not re-downloaded
    assert rep.items[0].outcome == "extracted"


def test_force_downloads_again(mini_manifest, data_dir, h, server):
    fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    n = len(server.downloads())
    rep = fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, force=True, **h.fast())
    assert rep.ok and len(server.downloads()) == n + 1


def test_share_link_5xx_is_retried(share_manifest, data_dir, h, server):
    orig = server.httpd.RequestHandlerClass._handle

    def flaky(self, head):                         # the first share-link resolution gets a 503
        if self.path.startswith("/:f:/") and not getattr(server, "_failed_once", False):
            server._failed_once = True
            self._send_simple(503, b"busy")
            return
        orig(self, head)

    server.httpd.RequestHandlerClass._handle = flaky
    try:
        rep = fetch(["tiger_proximity"], data_dir=data_dir, manifest=share_manifest, **h.fast())
    finally:
        server.httpd.RequestHandlerClass._handle = orig
    assert rep.ok and rep.items[0].attempts == 2


def test_unwritable_download_folder_fails_fast(mini_manifest, data_dir, h, server):
    from spacescans.data import DataError

    dl = data_dir / ".spacescans" / "downloads"
    dl.mkdir(parents=True)
    dl.chmod(0o500)
    try:
        with pytest.raises(DataError) as e:
            fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, **h.fast())
    finally:
        dl.chmod(0o755)
    assert "writable" in e.value.hint
    assert e.value.kind != "network"              # not retried as a network failure
    assert len(server.downloads()) == 0


def test_unwritable_data_dir_is_a_clear_error(mini_manifest, tmp_path, h, server):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        with pytest.raises(UsageError, match="cannot write to the data directory"):
            fetch(["tiger_proximity"], data_dir=ro / "ss-data", manifest=mini_manifest, **h.fast())
    finally:
        ro.chmod(0o755)


def test_tls_verification_failure_is_not_retried(mini_manifest, data_dir, h, monkeypatch):
    import ssl

    from spacescans.data.transport import Transport

    calls = []

    class BadTLS(Transport):
        def open(self, url, **kw):
            calls.append(url)
            raise ssl.SSLCertVerificationError("certificate verify failed: self-signed certificate")

    with pytest.raises(NetworkError) as e:
        fetch(["tiger_proximity"], data_dir=data_dir, manifest=mini_manifest, transport=BadTLS(),
              **h.fast())
    assert len(calls) == 1 and "truststore" in e.value.hint
