"""spacescans.data.extract: the port of the web's safe tar extraction."""
from __future__ import annotations

import io
import stat
import tarfile

import pytest

from spacescans.data import (
    DataCorruptError,
    UnsafeArchiveError,
    extract_archive,
    extract_tar,
)
from spacescans.data.extract import is_macos_metadata


def _write(tmp_path, data: bytes, name="a.tar.gz"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_extracts_payload_skips_macos_metadata_and_resets_modes(tmp_path, archive, h):
    arc = _write(tmp_path, archive.data)
    root = tmp_path / "root"
    root.mkdir()
    res = extract_archive(arc, root, allowed_roots=["County/C3"])
    base = root / "County/C3/tl_2010_us_county10"
    assert (base / "tl_2010_us_county10.shp").read_bytes() == h.SHP
    assert not list(root.rglob("._*")) and not list(root.rglob(".DS_Store"))
    assert not (root / "__MACOSX").exists()
    assert not list(root.rglob("*.part"))
    assert res.count == archive.files and res.bytes == archive.payload_bytes
    assert res.tops == {"County"}
    assert sorted(res.skipped) == sorted([
        "County/C3/tl_2010_us_county10/._tl_2010_us_county10.shp",
        "County/C3/tl_2010_us_county10/._tl_2010_us_county10.dbf",
        "County/C3/tl_2010_us_county10/.DS_Store",
        "__MACOSX/County/._C3"])
    rels = {r: (s, sha) for r, s, sha in res.files}
    assert rels["County/C3/tl_2010_us_county10/tl_2010_us_county10.shp"] == (len(h.SHP), h.sha256(h.SHP))
    for p in [base / "tl_2010_us_county10.shp", base / "tl_2010_us_county10.dbf"]:
        assert stat.S_IMODE(p.stat().st_mode) == 0o644      # archived as 0777 / 0700
    assert stat.S_IMODE((root / "County/C3").stat().st_mode) == 0o755


def test_web_compatible_wrapper(tmp_path, archive):
    arc = _write(tmp_path, archive.data)
    root = tmp_path / "root"
    root.mkdir()
    count, tops = extract_tar(arc, root)
    assert (count, tops) == (archive.files, {"County"})


@pytest.mark.parametrize("name,message", [
    ("../evil.txt", "unsafe path in archive"),
    ("/etc/evil.txt", "unsafe path in archive"),
    ("NotADataset/x.txt", "outside the known dataset dirs"),
])
def test_rejects_unsafe_member_names(tmp_path, h, name, message):
    arc = _write(tmp_path, h.make_tar([(name, b"x", 0o644)]))
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(UnsafeArchiveError, match=message):
        extract_archive(arc, root)
    assert not (tmp_path / "evil.txt").exists()


def test_rejects_links(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("County/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    arc = _write(tmp_path, buf.getvalue())
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(UnsafeArchiveError, match="symlinks/hardlinks/devices"):
        extract_archive(arc, root)


def test_rejects_members_outside_declared_roots(tmp_path, h):
    arc = _write(tmp_path, h.make_tar([("BG/C3/x.shp", b"x", 0o644)]))
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(UnsafeArchiveError, match="declared roots"):
        extract_archive(arc, root, allowed_roots=["County/C3"])


def test_collision_with_a_directory(tmp_path, h):
    arc = _write(tmp_path, h.make_tar([("County/C3", b"file where a dir is", 0o644)]))
    root = tmp_path / "root"
    (root / "County/C3").mkdir(parents=True)
    with pytest.raises(UnsafeArchiveError, match="collides with an existing directory"):
        extract_archive(arc, root)


def test_corrupt_gzip_is_data_corrupt(tmp_path, archive):
    arc = _write(tmp_path, archive.data[: len(archive.data) // 2])
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(DataCorruptError, match="could not read"):
        extract_archive(arc, root)


def test_macos_metadata_rule_matches_the_web():
    assert is_macos_metadata("County/C3/._x.shp")
    assert is_macos_metadata("__MACOSX/County/x")
    assert is_macos_metadata("BG/.DS_Store")
    assert not is_macos_metadata("County/C3/x.shp")
    assert not is_macos_metadata("County/C3/.hidden")
