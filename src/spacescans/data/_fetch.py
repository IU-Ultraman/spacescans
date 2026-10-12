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

Provider datasets: a ``provider-scripted`` dataset (EPA FAQSD, KNMI TEMIS,
WashU ACAG) is downloaded from its provider only when its terms id is in this
call's ``accept_terms``; otherwise ``fetch()`` raises
:class:`TermsRequiredError` before downloading anything (a dry run lists the
terms in ``plan.terms_required``). Its files are listed by
:mod:`spacescans.data._providers` and streamed by the same engine, file by
file: files already in place are skipped, so a re-run continues where the last
one stopped; a server that ignores Range (EPA) restarts each attempt at byte
0; sizes, file signatures and gzip checksums are checked; ``.gz`` downloads
are unpacked into place. A receipt per dataset lists every placed file with
its size and sha256 (``.spacescans/receipts/providers/<dataset>.json``).
``provider-manual`` datasets (VNL, which needs an EOG login) come back as
manual steps. Terms are never accepted implicitly: there is no configuration
key or environment variable that pre-accepts them; an acceptance counts only
for the call that passes it in ``accept_terms``.
"""
from __future__ import annotations

import errno
import gzip
import hashlib
import http.client
import json
import os
import shutil
import socket
import ssl
import time
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spacescans.data._providers import RemoteFile, remote_files
from spacescans.data._status import StatusReport, sha256_file, status
from spacescans.data.errors import (
    DataCorruptError,
    DataError,
    DataMissingError,
    DiskSpaceError,
    NetworkError,
    NotOnMirrorError,
    TermsRequiredError,
    TransientNetworkError,
    UsageError,
)
from spacescans.data.extract import (
    FILE_MODE,
    copy_file_atomic,
    extract_archive,
    place_file,
)
from spacescans.data.lock import DataLock
from spacescans.data.manifest import (
    Artifact,
    Dataset,
    Manifest,
    coerce_manifest,
    select,
)
from spacescans.data.paths import (
    archives_dir,
    downloads_dir,
    receipts_dir,
    resolve_data_dir,
)
from spacescans.data.receipts import (
    RECEIPT_VERSION,
    record_acknowledgement,
    remove_receipt,
    write_json_atomic,
    write_receipt,
)
from spacescans.data.sources import (
    MIRROR_ENV,
    FolderMirror,
    Source,
    source_for,
)
from spacescans.data.transport import Transport, parse_content_range

CHUNK = 1024 * 1024
HEADROOM = 1024 ** 3
EventFn = Callable[[dict], None]

_RETRYABLE = (OSError, socket.timeout, http.client.HTTPException, ssl.SSLError)


class _Retry(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _local_file_error(e: OSError) -> bool:
    """An error writing our own files, as opposed to a network failure."""
    return bool(getattr(e, "filename", None)) or e.errno in (
        errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EACCES, errno.EPERM)


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
    # Provider-scripted datasets: downloaded only when their terms id is in
    # this call's accept_terms ("providers"); otherwise listed in
    # "terms_required" and fetch() raises TermsRequiredError.
    terms_required: list[dict[str, Any]] = field(default_factory=list)  # not yet accepted
    terms_used: list[dict[str, Any]] = field(default_factory=list)      # accepted in this call
    providers: list[dict[str, Any]] = field(default_factory=list)       # provider downloads to run

    def to_dict(self) -> dict[str, Any]:
        return {"data_dir": self.data_dir, "items": [i.to_dict() for i in self.items],
                "manual": self.manual, "already_usable": self.already_usable,
                "download_bytes": self.download_bytes, "disk_bytes": self.disk_bytes,
                "free_bytes": self.free_bytes, "terms_required": self.terms_required,
                "terms_used": self.terms_used, "providers": self.providers}


@dataclass
class ItemResult:
    artifact: str               # artifact name, or the dataset key of a provider download
    outcome: str                # downloaded | resumed | copied | extracted | present | failed
    bytes_transferred: int = 0
    seconds: float = 0.0
    files: int = 0
    attempts: int = 0
    error: dict[str, Any] | None = None
    provider: bool = False      # a provider download (``artifact`` is the dataset key)
    skipped: int = 0            # provider files already in place
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"artifact": self.artifact, "outcome": self.outcome,
                "bytes_transferred": self.bytes_transferred, "seconds": round(self.seconds, 3),
                "files": self.files, "attempts": self.attempts, "error": self.error,
                "provider": self.provider, "skipped": self.skipped, "warnings": self.warnings}


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
    _, ds_keys = select(man, variables, datasets)
    in_use = list(dict.fromkeys([k for v in rep.variables for k in v.chosen] + ds_keys))

    items: list[PlanItem] = []
    manual: list[dict[str, Any]] = []
    usable: list[str] = []
    providers: list[dict[str, Any]] = []
    terms_required: list[dict[str, Any]] = []
    terms_used: list[dict[str, Any]] = []
    accepted = set(accept_terms)
    seen: set[str] = set()
    cache: dict[str, Source] = {}
    for key in in_use:
        d = rep.dataset(key)
        ds = man.dataset(key)
        if d.usable and not (force and ds.acquire == "mirror"):
            usable.append(key)
            continue
        if ds.scripted:
            entry = _provider_entry(ds, d)
            terms = dict(ds.terms.to_dict(), dataset=key)
            if ds.terms.id in accepted:
                providers.append(entry)
                terms_used.append(terms)
            else:
                terms_required.append(terms)
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
    download += sum(e["download_bytes"] for e in providers)
    peak += sum(e["disk_bytes"] + e["temp_bytes"] for e in providers)
    return FetchPlan(data_dir=str(root), items=items, manual=manual, already_usable=usable,
                     download_bytes=download, disk_bytes=peak, free_bytes=rep.free_bytes,
                     terms_required=terms_required, terms_used=terms_used, providers=providers)


def _provider_entry(ds: Dataset, d: Any) -> dict[str, Any]:
    """Plan entry for a provider download: estimates scaled to what is missing."""
    full_dl = int(ds.estimate.get("download_bytes", 0))
    full_disk = int(ds.estimate.get("disk_bytes", 0))
    frac = (d.missing_count / d.expected) if d.expected else 1.0
    dl = ds.download
    temp = max(dl.reference_download_bytes.values(), default=0) if dl.gunzip else 0
    return {"dataset": ds.key, "title": ds.title, "status": d.status, "method": dl.method,
            "source": dl.base or dl.url, "range": dl.range, "terms": ds.terms.id,
            "missing": d.missing_count, "expected": d.expected,
            "download_bytes": int(full_dl * min(1.0, frac)),
            "disk_bytes": max(0, full_disk - d.present_bytes), "temp_bytes": temp}


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
          backoff: Callable[[int], float] | None = None,
          sleep: Callable[[float], None] | None = None,
          headroom_bytes: int | None = None,
          disk_usage: Callable[[Path], Any] | None = None) -> FetchReport:
    """Download and place the mirrored data the variables/datasets need.

    Returns a :class:`FetchReport`. Raises (with ``.report`` set to what was
    done so far) :class:`NetworkError`, :class:`DataCorruptError`,
    :class:`DiskSpaceError`, :class:`LockedError`, :class:`NotOnMirrorError`
    or :class:`TermsRequiredError`. A provider-scripted dataset is downloaded
    from its provider only when its terms id is in ``accept_terms``
    (otherwise :class:`TermsRequiredError`, raised before any download; a dry
    run lists it in ``report.plan.terms_required``). Provider-manual datasets
    (VNL) are reported in ``report.plan.manual`` with instructions.
    """
    man = coerce_manifest(manifest)
    root = resolve_data_dir(data_dir)
    transport = transport or Transport()
    emit = on_event or (lambda e: None)
    # Resolved per call (not as defaults) so tests and callers can patch them.
    backoff = backoff or default_backoff
    sleep = sleep or time.sleep
    headroom_bytes = HEADROOM if headroom_bytes is None else headroom_bytes
    disk_usage = disk_usage or shutil.disk_usage
    p = plan(variables, datasets=datasets, data_dir=root, accept_terms=accept_terms,
             force=force, keep_archives=keep_archives, mirror=mirror, manifest=man,
             _transport=transport)
    report = FetchReport(data_dir=str(root), dry_run=dry_run, plan=p)
    if dry_run:
        return report
    if p.terms_required:
        ids = [t["id"] for t in p.terms_required]
        err = TermsRequiredError(
            "provider terms must be accepted for this download: "
            + "; ".join(f"{t['id']} ({t['dataset']}: {t['url']})" for t in p.terms_required),
            hint=(f"read the terms, then pass accept_terms={ids!r} (CLI: "
                  + " ".join(f"--accept-terms {i}" for i in ids) + ")"),
            details={"terms": p.terms_required})
        err.report = report  # type: ignore[attr-defined]
        raise err
    used_terms = {t["id"] for t in p.terms_used}
    for tid in accept_terms:  # explicit, per call; recorded locally only
        if tid in used_terms:
            root.mkdir(parents=True, exist_ok=True)
            record_acknowledgement(root, man.datasets[man.terms_ids()[tid]].terms)

    override = _override(mirror, transport)
    cache: dict[str, Source] = {}
    if p.items or p.providers:
        try:
            _check_plan_space(root, p, headroom_bytes, disk_usage)
            with DataLock(root, purpose="fetch"):
                if p.items:
                    _fetch_items(man, root, p, report, override, transport, cache, emit=emit,
                                 keep_archives=keep_archives, max_attempts=max_attempts,
                                 backoff=backoff, sleep=sleep, headroom_bytes=headroom_bytes,
                                 disk_usage=disk_usage)
                if p.providers:
                    _fetch_providers(man, root, p, report, transport, emit=emit,
                                     keep_archives=keep_archives, max_attempts=max_attempts,
                                     backoff=backoff, sleep=sleep)
        except DataError:
            raise
        except OSError as e:  # the data directory itself is not writable, etc.
            err = UsageError(f"cannot write to the data directory {root}: {e}",
                             hint="choose a data directory you can write to",
                             details={"data_dir": str(root)})
            err.report = report  # type: ignore[attr-defined]
            raise err from e
    report.status = status(variables, datasets=datasets, data_dir=root, manifest=man)
    return report


def _check_plan_space(root: Path, p: FetchPlan, headroom_bytes: int,
                      disk_usage: Callable[[Path], Any]) -> None:
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


def _fetch_items(man: Manifest, root: Path, p: FetchPlan, report: FetchReport,
                 override: Source | None, transport: Transport, cache: dict[str, Source], *,
                 emit: EventFn, keep_archives: bool, max_attempts: int,
                 backoff: Callable[[int], float], sleep: Callable[[float], None],
                 headroom_bytes: int, disk_usage: Callable[[Path], Any]) -> None:
    # The caller holds the data directory's lock.
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

        try:
            res = extract_archive(done, root, allowed_roots=art.roots, on_file=_on_file)
        except OSError as ex:
            if ex.errno in (errno.ENOSPC, errno.EDQUOT):
                raise DiskSpaceError(f"disk full while extracting {art.name}",
                                     hint="free space and re-run; the archive is kept and re-extracted",
                                     details={"artifact": art.name}) from ex
            raise DataError(f"could not write while extracting {art.name}: {ex}",
                            hint="check that the data directory is writable",
                            details={"artifact": art.name}) from ex
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


@dataclass
class _Job:
    """One file for the download engine: a mirror artifact or a provider file."""

    name: str
    url: Callable[[], str]
    refresh: Callable[[], bool]       # forget credentials; True if a retry could now succeed
    source: str
    bytes: int | None = None          # expected size (None: learn it from the response)
    sha256: str | None = None         # expected sha256 (None: computed and returned)
    resume: bool = True               # the server honours Range: retries continue
    magic: bytes = b""                # the file must start with these bytes
    missing: Callable[[int], DataError] | None = None   # 404/410 (and 401/403 if forbidden_is_missing)
    forbidden_is_missing: bool = False


def _download(transport: Transport, src: Source, art: Artifact, part: Path, meta: Path, *,
              emit: EventFn, max_attempts: int, backoff: Callable[[int], float],
              sleep: Callable[[float], None]) -> tuple[int, int]:
    """Download mirror artifact ``art`` into ``part``, resuming; return (bytes sent this call, attempts)."""
    state = _read_meta(meta, art)
    if state is None and part.exists():
        part.unlink()  # a .part from another version, or without bookkeeping

    def missing(code: int) -> DataError:
        return NotOnMirrorError(
            f"{art.name} is not on the mirror (HTTP {code})",
            hint="the mirror may have moved; set SPACESCANS_DATA_MIRROR or pass --mirror",
            details={"artifact": art.name, "status": code})

    job = _Job(name=art.name, url=lambda: src.url(art), refresh=src.refresh,
               source=src.describe(), bytes=art.bytes, sha256=art.sha256, missing=missing)
    transferred, attempts, _sha, _size = _stream(
        transport, job, part, meta, emit=emit, max_attempts=max_attempts, backoff=backoff,
        sleep=sleep, attempts=int((state or {}).get("attempts", 0)))
    return transferred, attempts


def _stream(transport: Transport, job: _Job, part: Path, meta: Path | None, *,
            emit: EventFn, max_attempts: int, backoff: Callable[[int], float],
            sleep: Callable[[float], None], attempts: int = 0) -> tuple[int, int, str, int]:
    """Download ``job`` into ``part``; return (bytes sent this call, attempts, sha256, size).

    With ``meta`` (mirror artifacts) a ``.part`` left by an earlier call is
    resumed and the bookkeeping file is kept up to date. Without it
    (provider files) every call starts afresh. ``job.resume=False`` (a server
    that ignores Range) restarts each attempt at byte 0.
    """
    digest = hashlib.sha256()
    offset = 0
    expected = job.bytes
    if part.exists():
        offset = part.stat().st_size
        if meta is None or expected is None or offset > expected or not job.resume:
            part.unlink()
            offset = 0
        elif offset:
            emit({"ev": "rehash", "artifact": job.name, "bytes": offset})
            with open(part, "rb") as f:
                while True:
                    buf = f.read(8 * CHUNK)
                    if not buf:
                        break
                    digest.update(buf)
    tries = 0
    transferred = 0
    last_emit = 0.0
    t_start = time.monotonic()
    complete = expected is not None and offset >= expected

    def _restart() -> None:
        nonlocal offset, digest
        part.unlink(missing_ok=True)
        offset = 0
        digest = hashlib.sha256()

    while not complete:
        attempts += 1
        tries += 1
        try:
            if meta is not None:
                write_json_atomic(meta, {"artifact": job.name, "sha256": job.sha256, "bytes": job.bytes,
                                         "source": job.source, "attempts": attempts, "offset": offset,
                                         "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            if offset and not job.resume:
                _restart()  # the server ignores Range: start again from byte 0
            url = job.url()
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            resp = transport.open(url, headers=headers)
            try:
                code = resp.status
                if code in (401, 403):
                    if job.refresh():
                        raise _Retry(f"HTTP {code} (access cookie expired?)")
                    if job.forbidden_is_missing and job.missing is not None:
                        raise job.missing(code)
                    raise NetworkError(f"HTTP {code} downloading {job.name}",
                                       details={"artifact": job.name, "status": code})
                if code in (404, 410):
                    if job.missing is not None:
                        raise job.missing(code)
                    raise DataMissingError(f"{job.name} was not found (HTTP {code})",
                                           details={"artifact": job.name, "status": code})
                if code == 416:
                    _restart()
                    raise _Retry("HTTP 416 (range not satisfiable); restarting from zero")
                if code == 429 or code >= 500:
                    ra = resp.header("Retry-After")
                    raise _Retry(f"HTTP {code}", retry_after=float(ra) if ra and ra.isdigit() else None)
                ctype = (resp.header("Content-Type") or "").lower()
                if code in (200, 206) and ctype.startswith("text/html"):
                    # SharePoint answers an expired cookie with a sign-in page.
                    job.refresh()
                    raise _Retry("the server sent an HTML page instead of the file")
                if code == 206:
                    cr = parse_content_range(resp.header("Content-Range"))
                    if cr is None or cr[0] != offset or (
                            cr[2] is not None and expected is not None and cr[2] != expected):
                        _restart()
                        raise _Retry(f"unexpected Content-Range {resp.header('Content-Range')!r}; "
                                     "restarting from zero")
                    if expected is None and cr[2] is not None:
                        expected = cr[2]
                elif code == 200:
                    length = resp.header("Content-Length")
                    n = int(length) if length is not None and length.isdigit() else None
                    if n is not None and expected is not None and n != expected:
                        job.refresh()
                        raise _Retry(f"server sent {n:,} bytes for {job.name}, expected {expected:,}")
                    if offset:
                        emit({"ev": "range_ignored", "artifact": job.name, "offset": offset})
                        _restart()
                    if expected is None:
                        expected = n
                else:
                    raise NetworkError(f"HTTP {code} downloading {job.name}",
                                       details={"artifact": job.name, "status": code})
                with open(part, "ab" if offset else "wb", buffering=0) as f:
                    while True:
                        buf = resp.read(CHUNK)
                        if not buf:
                            break
                        if expected is not None and offset + len(buf) > expected:
                            _restart()
                            raise DataCorruptError(
                                f"the server sent more than the {expected:,} bytes of {job.name}",
                                details={"artifact": job.name})
                        view = memoryview(buf)
                        while view:
                            n_written = f.write(view)
                            digest.update(view[:n_written])
                            offset += n_written
                            transferred += n_written
                            view = view[n_written:]
                        now = time.monotonic()
                        if now - last_emit >= 0.25:
                            last_emit = now
                            rate = transferred / max(now - t_start, 1e-6)
                            emit({"ev": "download", "artifact": job.name, "bytes": offset,
                                  "total": expected, "rate": round(rate)})
                if expected is not None and offset < expected:
                    raise _Retry(f"connection closed after {offset:,} of {expected:,} bytes")
            finally:
                resp.close()
            complete = True  # this attempt completed the download
            continue
        except TransientNetworkError as e:
            failure: BaseException = e
        except DataError:
            raise
        except ssl.SSLCertVerificationError as e:
            raise NetworkError(
                f"the server's TLS certificate could not be verified ({job.source}): {e}",
                hint="behind an institutional TLS proxy? `pip install truststore` (uses the "
                     "system keychain) or set SSL_CERT_FILE to the proxy's CA bundle",
                details={"artifact": job.name}) from e
        except (_Retry, *_RETRYABLE) as e:
            failure = e
        if isinstance(failure, OSError) and _local_file_error(failure):
            if failure.errno in (errno.ENOSPC, errno.EDQUOT):
                raise DiskSpaceError(f"disk full while downloading {job.name}",
                                     hint="free space and re-run; the download resumes",
                                     details={"artifact": job.name}) from failure
            raise DataError(f"cannot write the download of {job.name}: {failure}",
                            hint="check that the data directory is writable",
                            details={"artifact": job.name, "path": str(part)}) from failure
        if tries >= max_attempts:
            if meta is not None:
                raise NetworkError(
                    f"downloading {job.name} failed after {tries} attempts: {failure}. "
                    f"{offset:,} of {expected or 0:,} bytes are kept; re-run to resume",
                    hint="re-run the same fetch command; it resumes",
                    details={"artifact": job.name, "offset": offset, "bytes": expected,
                             "attempts": attempts}) from failure
            part.unlink(missing_ok=True)
            raise NetworkError(
                f"downloading {job.name} failed after {tries} attempts: {failure}; "
                "re-run to try again (files already in place are kept)",
                hint="re-run the same fetch command; finished files are skipped",
                details={"artifact": job.name, "url": job.source, "attempts": attempts}) from failure
        delay = backoff(tries)
        if isinstance(failure, _Retry) and failure.retry_after is not None:
            delay = min(max(delay, failure.retry_after), 300.0)
        emit({"ev": "retry", "artifact": job.name, "attempt": tries, "delay": delay,
              "reason": str(failure), "offset": offset})
        sleep(delay)
    emit({"ev": "download", "artifact": job.name, "bytes": offset, "total": expected,
          "rate": round(transferred / max(time.monotonic() - t_start, 1e-6))})
    sha = digest.hexdigest()
    if job.sha256 is not None and sha != job.sha256:
        part.unlink(missing_ok=True)
        if meta is not None:
            meta.unlink(missing_ok=True)
        raise DataCorruptError(
            f"checksum mismatch for {job.name}: the download is corrupt or not the published "
            f"file (got {sha[:16]}..., expected {job.sha256[:16]}...); it was deleted",
            hint="re-run the fetch to download it again",
            details={"artifact": job.name, "sha256": sha, "expected": job.sha256})
    if job.magic:
        with open(part, "rb") as f:
            head = f.read(len(job.magic))
        if head != job.magic:
            part.unlink(missing_ok=True)
            raise DataCorruptError(
                f"{job.name} is not the expected file type (it starts with {head[:8].hex() or 'nothing'}, "
                f"expected {job.magic.hex()}); it was deleted",
                hint="the provider may have sent an error page; re-run the fetch to try again",
                details={"artifact": job.name, "url": job.source})
    return transferred, attempts, sha, offset


# ---------------------------------------------------------------------------
# provider downloads

def _provider_present(root: Path, rf: RemoteFile, ds: Dataset) -> bool:
    """A provider file already in place (re-runs skip it)."""
    dl = ds.download
    p = root / rf.target
    try:
        st = p.stat()
    except OSError:
        return False
    if not p.is_file() or st.st_size == 0:
        return False
    if dl.gunzip:
        return True
    if rf.bytes is not None and st.st_size != rf.bytes:
        return False
    if dl.magic:
        magic = bytes.fromhex(dl.magic)
        try:
            with open(p, "rb") as f:
                return f.read(len(magic)) == magic
        except OSError:
            return False
    return True


def _gunzip_into(src: Path, dest: Path) -> tuple[int, str]:
    """Unpack ``src`` (.gz) to ``dest`` atomically; return (size, sha256) of the result."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    size = 0
    try:
        try:
            with gzip.open(src, "rb") as fi, open(part, "wb") as fo:
                while True:
                    buf = fi.read(CHUNK)
                    if not buf:
                        break
                    fo.write(buf)
                    h.update(buf)
                    size += len(buf)
        except (OSError, EOFError, zlib.error) as e:
            if isinstance(e, OSError) and _local_file_error(e) and not isinstance(e, gzip.BadGzipFile):
                raise
            raise DataCorruptError(f"could not unpack {src.name}: {e}",
                                   hint="re-run the fetch to download it again",
                                   details={"file": src.name}) from e
        os.chmod(part, FILE_MODE)
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)
    return size, h.hexdigest()


def _fetch_remote(root: Path, ds: Dataset, rf: RemoteFile, transport: Transport, *, emit: EventFn,
                  keep_archives: bool, max_attempts: int, backoff: Callable[[int], float],
                  sleep: Callable[[float], None]) -> tuple[dict[str, Any], list[str]]:
    dl = ds.download
    tmp = downloads_dir(root) / "providers" / ds.key
    tmp.mkdir(parents=True, exist_ok=True)
    part = tmp / f"{rf.name}.part"

    def missing(code: int) -> DataError:
        return DataMissingError(
            f"{rf.name} is not on the provider's server (HTTP {code})",
            hint="the provider may not publish this file (yet); see the dataset's instructions",
            details={"file": rf.name, "url": rf.url, "status": code})

    job = _Job(name=rf.name, url=lambda: rf.url, refresh=lambda: False, source=rf.url,
               bytes=rf.bytes, resume=dl.range, magic=bytes.fromhex(dl.magic) if dl.magic else b"",
               missing=missing, forbidden_is_missing=True)
    transferred, attempts, sha, size = _stream(transport, job, part, None, emit=emit,
                                               max_attempts=max_attempts, backoff=backoff, sleep=sleep)
    warnings = []
    if rf.reference_bytes is not None and size != rf.reference_bytes:
        warnings.append(f"{rf.name}: {size:,} bytes; the project's download was "
                        f"{rf.reference_bytes:,} (the provider may have republished it)")
    dest = root / rf.target
    if dl.gunzip:
        emit({"ev": "extract", "artifact": rf.name, "bytes": size})
        try:
            placed_size, placed_sha = _gunzip_into(part, dest)
        except DataCorruptError:
            part.unlink(missing_ok=True)   # a broken download is not worth keeping
            raise
        if keep_archives:
            keep = archives_dir(root) / "providers" / ds.key / rf.name
            keep.parent.mkdir(parents=True, exist_ok=True)
            os.replace(part, keep)
        else:
            part.unlink(missing_ok=True)
    else:
        placed_size, placed_sha = size, sha
        place_file(part, dest)
    return {"bytes": placed_size, "sha256": placed_sha, "url": rf.url, "download_bytes": size,
            "download_sha256": sha, "attempts": attempts, "transferred": transferred}, warnings


def provider_receipt_path(root: Path, dataset: str) -> Path:
    return receipts_dir(root) / "providers" / f"{dataset}.json"


def _write_provider_receipt(root: Path, ds: Dataset, records: dict[str, dict[str, Any]]) -> None:
    """Merge the files placed by this call into the dataset's provider receipt."""
    from spacescans import __version__

    path = provider_receipt_path(root, ds.key)
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(old, dict) or old.get("receipt_version") != RECEIPT_VERSION:
            old = {}
    except (OSError, ValueError):
        old = {}
    files = {f[0]: f for f in old.get("files") or () if isinstance(f, list) and len(f) == 3}
    downloads = dict(old.get("downloads") or {})
    for rel, rec in records.items():
        files[rel] = [rel, rec["bytes"], rec["sha256"]]
        downloads[rel] = {"url": rec["url"], "bytes": rec["download_bytes"],
                          "sha256": rec["download_sha256"]}
    write_json_atomic(path, {
        "receipt_version": RECEIPT_VERSION, "dataset": ds.key, "origin": "provider-fetch",
        "provider": ds.provider, "terms": ds.terms.id if ds.terms else None,
        "method": ds.download.method, "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "spacescans_version": __version__,
        "files": [files[k] for k in sorted(files)], "downloads": {k: downloads[k] for k in sorted(downloads)},
    })


def _fetch_providers(man: Manifest, root: Path, p: FetchPlan, report: FetchReport,
                     transport: Transport, *, emit: EventFn, keep_archives: bool, max_attempts: int,
                     backoff: Callable[[int], float], sleep: Callable[[float], None]) -> None:
    """Download each accepted provider dataset, file by file (the caller holds the lock)."""
    for entry in p.providers:
        ds = man.dataset(entry["dataset"])
        result = ItemResult(artifact=ds.key, outcome="failed", provider=True)
        report.items.append(result)
        t0 = time.monotonic()
        emit({"ev": "provider_start", "dataset": ds.key, "method": ds.download.method,
              "source": entry["source"], "terms": entry["terms"]})
        records: dict[str, dict[str, Any]] = {}
        failures: list[dict[str, Any]] = []
        try:
            files = list(remote_files(man, ds, transport=transport, max_attempts=max_attempts,
                                      backoff=backoff, sleep=sleep))
            todo = [rf for rf in files if not _provider_present(root, rf, ds)]
            result.skipped = len(files) - len(todo)
            emit({"ev": "provider_files", "dataset": ds.key, "files": len(files), "todo": len(todo)})
            for i, rf in enumerate(todo):
                emit({"ev": "provider_file", "dataset": ds.key, "file": rf.name, "i": i,
                      "n": len(todo), "bytes": rf.bytes})
                try:
                    rec, warns = _fetch_remote(root, ds, rf, transport, emit=emit,
                                               keep_archives=keep_archives, max_attempts=max_attempts,
                                               backoff=backoff, sleep=sleep)
                except (DataMissingError, DataCorruptError) as e:
                    if isinstance(e, DiskSpaceError):  # pragma: no cover - defensive
                        raise
                    failures.append({"file": rf.name, "target": rf.target, "kind": e.kind,
                                     "message": e.message})
                    emit({"ev": "provider_file_failed", "dataset": ds.key, "file": rf.name,
                          "error": e.kind, "message": e.message})
                    continue
                records[rf.target] = rec
                result.files += 1
                result.attempts += rec["attempts"]
                result.bytes_transferred += rec["transferred"]
                for w in warns:
                    result.warnings.append(w)
                    emit({"ev": "warning", "dataset": ds.key, "message": w})
        except DataError as e:
            result.error = e.to_dict()
            result.seconds = time.monotonic() - t0
            emit({"ev": "provider_end", "dataset": ds.key, "ok": False, "error": e.kind,
                  "message": e.message})
            e.report = report  # type: ignore[attr-defined]
            raise
        finally:
            if records:
                _write_provider_receipt(root, ds, records)
        result.seconds = time.monotonic() - t0
        if failures:
            corrupt = any(f["kind"] == "data_corrupt" for f in failures)
            cls = DataCorruptError if corrupt else DataMissingError
            err = cls(f"{len(failures)} of {len(todo)} {ds.key} files could not be downloaded, e.g. "
                      f"{failures[0]['file']}: {failures[0]['message']}",
                      hint="re-run the same fetch to retry them; files already in place are kept",
                      details={"dataset": ds.key, "failed": failures[:50], "count": len(failures)})
            result.error = err.to_dict()
            emit({"ev": "provider_end", "dataset": ds.key, "ok": False, "error": err.kind,
                  "downloaded": result.files, "skipped": result.skipped, "failed": len(failures)})
            err.report = report  # type: ignore[attr-defined]
            raise err
        result.outcome = "downloaded" if result.files else "present"
        emit({"ev": "provider_end", "dataset": ds.key, "ok": True, "downloaded": result.files,
              "skipped": result.skipped, "seconds": round(result.seconds, 3)})
