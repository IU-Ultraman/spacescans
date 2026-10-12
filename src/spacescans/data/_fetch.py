"""``plan()`` and ``fetch()``: download mirrored artifacts, resumably.

Per artifact, under the data directory's lock:

1. skip it if its receipt is valid and the files match (unless ``force``);
2. check free space: what is left to download + the extracted size + 1 GB;
3. download into ``.spacescans/downloads/<name>.part``, with a small
   ``<name>.part.json`` beside it (source, expected size and sha256,
   attempts). An interrupted download resumes with ``Range: bytes=N-``; a 206
   is accepted only when ``Content-Range`` starts at N and its total equals the
   manifest size. A 200 means the server ignored Range, so the file restarts.
   sha256 is computed while streaming (the existing prefix is re-hashed once on
   resume), so verification needs no second pass;
4. connect timeout 30 s, idle timeout 300 s, no overall cap; up to
   ``max_attempts`` attempts with exponential backoff (1, 2, 4 ... 60 s),
   honouring ``Retry-After``. When attempts run out the ``.part`` is kept and a
   :class:`NetworkError` says to re-run;
5. on a sha256 mismatch the ``.part`` is deleted (:class:`DataCorruptError`);
6. archives are extracted with :func:`spacescans.data.extract.extract_archive`
   (validated members, macOS metadata skipped, ``.part`` + ``os.replace`` per
   file); bare files are moved into place atomically;
7. the receipt is written last, then the archive is deleted (or kept under
   ``.spacescans/archives/`` with ``keep_archives``).

Provider datasets are never downloaded here: their status and instructions
come back as manual steps. Terms are never accepted implicitly: there is no
configuration key or environment variable that pre-accepts them; an
acceptance counts only for the call that passes it in ``accept_terms``.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import socket
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from spacescans.data.errors import (DataCorruptError, DataError, DiskSpaceError, NetworkError,
                                    NotOnMirrorError, TermsRequiredError, UsageError)
from spacescans.data.extract import copy_file_atomic, extract_archive, place_file
from spacescans.data.lock import DataLock
from spacescans.data.manifest import Artifact, Manifest, coerce_manifest, select
from spacescans.data.paths import archives_dir, downloads_dir, resolve_data_dir
from spacescans.data.receipts import (record_acknowledgement, remove_receipt, write_json_atomic,
                                      write_receipt)
from spacescans.data.sources import MIRROR_ENV, FolderMirror, SharePointShare, Source, source_for
from spacescans.data._status import StatusReport, sha256_file, status
from spacescans.data.transport import Transport, parse_content_range

CHUNK = 1024 * 1024
HEADROOM = 1024 ** 3
EventFn = Callable[[dict], None]

_RETRYABLE = (OSError, socket.timeout, http.client.HTTPException, ssl.SSLError)


class _Retry(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def default_backoff(attempt: int) -> float:
    return float(min(60, 2 ** max(0, attempt - 1)))


@dataclass
class PlanItem:
    artifact: str
    datasets: list[str]
    kind: str
    bytes: int                  # full download size
    disk_bytes: int             # size once placed / extracted
    resume_from: int            # bytes already in a matching .part
    verified_archive: bool      # a complete, verified archive is already downloaded
    source: str                 # where it will come from

    @property
    def remaining_bytes(self) -> int:
        return 0 if self.verified_archive else self.bytes - self.resume_from

    def to_dict(self) -> dict[str, Any]:
        return {"artifact": self.artifact, "datasets": self.datasets, "kind": self.kind,
                "bytes": self.bytes, "disk_bytes": self.disk_bytes,
                "resume_from": self.resume_from, "remaining_bytes": self.remaining_bytes,
                "verified_archive": self.verified_archive, "source": self.source}


@dataclass
class FetchPlan:
    data_dir: str
    items: list[PlanItem]
    manual: list[dict[str, Any]]           # datasets fetch cannot complete
    already_usable: list[str]              # datasets that need nothing
    download_bytes: int
    disk_bytes: int                        # peak free space the plan needs
    free_bytes: int | None
    # Provider terms. Reserved for scripted provider downloads, which v1 does
    # not perform: both stay empty, and provider datasets go to ``manual``.
    terms_required: list[dict[str, Any]] = field(default_factory=list)  # not yet accepted
    terms_used: list[dict[str, Any]] = field(default_factory=list)      # accepted in this call

    def to_dict(self) -> dict[str, Any]:
        return {"data_dir": self.data_dir, "items": [i.to_dict() for i in self.items],
                "manual": self.manual, "already_usable": self.already_usable,
                "download_bytes": self.download_bytes, "disk_bytes": self.disk_bytes,
                "free_bytes": self.free_bytes, "terms_required": self.terms_required,
                "terms_used": self.terms_used}


@dataclass
class ItemResult:
    artifact: str
    outcome: str                # downloaded | resumed | copied | extracted | failed
    bytes_transferred: int = 0
    seconds: float = 0.0
    files: int = 0
    attempts: int = 0
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"artifact": self.artifact, "outcome": self.outcome,
                "bytes_transferred": self.bytes_transferred, "seconds": round(self.seconds, 3),
                "files": self.files, "attempts": self.attempts, "error": self.error}


@dataclass
class FetchReport:
    data_dir: str
    dry_run: bool
    plan: FetchPlan
    items: list[ItemResult] = field(default_factory=list)
    status: StatusReport | None = None

    @property
    def ok(self) -> bool:
        """Nothing failed, and (after a real fetch) everything requested is usable."""
        if any(i.outcome == "failed" for i in self.items):
            return False
        if self.dry_run:
            return not self.plan.manual and not self.plan.terms_required
        return self.status is not None and self.status.ready

    def to_dict(self) -> dict[str, Any]:
        return {"data_dir": self.data_dir, "dry_run": self.dry_run, "ok": self.ok,
                "plan": self.plan.to_dict(), "items": [i.to_dict() for i in self.items],
                "manual": self.plan.manual,
                "status": self.status.to_dict() if self.status is not None else None}


# ---------------------------------------------------------------------------

def _part_paths(root: Path, name: str) -> tuple[Path, Path, Path]:
    d = downloads_dir(root)
    return d / f"{name}.part", d / f"{name}.part.json", d / name


def _read_meta(meta: Path, art: Artifact) -> dict | None:
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("sha256") != art.sha256 or data.get("bytes") != art.bytes:
        return None
    return data


def _resume_offset(root: Path, art: Artifact) -> tuple[int, bool]:
    """(bytes in a matching .part, whether a complete archive is waiting)."""
    part, meta, done = _part_paths(root, art.name)
    if done.is_file() and done.stat().st_size == art.bytes:
        return art.bytes, True
    if part.is_file() and _read_meta(meta, art) is not None:
        return min(part.stat().st_size, art.bytes), False
    return 0, False


def _choose_source(man: Manifest, art: Artifact, override: Source | None,
                   transport: Transport, cache: dict[str, Source]) -> Source | None:
    if override is not None:
        return override
    if not art.available:
        return None
    if art.mirror not in cache:
        cache[art.mirror] = source_for(man.mirrors[art.mirror], transport)
    return cache[art.mirror]


def _override(mirror: str | os.PathLike[str] | None, transport: Transport) -> Source | None:
    if mirror is None:
        mirror = os.environ.get(MIRROR_ENV) or None
    return source_for(mirror, transport) if mirror is not None else None


def plan(variables: Sequence[str] | str | None = None, *,
         datasets: Sequence[str] | str | None = None,
         data_dir: str | os.PathLike[str] | None = None,
         accept_terms: Sequence[str] = (),
         force: bool = False,
         keep_archives: bool = False,
         mirror: str | os.PathLike[str] | None = None,
         manifest: Any = None,
         _transport: Transport | None = None) -> FetchPlan:
    """What ``fetch()`` would do: artifacts to download (shared ones once),
    bytes, peak disk, manual steps and terms. Makes no network request."""
    man = coerce_manifest(manifest)
    root = resolve_data_dir(data_dir)
    _check_terms_ids(man, accept_terms)
    transport = _transport or Transport()
    override = _override(mirror, transport)
    rep = status(variables, datasets=datasets, data_dir=root, manifest=man)
    var_keys, ds_keys = select(man, variables, datasets)
    in_use = list(dict.fromkeys([k for v in rep.variables for k in v.chosen] + ds_keys))

    items: list[PlanItem] = []
    manual: list[dict[str, Any]] = []
    usable: list[str] = []
    seen: set[str] = set()
    cache: dict[str, Source] = {}
    for key in in_use:
        d = rep.dataset(key)
        ds = man.dataset(key)
        if d.usable and not (force and ds.acquire == "mirror"):
            usable.append(key)
            continue
        if ds.acquire != "mirror":
            manual.append({"dataset": key, "status": d.status, "title": ds.title,
                           "acquire": ds.acquire, "terms": d.terms,
                           "instructions": d.instructions, "missing": d.missing})
            continue
        names = list(ds.artifacts) if force else d.needed_artifacts
        blocked = []
        for name in names:
            if name in seen:
                for it in items:
                    if it.artifact == name and key not in it.datasets:
                        it.datasets.append(key)
                continue
            seen.add(name)
            art = man.artifacts[name]
            src = _choose_source(man, art, override, transport, cache)
            if src is None or (isinstance(src, FolderMirror) and src.path(art) is None):
                blocked.append(art)
                continue
            offset, done = _resume_offset(root, art)
            items.append(PlanItem(artifact=name, datasets=[key], kind=art.kind, bytes=art.bytes,
                                  disk_bytes=art.disk_bytes, resume_from=offset,
                                  verified_archive=done, source=src.describe()))
        if blocked:
            manual.append({"dataset": key, "status": d.status, "title": ds.title,
                           "acquire": ds.acquire, "terms": d.terms,
                           "instructions": [f"{a.name}: {a.interim or 'not on the mirror'}"
                                            for a in blocked],
                           "missing": [a.relpath or a.name for a in blocked]})

    download = sum(i.remaining_bytes for i in items)
    if keep_archives:
        peak = sum(i.remaining_bytes for i in items) + sum(i.disk_bytes for i in items)
    else:
        peak = sum(i.disk_bytes for i in items) + max(
            [i.remaining_bytes for i in items if i.kind == "tar"] or [0])
    return FetchPlan(data_dir=str(root), items=items, manual=manual, already_usable=usable,
                     download_bytes=download, disk_bytes=peak, free_bytes=rep.free_bytes)


def _check_terms_ids(man: Manifest, accept_terms: Sequence[str]) -> None:
    if isinstance(accept_terms, str):
        raise UsageError("accept_terms must be a list of terms ids, not a string")
    known = man.terms_ids()
    bad = [t for t in accept_terms if t not in known]
    if bad:
        raise UsageError(f"unknown terms id(s) {bad}; known: {sorted(known)}",
                         details={"unknown": bad, "known": sorted(known)})


def fetch(variables: Sequence[str] | str | None = None, *,
          datasets: Sequence[str] | str | None = None,
          data_dir: str | os.PathLike[str] | None = None,
          accept_terms: Sequence[str] = (),
          keep_archives: bool = False,
          dry_run: bool = False,
          mirror: str | os.PathLike[str] | None = None,
          force: bool = False,
          on_event: EventFn | None = None,
          manifest: Any = None,
          transport: Transport | None = None,
          max_attempts: int = 10,
          backoff: Callable[[int], float] = default_backoff,
          sleep: Callable[[float], None] = time.sleep,
          headroom_bytes: int = HEADROOM,
          disk_usage: Callable[[Path], Any] = shutil.disk_usage) -> FetchReport:
    """Download and place the mirrored data the variables/datasets need.

    Returns a :class:`FetchReport`. Raises (with ``.report`` set to what was
    done so far) :class:`NetworkError`, :class:`DataCorruptError`,
    :class:`DiskSpaceError`, :class:`LockedError`, :class:`NotOnMirrorError`
    or :class:`TermsRequiredError`. Provider datasets are reported in
    ``report.plan.manual`` with instructions, never downloaded.
    """
    man = coerce_manifest(manifest)
    root = resolve_data_dir(data_dir)
    transport = transport or Transport()
    emit = on_event or (lambda e: None)
    p = plan(variables, datasets=datasets, data_dir=root, accept_terms=accept_terms,
             force=force, keep_archives=keep_archives, mirror=mirror, manifest=man,
             _transport=transport)
    report = FetchReport(data_dir=str(root), dry_run=dry_run, plan=p)
    if p.terms_required:  # pragma: no cover - no scripted provider downloads in v1
        raise TermsRequiredError(
            "provider terms must be accepted for this download: "
            + ", ".join(t["id"] for t in p.terms_required),
            hint="pass accept_terms=[...] (CLI: --accept-terms ID) after reading the terms",
            details={"terms": p.terms_required})
    if dry_run:
        return report
    used_terms = {t["id"] for t in p.terms_used}
    for tid in accept_terms:  # explicit, per call; recorded locally only
        if tid in used_terms:
            root.mkdir(parents=True, exist_ok=True)
            record_acknowledgement(root, man.datasets[man.terms_ids()[tid]].terms)

    override = _override(mirror, transport)
    cache: dict[str, Source] = {}
    if p.items:
        root.mkdir(parents=True, exist_ok=True)
        free = disk_usage(root).free
        if free < p.disk_bytes + headroom_bytes:
            raise DiskSpaceError(
                f"not enough free space in {root}: the plan needs about "
                f"{(p.disk_bytes + headroom_bytes) / 1e9:.1f} GB "
                f"({p.download_bytes / 1e9:.1f} GB still to download, {p.disk_bytes / 1e9:.1f} GB peak, "
                f"plus 1 GB headroom); {free / 1e9:.1f} GB available",
                hint="free space or choose another data directory, then re-run",
                details={"need_bytes": p.disk_bytes + headroom_bytes, "free_bytes": free,
                         "plan": p.to_dict()})
        with DataLock(root, purpose="fetch"):
            for i, item in enumerate(p.items):
                art = man.artifacts[item.artifact]
                src = _choose_source(man, art, override, transport, cache)
                emit({"ev": "artifact_start", "artifact": art.name, "i": i, "n": len(p.items),
                      "bytes": art.bytes, "resume_from": item.resume_from})
                t0 = time.monotonic()
                result = ItemResult(artifact=art.name, outcome="failed")
                report.items.append(result)
                try:
                    _fetch_one(man, root, art, src, transport, result, emit=emit,
                               keep_archives=keep_archives, max_attempts=max_attempts,
                               backoff=backoff, sleep=sleep, headroom_bytes=headroom_bytes,
                               disk_usage=disk_usage)
                except DataError as e:
                    result.outcome = "failed"
                    result.error = e.to_dict()
                    result.seconds = time.monotonic() - t0
                    emit({"ev": "artifact_end", "artifact": art.name, "ok": False,
                          "error": e.kind, "message": e.message})
                    e.report = report  # type: ignore[attr-defined]
                    raise
                result.seconds = time.monotonic() - t0
                emit({"ev": "artifact_end", "artifact": art.name, "ok": True,
                      "outcome": result.outcome, "seconds": round(result.seconds, 3)})
    report.status = status(variables, datasets=datasets, data_dir=root, manifest=man)
    return report


def _fetch_one(man: Manifest, root: Path, art: Artifact, src: Source | None, transport: Transport,
               result: ItemResult, *, emit: EventFn, keep_archives: bool, max_attempts: int,
               backoff: Callable[[int], float], sleep: Callable[[float], None],
               headroom_bytes: int, disk_usage: Callable[[Path], Any]) -> None:
    if src is None:
        raise NotOnMirrorError(f"{art.name} is not on the mirror yet: {art.interim}",
                               details={"artifact": art.name})
    part, meta, done = _part_paths(root, art.name)
    part.parent.mkdir(parents=True, exist_ok=True)
    offset, complete = _resume_offset(root, art)
    need = (0 if complete else art.bytes - offset) + art.disk_bytes + headroom_bytes
    free = disk_usage(root).free
    if free < need:
        raise DiskSpaceError(
            f"{art.name} needs about {need / 1e9:.1f} GB free in {root} "
            f"(download {art.bytes / 1e9:.1f} GB, placed {art.disk_bytes / 1e9:.1f} GB, "
            f"1 GB headroom); {free / 1e9:.1f} GB available",
            hint="free space, then re-run; the partial download is kept",
            details={"artifact": art.name, "need_bytes": need, "free_bytes": free})

    if complete:
        emit({"ev": "verify", "artifact": art.name, "file": str(done), "bytes": art.bytes})
        if sha256_file(done) != art.sha256:
            done.unlink(missing_ok=True)
            raise DataCorruptError(
                f"the downloaded {art.name} no longer matches its sha256; it was deleted",
                hint="re-run the fetch to download it again", details={"artifact": art.name})
    elif isinstance(src, FolderMirror):
        path = src.path(art)
        if path is None:
            raise NotOnMirrorError(f"{art.name} is not in {src.folder}", details={"artifact": art.name})
        if path.stat().st_size != art.bytes:
            raise DataCorruptError(
                f"{path} is {path.stat().st_size:,} bytes; {art.name} should be {art.bytes:,}",
                details={"artifact": art.name, "path": str(path)})
        emit({"ev": "copy", "artifact": art.name, "from": str(path), "bytes": art.bytes})
        _size, sha = copy_file_atomic(path, done)
        if sha != art.sha256:
            done.unlink(missing_ok=True)
            raise DataCorruptError(f"{path} does not match the sha256 of {art.name}",
                                   details={"artifact": art.name, "path": str(path)})
        result.outcome = "copied"
        result.bytes_transferred = art.bytes
    else:
        resumed = offset > 0
        transferred, attempts = _download(transport, src, art, part, meta, emit=emit,
                                          max_attempts=max_attempts, backoff=backoff, sleep=sleep)
        os.replace(part, done)
        meta.unlink(missing_ok=True)
        result.outcome = "resumed" if resumed else "downloaded"
        result.bytes_transferred = transferred
        result.attempts = attempts

    # Place: the receipt goes last; until then a re-run redoes this step.
    remove_receipt(root, art.name)
    source = src.describe()
    if art.kind == "bare":
        dest = root / art.relpath
        place_file(done, dest)
        files = [(art.relpath, art.bytes, art.sha256)]
        skipped: list[str] = []
        result.files = 1
    else:
        emit({"ev": "extract", "artifact": art.name, "bytes": art.disk_bytes})
        n_done = [0]

        def _on_file(rel: str, size: int) -> None:
            n_done[0] += 1
            if n_done[0] % 500 == 0:
                emit({"ev": "extract_progress", "artifact": art.name, "files": n_done[0],
                      "total": art.files})

        res = extract_archive(done, root, allowed_roots=art.roots, on_file=_on_file)
        files, skipped = res.files, res.skipped
        result.files = res.count
        if art.files is not None and res.count != art.files:
            raise DataCorruptError(
                f"{art.name} held {res.count} files; the manifest says {art.files}",
                details={"artifact": art.name})
        if keep_archives:
            archives_dir(root).mkdir(parents=True, exist_ok=True)
            os.replace(done, archives_dir(root) / art.name)
        else:
            done.unlink(missing_ok=True)
        if result.outcome not in ("downloaded", "resumed", "copied"):
            result.outcome = "extracted"
    write_receipt(root, art, files=files, source=source, origin="fetch",
                  data_release=man.data_release, skipped=skipped)


def _download(transport: Transport, src: Source, art: Artifact, part: Path, meta: Path, *,
              emit: EventFn, max_attempts: int, backoff: Callable[[int], float],
              sleep: Callable[[float], None]) -> tuple[int, int]:
    """Download ``art`` into ``part``, resuming; return (bytes sent this call, attempts)."""
    state = _read_meta(meta, art)
    if state is None and part.exists():
        part.unlink()  # a .part from another version, or without bookkeeping
    digest = hashlib.sha256()
    offset = 0
    if part.exists():
        offset = part.stat().st_size
        if offset > art.bytes:
            part.unlink()
            offset = 0
        elif offset:
            emit({"ev": "rehash", "artifact": art.name, "bytes": offset})
            with open(part, "rb") as f:
                while True:
                    buf = f.read(8 * CHUNK)
                    if not buf:
                        break
                    digest.update(buf)
    attempts = int((state or {}).get("attempts", 0))
    tries = 0
    transferred = 0
    last_emit = 0.0
    t_start = time.monotonic()

    def _restart() -> None:
        nonlocal offset, digest
        part.unlink(missing_ok=True)
        offset = 0
        digest = hashlib.sha256()

    while offset < art.bytes:
        attempts += 1
        tries += 1
        write_json_atomic(meta, {"artifact": art.name, "sha256": art.sha256, "bytes": art.bytes,
                                 "source": src.describe(), "attempts": attempts,
                                 "offset": offset, "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        try:
            url = src.url(art)
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            resp = transport.open(url, headers=headers)
            try:
                code = resp.status
                if code in (401, 403):
                    if src.refresh():
                        raise _Retry(f"HTTP {code} (access cookie expired?)")
                    raise NetworkError(f"HTTP {code} downloading {art.name}",
                                       details={"artifact": art.name, "status": code})
                if code in (404, 410):
                    raise NotOnMirrorError(
                        f"{art.name} is not on the mirror (HTTP {code})",
                        hint="the mirror may have moved; set SPACESCANS_DATA_MIRROR or pass --mirror",
                        details={"artifact": art.name, "status": code})
                if code == 416:
                    _restart()
                    raise _Retry("HTTP 416 (range not satisfiable); restarting from zero")
                if code == 429 or code >= 500:
                    ra = resp.header("Retry-After")
                    raise _Retry(f"HTTP {code}", retry_after=float(ra) if ra and ra.isdigit() else None)
                ctype = (resp.header("Content-Type") or "").lower()
                if code in (200, 206) and ctype.startswith("text/html"):
                    # SharePoint answers an expired cookie with a sign-in page.
                    src.refresh()
                    raise _Retry("the server sent an HTML page instead of the file")
                if code == 206:
                    cr = parse_content_range(resp.header("Content-Range"))
                    if cr is None or cr[0] != offset or (cr[2] is not None and cr[2] != art.bytes):
                        _restart()
                        raise _Retry(f"unexpected Content-Range {resp.header('Content-Range')!r}; "
                                     "restarting from zero")
                elif code == 200:
                    length = resp.header("Content-Length")
                    if length is not None and length.isdigit() and int(length) != art.bytes:
                        src.refresh()
                        raise _Retry(f"server sent {int(length):,} bytes for {art.name}, "
                                     f"expected {art.bytes:,}")
                    if offset:
                        emit({"ev": "range_ignored", "artifact": art.name, "offset": offset})
                        _restart()
                else:
                    raise NetworkError(f"HTTP {code} downloading {art.name}",
                                       details={"artifact": art.name, "status": code})
                with open(part, "ab" if offset else "wb", buffering=0) as f:
                    while True:
                        buf = resp.read(CHUNK)
                        if not buf:
                            break
                        if offset + len(buf) > art.bytes:
                            _restart()
                            raise DataCorruptError(
                                f"the server sent more than the {art.bytes:,} bytes of {art.name}",
                                details={"artifact": art.name})
                        view = memoryview(buf)
                        while view:
                            n = f.write(view)
                            digest.update(view[:n])
                            offset += n
                            transferred += n
                            view = view[n:]
                        now = time.monotonic()
                        if now - last_emit >= 0.25:
                            last_emit = now
                            rate = transferred / max(now - t_start, 1e-6)
                            emit({"ev": "download", "artifact": art.name, "bytes": offset,
                                  "total": art.bytes, "rate": round(rate)})
                if offset < art.bytes:
                    raise _Retry(f"connection closed after {offset:,} of {art.bytes:,} bytes")
            finally:
                resp.close()
        except DataError:
            raise
        except (_Retry, *_RETRYABLE) as e:
            if isinstance(e, OSError) and getattr(e, "errno", None) == 28:  # ENOSPC
                raise DiskSpaceError(f"disk full while downloading {art.name}",
                                     hint="free space and re-run; the download resumes",
                                     details={"artifact": art.name}) from e
            if tries >= max_attempts:
                raise NetworkError(
                    f"downloading {art.name} failed after {tries} attempts: {e}. "
                    f"{offset:,} of {art.bytes:,} bytes are kept; re-run to resume",
                    hint="re-run the same fetch command; it resumes",
                    details={"artifact": art.name, "offset": offset, "bytes": art.bytes,
                             "attempts": attempts}) from e
            delay = backoff(tries)
            if isinstance(e, _Retry) and e.retry_after is not None:
                delay = min(max(delay, e.retry_after), 300.0)
            emit({"ev": "retry", "artifact": art.name, "attempt": tries, "delay": delay,
                  "reason": str(e), "offset": offset})
            sleep(delay)
            continue
    emit({"ev": "download", "artifact": art.name, "bytes": offset, "total": art.bytes,
          "rate": round(transferred / max(time.monotonic() - t_start, 1e-6))})
    if digest.hexdigest() != art.sha256:
        part.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        raise DataCorruptError(
            f"checksum mismatch for {art.name}: the download is corrupt or not the published "
            f"file (got {digest.hexdigest()[:16]}..., expected {art.sha256[:16]}...); it was deleted",
            hint="re-run the fetch to download it again",
            details={"artifact": art.name, "sha256": digest.hexdigest(), "expected": art.sha256})
    return transferred, attempts
