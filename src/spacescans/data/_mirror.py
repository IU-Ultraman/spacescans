"""``mirror_check()``: is each ``pending_upload`` artifact on the live mirror yet?

A maintainer check, run after uploading files to the mirror. For each
artifact it reads at most ``max_read`` bytes (1 MB) and never downloads a
dataset:

* an HTTP mirror (the OneDrive share, or any URL): a HEAD request, then a
  range read. A file no larger than ``max_read`` is read whole and its
  sha256 compared; a larger one is checked by size: the ``Content-Range``
  total of a 1-byte range read (or the HEAD ``Content-Length``) must equal
  the manifest's ``bytes``. An HTML answer (a sign-in or error page) never
  counts as the file;
* a folder mirror: the file's size, and its sha256 when it is small.

An artifact *passes* when it is reachable, its size matches, and (when read
whole) its sha256 matches. ``write_manifest`` then flips exactly the passing
``pending_upload`` artifacts to ``available`` in the manifest file (a text
edit of the ``status`` line, so the file's layout is kept) and re-validates
it. Nothing is flipped for an artifact that did not pass.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import re
import os
import socket
import ssl
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spacescans.data.errors import DataError, UsageError
from spacescans.data.manifest import (
    MANIFEST_PATH,
    Artifact,
    Manifest,
    coerce_manifest,
    parse_manifest,
)
from spacescans.data.sources import MIRROR_ENV, FolderMirror, Source, source_for
from spacescans.data.transport import Transport, parse_content_range

MAX_READ = 1024 * 1024


@dataclass
class MirrorCheck:
    artifact: str
    manifest_status: str
    url: str | None = None
    reachable: bool = False
    size_ok: bool | None = None
    sha256_ok: bool | None = None      # only when the whole file was read
    http_status: int | None = None
    remote_bytes: int | None = None
    bytes_read: int = 0
    method: str = ""
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.reachable and self.size_ok is True and self.sha256_ok is not False

    def to_dict(self) -> dict[str, Any]:
        return {"artifact": self.artifact, "manifest_status": self.manifest_status,
                "url": self.url, "reachable": self.reachable, "size_ok": self.size_ok,
                "sha256_ok": self.sha256_ok, "passed": self.passed,
                "http_status": self.http_status, "remote_bytes": self.remote_bytes,
                "bytes_read": self.bytes_read, "method": self.method, "detail": self.detail}


@dataclass
class MirrorCheckReport:
    mirror: str
    checks: list[MirrorCheck] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    manifest_path: str | None = None

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def reachable(self) -> list[str]:
        return [c.artifact for c in self.checks if c.passed]

    @property
    def not_reachable(self) -> list[str]:
        return [c.artifact for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {"mirror": self.mirror, "ok": self.ok, "reachable": self.reachable,
                "not_reachable": self.not_reachable, "checks": [c.to_dict() for c in self.checks],
                "updated": self.updated, "manifest_path": self.manifest_path}


_NET = (OSError, socket.timeout, http.client.HTTPException, ssl.SSLError)


def _check_folder(src: FolderMirror, art: Artifact, c: MirrorCheck, max_read: int) -> None:
    c.method = "folder"
    path = src.path(art)
    c.url = str(path) if path else str(src.folder / art.name)
    if path is None:
        c.detail = "not in the mirror folder"
        return
    c.reachable = True
    c.remote_bytes = path.stat().st_size
    c.size_ok = c.remote_bytes == art.bytes
    if c.size_ok and art.bytes <= max_read:
        data = path.read_bytes()
        c.bytes_read = len(data)
        c.sha256_ok = hashlib.sha256(data).hexdigest() == art.sha256
    c.detail = _verdict(c, art)


def _verdict(c: MirrorCheck, art: Artifact) -> str:
    if not c.reachable:
        return c.detail or "not reachable"
    if c.size_ok is False:
        return f"size {c.remote_bytes!r} differs from the manifest's {art.bytes:,}"
    if c.sha256_ok is False:
        return "sha256 differs from the manifest"
    if c.sha256_ok:
        return f"on the mirror: {art.bytes:,} bytes, sha256 matches"
    return f"on the mirror: {art.bytes:,} bytes (size checked)"


def _is_html(resp: Any) -> bool:
    return (resp.header("Content-Type") or "").lower().startswith("text/html")


def _check_http(src: Source, art: Artifact, c: MirrorCheck, transport: Transport, max_read: int) -> None:
    try:
        c.url = src.url(art)
    except DataError as e:
        c.detail = f"could not resolve the mirror: {e.message}"
        return
    except _NET as e:
        c.detail = f"could not resolve the mirror: {e}"
        return
    whole = art.bytes <= max_read
    for attempt in (1, 2):
        try:
            # 1. HEAD: status, type and size without a body.
            c.method = "HEAD"
            with transport.open(c.url, method="HEAD") as resp:
                c.http_status = resp.status
                head_html = _is_html(resp)
                length = resp.header("Content-Length")
            if c.http_status in (401, 403) and attempt == 1 and src.refresh():
                c.url = src.url(art)
                continue
            if c.http_status in (404, 410):
                c.detail = f"not on the mirror (HTTP {c.http_status})"
                return
            if c.http_status == 200 and not head_html and length and length.isdigit():
                c.remote_bytes = int(length)
            # 2. A range read: the whole file when small, else one byte for the total.
            n = art.bytes if whole else 1
            c.method = "HEAD + GET range"
            with transport.open(c.url, headers={"Range": f"bytes=0-{n - 1}"}) as resp:
                c.http_status = resp.status
                if resp.status in (401, 403) and attempt == 1 and src.refresh():
                    c.url = src.url(art)
                    continue
                if resp.status in (404, 410):
                    c.detail = f"not on the mirror (HTTP {resp.status})"
                    return
                if resp.status not in (200, 206):
                    c.detail = f"HTTP {resp.status}"
                    return
                if _is_html(resp):
                    c.detail = ("the mirror answered with an HTML page (sign-in or error page), "
                                "not the file: not uploaded yet, or not shared")
                    return
                if resp.status == 206:
                    cr = parse_content_range(resp.header("Content-Range"))
                    if cr is not None and cr[2] is not None:
                        c.remote_bytes = cr[2]
                else:
                    length = resp.header("Content-Length")
                    if length and length.isdigit():
                        c.remote_bytes = int(length)
                body = resp.read(n)          # n <= max_read: never more than 1 MB
                c.bytes_read = len(body)
            c.reachable = True
            c.size_ok = c.remote_bytes == art.bytes if c.remote_bytes is not None else None
            if whole and c.size_ok:
                c.sha256_ok = (len(body) == art.bytes
                               and hashlib.sha256(body).hexdigest() == art.sha256)
            c.detail = _verdict(c, art)
            return
        except _NET as e:
            c.detail = f"network error: {e}"
            return
        except DataError as e:
            c.detail = f"could not resolve the mirror: {e.message}"
            return
    c.detail = f"access denied (HTTP {c.http_status})"


def mirror_check(artifacts: Sequence[str] | str | None = None, *,
                 mirror: str | os.PathLike[str] | None = None,
                 manifest: Any = None, transport: Transport | None = None,
                 all_artifacts: bool = False, max_read: int = MAX_READ,
                 write_manifest: bool | str | os.PathLike[str] = False) -> MirrorCheckReport:
    """Check the live mirror for ``artifacts`` (default: every ``pending_upload`` one).

    ``all_artifacts`` checks the available ones too. ``mirror`` (or
    ``$SPACESCANS_DATA_MIRROR``) overrides the manifest's mirror.
    ``write_manifest`` (``True`` for the packaged manifest, or a path) flips
    the passing ``pending_upload`` artifacts to ``available`` in that file.
    Never raises for an unreachable artifact: the report says what failed.
    """
    if max_read > MAX_READ:
        raise UsageError(f"max_read is capped at {MAX_READ:,} bytes (no dataset downloads)")
    man: Manifest = coerce_manifest(manifest)
    if isinstance(artifacts, str):
        artifacts = [artifacts]
    if artifacts is None:
        names = [n for n, a in man.artifacts.items() if all_artifacts or not a.available]
    else:
        names = list(dict.fromkeys(artifacts))
        for n in names:
            man.artifact(n)  # raises UsageError for an unknown name
    transport = transport or Transport()
    spec = mirror if mirror is not None else (os.environ.get(MIRROR_ENV) or None)
    cache: dict[str, Source] = {}
    report = MirrorCheckReport(mirror=str(spec) if spec is not None else "")
    for name in names:
        art = man.artifacts[name]
        c = MirrorCheck(artifact=name, manifest_status=art.status)
        report.checks.append(c)
        try:
            if spec is not None:
                key = "override"
                if key not in cache:
                    cache[key] = source_for(spec, transport)
            else:
                key = art.mirror
                if key not in cache:
                    cache[key] = source_for(man.mirrors[art.mirror], transport)
            src = cache[key]
        except DataError as e:
            c.detail = e.message
            continue
        if not report.mirror:
            report.mirror = src.describe()
        if isinstance(src, FolderMirror):
            _check_folder(src, art, c, max_read)
        else:
            _check_http(src, art, c, transport, max_read)
    if write_manifest:
        path = MANIFEST_PATH if write_manifest is True else Path(write_manifest)
        report.manifest_path = str(path)
        flip = [c.artifact for c in report.checks
                if c.passed and man.artifacts[c.artifact].status == "pending_upload"]
        if flip:
            flip_status(path, flip)
            report.updated = flip
    return report


def flip_status(path: str | os.PathLike[str], names: Sequence[str]) -> None:
    """Set ``status`` to ``available`` for ``names`` in a manifest file, keeping its layout.

    The entry's ``interim`` instructions (how to get the file while it is not on
    the mirror) are removed with it: they no longer apply.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    artifacts_at = text.index('"artifacts"')
    for name in names:
        key = json.dumps(name) + ":"
        i = text.index(key, artifacts_at)
        end = text.index("}", i)            # artifact entries hold no nested objects
        entry = text[i:end]
        old = '"status": "pending_upload"'
        if old not in entry:
            raise UsageError(f"{name} is not pending_upload in {path}")
        entry = entry.replace(old, '"status": "available"', 1)
        entry = re.sub(r',\s*"interim":\s*"(?:[^"\\]|\\.)*"', "", entry, count=1)
        text = text[:i] + entry + text[end:]
    parse_manifest(json.loads(text))         # still a valid manifest
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
