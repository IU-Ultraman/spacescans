"""Safe extraction of the distribution archives.

Ported from spacescans-web ``backend/app/routers/data_setup.py:64-136``
(``_validate_member``, ``_is_macos_metadata``, ``_extract_tar``). The member
checks and their messages are kept word for word so the web's upload endpoint
can switch to this module without changing what users see. Additions:

* ``tarfile.data_filter`` (the "data" extraction filter, Python >= 3.12 and
  the 3.10/3.11 security releases) runs as a second check when available;
* optional ``allowed_roots``: every member must lie under one of the
  artifact's declared roots, not merely under a known dataset folder;
* modes are reset: files 0644, directories 0755. Some archives were packed
  with 0777/0770/0700 members; the web's fetch script then had to
  ``chmod -R a+rX`` the whole data root;
* every written file is hashed while it streams, so a receipt (path, size,
  sha256) can be written without reading the tree twice.

Members are written to ``<target>.part`` and moved into place with
``os.replace``, so a pipeline reading the tree never sees a half-written file.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import tarfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence

from spacescans.data.errors import DataCorruptError, UnsafeArchiveError

# Top-level folders an archive may write into (web app/config.py:52-59).
DATASET_DIRS = (
    "ACAG", "BG", "Community_Organization_Density", "County", "FAQSD", "FARA", "NDI",
    "Noise", "TEMIS", "TRACT", "VNL", "Walkability", "ZCTA5",
    # TIGER roads and NHD water features are served from the distributed
    # prefiltered caches rather than raw dataset dirs.
    "cache",
)

CHUNK = 4 * 1024 * 1024
FILE_MODE = 0o644
DIR_MODE = 0o755


@dataclass
class ExtractResult:
    count: int = 0                                  # regular files written
    bytes: int = 0                                  # their total size
    tops: set[str] = field(default_factory=set)     # top-level folders touched
    files: list[tuple[str, int, str]] = field(default_factory=list)  # (relpath, size, sha256)
    skipped: list[str] = field(default_factory=list)                 # macOS metadata members


def is_macos_metadata(name: str) -> bool:
    """macOS packaging junk: AppleDouble sidecars, `.DS_Store`, `__MACOSX/`.

    The archives were packed on macOS from files carrying extended attributes
    (a com.apple.quarantine flag on anything downloaded via a browser), so BSD
    tar wrote one `._x` member per `x`. BSD tar reabsorbs those on extraction;
    GNU tar and Python's tarfile write them out as ordinary files — ~15k junk
    files for the NHD cache alone. Skip them: nothing in the pipeline reads
    them (every consumer resolves exact filenames).
    """
    parts = Path(name).parts
    if not parts:
        return False
    return (
        parts[0] == "__MACOSX"
        or parts[-1].startswith("._")
        or parts[-1] == ".DS_Store"
    )


def validate_member(m: tarfile.TarInfo, root: Path, *,
                    allowed_tops: Iterable[str] = DATASET_DIRS,
                    allowed_roots: Sequence[str] | None = None) -> None:
    """Raise :class:`UnsafeArchiveError` unless ``m`` is safe to write under ``root``."""
    p = Path(m.name)
    if not p.parts:
        raise UnsafeArchiveError("empty member name in archive")
    if p.is_absolute() or ".." in p.parts:
        raise UnsafeArchiveError(f"unsafe path in archive: {m.name!r}")
    tops = set(allowed_tops)
    if p.parts[0] not in tops:
        raise UnsafeArchiveError(
            f"archive entry {m.name!r} is outside the known dataset dirs "
            f"({', '.join(sorted(tops))})"
        )
    if not (m.isreg() or m.isdir()):
        raise UnsafeArchiveError(
            f"archive entry {m.name!r} is not a regular file or directory "
            "(symlinks/hardlinks/devices are not allowed)"
        )
    if not (root / p).resolve().is_relative_to(root.resolve()):
        raise UnsafeArchiveError(f"archive entry escapes the data root: {m.name!r}")
    if allowed_roots is not None:
        posix = PurePosixPath(m.name.rstrip("/"))
        ok = any(_under_or_above(posix, PurePosixPath(r)) for r in allowed_roots)
        if not ok:
            raise UnsafeArchiveError(
                f"archive entry {m.name!r} is outside this archive's declared "
                f"roots ({', '.join(allowed_roots)})"
            )
    data_filter = getattr(tarfile, "data_filter", None)
    if data_filter is not None:
        try:
            data_filter(m, str(root))
        except tarfile.FilterError as e:  # pragma: no cover - the checks above fire first
            raise UnsafeArchiveError(f"archive entry {m.name!r} rejected: {e}") from e


def _under_or_above(member: PurePosixPath, root: PurePosixPath) -> bool:
    """True if member is root, inside root, or a parent folder of root."""
    if member == root:
        return True
    try:
        member.relative_to(root)
        return True
    except ValueError:
        pass
    try:
        root.relative_to(member)  # e.g. the "BG" or "BG/C3" directory entries
        return True
    except ValueError:
        return False


def extract_archive(archive: Path, root: Path, *,
                    allowed_tops: Iterable[str] = DATASET_DIRS,
                    allowed_roots: Sequence[str] | None = None,
                    hash_files: bool = True,
                    on_file: Callable[[str, int], None] | None = None,
                    chunk: int = CHUNK) -> ExtractResult:
    """Validate then write each member of a ``.tar.gz`` under ``root``.

    Raises :class:`UnsafeArchiveError` for a member that fails validation and
    :class:`DataCorruptError` when the archive cannot be read.
    """
    archive = Path(archive)
    root = Path(root)
    result = ExtractResult()
    tops = tuple(allowed_tops)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for m in tar:
                # Skipping precedes validation on purpose: skipped members are
                # never written, and `__MACOSX/` would otherwise trip the
                # top-level dataset-dir check and reject the whole archive.
                if is_macos_metadata(m.name):
                    result.skipped.append(m.name)
                    continue
                validate_member(m, root, allowed_tops=tops, allowed_roots=allowed_roots)
                rel = PurePosixPath(m.name).as_posix().rstrip("/")
                result.tops.add(Path(m.name).parts[0])
                target = root / m.name
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    _chmod(target, DIR_MODE)
                    continue
                if target.is_dir():
                    raise UnsafeArchiveError(
                        f"archive entry {m.name!r} collides with an existing directory"
                    )
                _mkdirs(target.parent, root)
                src = tar.extractfile(m)
                if src is None:  # pragma: no cover - isreg() implies extractable
                    raise UnsafeArchiveError(f"unreadable archive entry: {m.name!r}")
                part = target.with_name(target.name + ".part")
                digest = hashlib.sha256() if hash_files else None
                size = 0
                try:
                    with src, open(part, "wb") as dst:
                        while True:
                            buf = src.read(chunk)
                            if not buf:
                                break
                            dst.write(buf)
                            size += len(buf)
                            if digest is not None:
                                digest.update(buf)
                    _chmod(part, FILE_MODE)
                    os.replace(part, target)
                finally:
                    part.unlink(missing_ok=True)
                result.count += 1
                result.bytes += size
                result.files.append((rel, size, digest.hexdigest() if digest else ""))
                if on_file is not None:
                    on_file(rel, size)
    except (tarfile.TarError, EOFError, zlib.error, gzip.BadGzipFile) as e:
        raise DataCorruptError(
            f"could not read {archive.name} as a .tar.gz: {e}",
            hint="re-run the fetch; a corrupt archive is downloaded again",
            details={"artifact": archive.name},
        ) from e
    return result


def extract_tar(archive: Path, root: Path) -> tuple[int, set[str]]:
    """Drop-in for the web's ``_extract_tar``: returns (file count, top dirs)."""
    res = extract_archive(archive, root, hash_files=False)
    return res.count, res.tops


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:  # pragma: no cover - e.g. a filesystem without modes
        pass


def _mkdirs(path: Path, root: Path) -> None:
    """mkdir -p, giving the folders we create mode 0755."""
    missing = []
    p = path
    while not p.exists() and p != root and p != p.parent:
        missing.append(p)
        p = p.parent
    path.mkdir(parents=True, exist_ok=True)
    for d in missing:
        _chmod(d, DIR_MODE)


def copy_file_atomic(src: Path, dst: Path, *, chunk: int = CHUNK) -> tuple[int, str]:
    """Copy src to dst via dst.part + os.replace; return (size, sha256)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with open(src, "rb") as fi, open(part, "wb") as fo:
            while True:
                buf = fi.read(chunk)
                if not buf:
                    break
                fo.write(buf)
                digest.update(buf)
                size += len(buf)
        _chmod(part, FILE_MODE)
        os.replace(part, dst)
    finally:
        part.unlink(missing_ok=True)
    return size, digest.hexdigest()


def place_file(src: Path, dst: Path) -> None:
    """Move a verified download into place atomically (copy across devices)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    _chmod(src, FILE_MODE)
    try:
        os.replace(src, dst)
    except OSError:
        part = dst.with_name(dst.name + ".part")
        try:
            shutil.copyfile(src, part)
            _chmod(part, FILE_MODE)
            os.replace(part, dst)
        finally:
            part.unlink(missing_ok=True)
        src.unlink(missing_ok=True)
