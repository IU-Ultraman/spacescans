"""The C3 weight cache: the web's ``legacy-v1`` entries and the ``v2`` entries R uses.

C3 output (buffer weights or distances per episode) depends only on the
cohort's coordinates, the buffer, the boundary or grid data and the code, so
one run's C3 table can serve another. C4 is never cached.

``legacy-v1`` (the web, ``settings.C3_CACHE_DIR``)
    ``<root>/<key>.parquet`` plus ``<root>/<key>.meta.json``, byte for byte
    what the 0.2.7 runners write. The key is
    ``f"{sha256(input.parquet)[:8]}__{TAG}__b{buffer_m}m"`` plus
    ``f"__r{raster_res_m}m"`` for areal tags (:func:`legacy_key`). A hit needs
    only a readable parquet of at least 100 bytes; nothing else is checked,
    as in the web. Entries are copied plainly, as the web does.

``v2`` (R and new callers; never shares a directory with the web)
    ``<root>/c3/v2/<cohort8>__<TAG>__b<buffer>m[__r<res>m]__<sha12>.parquet``
    plus ``.meta.json``. ``sha12`` hashes every key component
    (:meth:`WeightCache.components`): schema 2, the spacescans version and the
    fingerprint of the code a ``spacescans run`` step executes, the linkage
    pattern and tag, the rendered C3 config without its run-specific fields,
    ``cohort_fp`` (sha256 of episode_id:int64, longitude:float64,
    latitude:float64 in row order), ``source_fp`` (data receipts' sha256, else
    ``(path, size, mtime_ns)`` of the files the config reads) and the
    exactextract / GDAL / GEOS / PROJ versions. The meta file records them all
    and a read requires every one to match, plus the parquet's size and
    sha256. Writes go to a temp file and ``os.replace``; each key has its own
    file lock; directories are created 0700 and files 0600.

The cohort fingerprint is a hash: no cohort value is written to a meta file,
an error message or a log.
"""
from __future__ import annotations

import contextlib
import functools
import glob
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

try:  # POSIX; without it (Windows) v2 entries are written unlocked
    import fcntl
except ImportError:  # pragma: no cover - not a supported platform
    fcntl = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from spacescans.catalog.recipes import Step

__all__ = [
    "KEY_FORMATS", "LEGACY", "V2", "V2_SCHEMA", "WeightCache", "cohort_fingerprint",
    "legacy_key", "native_versions", "normalised_config", "sha256_file",
    "source_fingerprint", "step_code_fingerprint",
]

LEGACY = "legacy-v1"
V2 = "v2"
KEY_FORMATS: tuple[str, ...] = (LEGACY, V2)
V2_SCHEMA = 2
#: The web's floor for a usable cached parquet (bg_ndi_wi._is_valid_cached_parquet).
MIN_PARQUET_BYTES = 100
#: Temp files of an interrupted v2 store older than this are removed by ``clear``.
STALE_TEMP_S = 3600.0

# What a `spacescans run` subprocess executes: the computation packages, the CLI
# and the top-level modules they import (not catalog, cohort, orchestrate,
# custom, data or bridge, which never run inside a step).
_STEP_CODE_PACKAGES = ("cli", "engine", "geometry", "io", "linkage", "models", "pipeline",
                       "plugins", "tools", "transforms")
_STEP_CODE_MODULES = ("__main__.py", "_extras.py", "config_resolution.py")
# Rendered-config fields that differ between runs of the same computation.
_RUN_FIELDS = (("name",), ("buffer", "patient_file"), ("output", "path"),
               ("engine", "threads"), ("engine", "memory_limit"), ("engine", "temp_directory"))


# ---------------------------------------------------------------------------
# helpers shared by both formats
# ---------------------------------------------------------------------------

def sha256_file(path: str | os.PathLike) -> str:
    """Hex sha256 of a file's bytes, read in 1 MB chunks (as the web does)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def legacy_key(cache_tag: str, input_sha256: str, *, buffer_m: Any, raster_res_m: Any,
               uses_raster_res: bool) -> str:
    """The web's C3 cache key: ``<sha8>__<TAG>__b<buffer>m[__r<res>m]``.

    ``buffer_m``/``raster_res_m`` are formatted as given (the web formats its
    config's JSON values, so ``270`` and ``270.0`` give different keys).
    """
    if not cache_tag:
        raise ValueError("a C3 cache key needs a cache tag")
    key = f"{input_sha256[:8]}__{cache_tag}__b{buffer_m}m"
    if uses_raster_res:
        if raster_res_m is None:
            raise ValueError(f"cache tag {cache_tag!r} needs raster_res_m")
        key += f"__r{raster_res_m}m"
    return key


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _require_c3(step: Step) -> None:
    if not getattr(step, "is_c3", False) or not getattr(step, "cache_tag", None):
        raise ValueError(f"step {step.name!r} is not a cached C3 step")


def _readable_parquet(path: Path) -> bool:
    """The web's check: present, at least 100 bytes, and a readable parquet footer."""
    try:
        if path.stat().st_size < MIN_PARQUET_BYTES:
            return False
        import pyarrow.parquet as pq

        pq.read_metadata(path)
        return True
    except Exception:  # noqa: BLE001 - any unreadable file is a miss
        return False


def _parquet_rows(path: Path) -> int:
    import pyarrow.parquet as pq

    return int(pq.read_metadata(path).num_rows)


def _input_row_count(ctx: Any) -> int:
    """The web's ``input_row_count``: data lines of ``run_dir/input.csv``.

    Without an ``input.csv`` (a non-web run) the cohort parquet's row count.
    """
    csv = Path(ctx.run_dir) / "input.csv"
    if csv.is_file():
        with open(csv, encoding="utf-8", errors="replace") as f:
            next(f, None)
            return sum(1 for _ in f)
    return _parquet_rows(Path(ctx.cohort_parquet))


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ---------------------------------------------------------------------------
# v2 key components
# ---------------------------------------------------------------------------

def cohort_fingerprint(cohort_parquet: str | os.PathLike) -> str:
    """sha256 of ``episode_id`` (int64), ``longitude`` and ``latitude`` (float64), in row order.

    C3 output carries no PATID or dates (they join at C4), so the coordinates
    and the episode ids are all of the cohort that C3 sees.
    """
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    cols = ("episode_id", "longitude", "latitude")
    names = set(pq.read_schema(cohort_parquet).names)
    missing = [c for c in cols if c not in names]
    if missing:
        raise ValueError(f"the cohort parquet has no {', '.join(missing)} column(s)")
    table = pq.read_table(cohort_parquet, columns=list(cols))
    h = hashlib.sha256(b"spacescans cohort_fp v1\0")
    h.update(int(table.num_rows).to_bytes(8, "little"))
    for name, typ, dtype in (("episode_id", pa.int64(), "<i8"),
                             ("longitude", pa.float64(), "<f8"),
                             ("latitude", pa.float64(), "<f8")):
        arr = table.column(name).cast(typ).to_numpy(zero_copy_only=False)
        h.update(np.ascontiguousarray(arr, dtype=dtype).tobytes())
    return h.hexdigest()


@functools.cache
def step_code_fingerprint() -> str:
    """Fingerprint of the code a ``spacescans run`` step executes.

    The computation packages, the CLI and the top-level modules they import,
    hashed like ``spacescans.capabilities()["spacescans"]["code_fp"]``. Code that
    never runs inside a step (catalog, orchestrate, bridge, ...) is left out,
    so changing it keeps the cache.
    """
    import spacescans
    from spacescans._capabilities import fingerprint_entries

    base = Path(spacescans.__file__).resolve().parent
    entries: list[tuple[str, bytes]] = []
    for package in _STEP_CODE_PACKAGES:
        for path in (base / package).rglob("*.py"):
            if "__pycache__" not in path.parts:
                entries.append((path.relative_to(base).as_posix(), path.read_bytes()))
    for name in _STEP_CODE_MODULES:
        if (base / name).is_file():
            entries.append((name, (base / name).read_bytes()))
    return fingerprint_entries(entries)


@functools.cache
def native_versions() -> dict[str, str | None]:
    """exactextract, GDAL, GEOS and PROJ versions (None when not installed)."""
    import importlib.metadata

    from spacescans._capabilities import native_versions as _native

    try:
        exactextract = importlib.metadata.version("exactextract")
    except importlib.metadata.PackageNotFoundError:
        exactextract = None
    native = _native()
    return {"exactextract": exactextract, "gdal": native.get("gdal"),
            "geos": native.get("geos"), "proj": native.get("proj")}


def normalised_config(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """The rendered config without its run-specific fields.

    Drops ``name`` (task suffix), ``buffer.patient_file``, ``output.path`` and
    the engine resource caps (``threads``, ``memory_limit``,
    ``temp_directory``), which do not change what a step computes.
    """
    out = json.loads(json.dumps(cfg))
    for path in _RUN_FIELDS:
        node = out
        for part in path[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return out


def _data_refs(cfg: Mapping[str, Any]) -> list[str]:
    """The data files a C3 config reads: ``source.file`` and every ``source.*_file``.

    ``*_cache_dir`` entries are cohort-independent caches the pipeline writes
    itself; they are not inputs.
    """
    source = cfg.get("source")
    refs: list[str] = []
    if isinstance(source, Mapping):
        for key, value in source.items():
            if key != "file" and not key.endswith("_file"):
                continue
            values = value if isinstance(value, list) else [value]
            refs.extend(v for v in values if isinstance(v, str))
    return refs


def _static_prefix(pattern: str) -> str:
    return pattern.split("{", 1)[0]


def _receipt_entries(refs: list[str], data_dir: Path) -> tuple[list[list[str]], list[str]]:
    """Receipts covering ``refs``: ``([[artifact, sha256], ...], refs not covered)``."""
    try:
        from spacescans.data import load_manifest
        from spacescans.data.receipts import read_receipt

        manifest = load_manifest()
    except Exception:  # noqa: BLE001 - no manifest: every ref falls back to stat
        return [], list(refs)
    prefixes: dict[str, list[str]] = {}
    for key, ds in manifest.datasets.items():
        found = []
        for rule in ds.layout:
            for field in ("path", "dir"):
                if isinstance(rule.get(field), str) and _static_prefix(rule[field]):
                    found.append(_static_prefix(rule[field]))
        for name in ds.artifacts:
            art = manifest.artifacts.get(name)
            if art is not None and art.relpath:
                found.append(art.relpath)
        prefixes[key] = found
    covered: dict[str, str] = {}
    rest: list[str] = []
    for ref in refs:
        if os.path.isabs(ref):
            rest.append(ref)
            continue
        datasets = [k for k, ps in prefixes.items()
                    if any(ref.startswith(p) or p.startswith(ref.rstrip("/") + "/") for p in ps)]
        receipts = {}
        for ds_key in datasets:
            for name in manifest.datasets[ds_key].artifacts:
                rec = read_receipt(data_dir, name)
                receipts[name] = rec.get("sha256") if rec else None
        if datasets and receipts and all(isinstance(v, str) for v in receipts.values()):
            covered.update(receipts)
        else:
            rest.append(ref)
    return [[name, sha] for name, sha in sorted(covered.items())], rest


def _stat_entries(ref: str, data_dir: Path) -> list[list[Any]]:
    """``[path, size, mtime_ns]`` of a referenced file (with its same-stem siblings,
    e.g. a shapefile's parts) or of every file under a referenced directory."""
    path = Path(ref) if os.path.isabs(ref) else data_dir / ref

    def rel(p: Path) -> str:
        if os.path.isabs(ref):
            return str(p)
        try:
            return p.relative_to(data_dir).as_posix()
        except ValueError:
            return str(p)

    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file())
    elif path.is_file():
        files = sorted(Path(p) for p in glob.glob(glob.escape(str(path.parent / path.stem)) + ".*")
                       if Path(p).is_file())
    else:
        return [[rel(path), None, None]]
    out = []
    for p in files:
        st = p.stat()
        out.append([rel(p), st.st_size, st.st_mtime_ns])
    return out


def source_fingerprint(cfg: Mapping[str, Any], data_dir: str | os.PathLike) -> dict[str, Any]:
    """What identifies the data a C3 config reads.

    ``receipts``: ``[artifact, sha256]`` for every manifest artifact whose
    receipt covers a referenced file. ``files``: ``[path, size, mtime_ns]``
    for referenced files no receipt covers (paths relative to ``data_dir``
    unless the config gave an absolute one; size and mtime are None for a
    missing file).
    """
    data_dir = Path(data_dir)
    receipts, rest = _receipt_entries(_data_refs(cfg), data_dir)
    files: list[list[Any]] = []
    seen: set[str] = set()
    for ref in rest:
        for entry in _stat_entries(ref, data_dir):
            if entry[0] not in seen:
                seen.add(entry[0])
                files.append(entry)
    return {"receipts": receipts, "files": sorted(files, key=lambda e: e[0])}


# ---------------------------------------------------------------------------
# the cache
# ---------------------------------------------------------------------------

def _mkdir_private(path: Path) -> None:
    """Create ``path`` and missing parents with mode 0700 (umask cannot widen it)."""
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        if p.parent == p:
            break
        p = p.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(d, 0o700)


class WeightCache:
    """A C3 weight cache in the web's format (``legacy-v1``) or the v2 format.

    ``key(step, ctx)`` names the entry of a C3 step; ``fetch`` copies a hit to
    ``dst`` and returns its key (None on a miss); ``store`` adds ``src`` (the
    step's fresh output) and returns the key; ``info`` and ``clear`` manage
    the directory.
    """

    def __init__(self, root: str | os.PathLike, *, key_format: Literal["legacy-v1", "v2"]) -> None:
        if key_format not in KEY_FORMATS:
            raise ValueError(f"key_format must be one of {KEY_FORMATS}, not {key_format!r}")
        self.root = Path(os.fspath(root))
        self.key_format = key_format

    def __repr__(self) -> str:
        return f"WeightCache({str(self.root)!r}, key_format={self.key_format!r})"

    @property
    def directory(self) -> Path:
        """Where the entries live: ``root`` (legacy-v1) or ``root/c3/v2``."""
        return self.root if self.key_format == LEGACY else self.root / "c3" / "v2"

    def path(self, key: str) -> Path:
        """The parquet file of the entry ``key``."""
        return self.directory / f"{key}.parquet"

    def meta_path(self, key: str) -> Path:
        """The ``.meta.json`` sidecar of the entry ``key``."""
        return self.directory / f"{key}.meta.json"

    # -- keys ---------------------------------------------------------------

    def components(self, step: Step, ctx: Any) -> dict[str, Any]:
        """Every v2 key component of ``step`` in ``ctx`` (JSON-ready)."""
        from spacescans import __version__
        from spacescans.orchestrate.render import render

        _require_c3(step)
        cfg = render(step, ctx)
        return {
            "schema": V2_SCHEMA,
            "spacescans": {"version": __version__, "code_fp": step_code_fingerprint()},
            "pattern": cfg.get("linkage_pattern"),
            "tag": step.cache_tag,
            "config": normalised_config(cfg),
            "cohort_fp": cohort_fingerprint(ctx.cohort_parquet),
            "source_fp": source_fingerprint(cfg, ctx.data_dir),
            "native": dict(native_versions()),
        }

    def _v2_key(self, step: Step, ctx: Any) -> tuple[str, dict[str, Any]]:
        comps = self.components(step, ctx)
        digest = hashlib.sha256(_canonical(comps).encode("ascii")).hexdigest()
        key = f"{comps['cohort_fp'][:8]}__{step.cache_tag}__b{ctx.buffer_m}m"
        if step.uses_raster_res:
            key += f"__r{ctx.raster_res_m}m"
        return f"{key}__{digest[:12]}", comps

    def key(self, step: Step, ctx: Any) -> str:
        """The entry name of the C3 ``step`` in the run ``ctx``."""
        _require_c3(step)
        if self.key_format == LEGACY:
            return legacy_key(step.cache_tag, sha256_file(ctx.cohort_parquet),
                              buffer_m=ctx.buffer_m, raster_res_m=ctx.raster_res_m,
                              uses_raster_res=step.uses_raster_res)
        return self._v2_key(step, ctx)[0]

    # -- locks --------------------------------------------------------------

    @contextlib.contextmanager
    def _lock(self, key: str, *, shared: bool = False, blocking: bool = True) -> Iterator[bool]:
        """One lock file per v2 key (``c3/v2/.locks/<key>.lock``); yields False if busy."""
        if self.key_format == LEGACY or fcntl is None:
            yield True
            return
        locks = self.directory / ".locks"
        _mkdir_private(locks)
        fd = os.open(locks / f"{key}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(fd, flags)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # -- fetch / store --------------------------------------------------------

    def fetch(self, step: Step, ctx: Any, dst: str | os.PathLike) -> str | None:
        """Copy the cached C3 output of ``step`` to ``dst``; its key on a hit, None on a miss."""
        dst = Path(dst)
        if self.key_format == LEGACY:
            key = self.key(step, ctx)
            path = self.path(key)
            if not _readable_parquet(path):
                return None
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, dst)
            return key
        key, comps = self._v2_key(step, ctx)
        path, meta_path = self.path(key), self.meta_path(key)
        if not meta_path.exists():
            return None
        with self._lock(key, shared=True):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            if (not isinstance(meta, dict) or meta.get("key") != key
                    or _canonical(meta.get("components")) != _canonical(comps)):
                return None
            entry = meta.get("file") or {}
            try:
                if path.stat().st_size != entry.get("bytes") or not _readable_parquet(path):
                    return None
            except OSError:
                return None
            dst.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=f".{dst.name}.", suffix=".tmp")
            try:
                h = hashlib.sha256()
                with open(path, "rb") as fin, os.fdopen(fd, "wb") as fout:
                    for chunk in iter(lambda: fin.read(1 << 20), b""):
                        h.update(chunk)
                        fout.write(chunk)
                if h.hexdigest() != entry.get("sha256"):
                    return None
                os.replace(tmp, dst)
                tmp = None
            finally:
                if tmp is not None:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(tmp)
        return key

    def store(self, step: Step, ctx: Any, src: str | os.PathLike, *, seconds: float) -> str:
        """Add ``src`` (the fresh output of the C3 ``step``) to the cache; return its key."""
        src = Path(src)
        if self.key_format == LEGACY:
            return self._store_legacy(step, ctx, src, seconds=seconds)
        if not _readable_parquet(src):
            raise ValueError(f"step {step.name!r}: its output is not a readable parquet file")
        key, comps = self._v2_key(step, ctx)
        directory = self.directory
        _mkdir_private(directory)
        with self._lock(key):
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{key}.", suffix=".parquet.tmp")
            meta_tmp = None
            try:
                h = hashlib.sha256()
                size = 0
                with open(src, "rb") as fin, os.fdopen(fd, "wb") as fout:
                    for chunk in iter(lambda: fin.read(1 << 20), b""):
                        h.update(chunk)
                        size += len(chunk)
                        fout.write(chunk)
                    fout.flush()
                    os.fsync(fout.fileno())
                meta = {
                    "key": key,
                    "key_format": V2,
                    "step": step.name,
                    "components": comps,
                    "file": {"bytes": size, "sha256": h.hexdigest(), "rows": _parquet_rows(src)},
                    "seconds": round(float(seconds), 3),
                    "created_at": _utc_now_iso(),
                }
                mfd, meta_tmp = tempfile.mkstemp(dir=directory, prefix=f".{key}.",
                                                 suffix=".meta.json.tmp")
                with os.fdopen(mfd, "w", encoding="utf-8") as f:
                    json.dump(meta, f, indent=2, sort_keys=False)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path(key))
                tmp = None
                os.replace(meta_tmp, self.meta_path(key))
                meta_tmp = None
            finally:
                for leftover in (tmp, meta_tmp):
                    if leftover is not None:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(leftover)
        return key

    def legacy_meta(self, step: Step, ctx: Any, src: str | os.PathLike, *,
                    seconds: float) -> dict[str, Any]:
        """The web's ``.meta.json`` fields for a fresh C3 output, in the web's order.

        ``boundary`` is ``step.meta_boundary`` (bg_ndi_wi records BG2020 as
        "BG"); ``raster_res_m`` appears only for ``step.meta_raster_res``
        (bg_ndi_wi and zcta5_cbp); ``wall_clock_seconds`` is truncated to int.
        """
        fields: dict[str, Any] = {
            "sha_full": sha256_file(ctx.cohort_parquet),
            "boundary": step.meta_boundary or step.cache_tag,
            "buffer_m": ctx.buffer_m,
        }
        if step.meta_raster_res:
            fields["raster_res_m"] = ctx.raster_res_m
        fields["input_row_count"] = _input_row_count(ctx)
        fields["wall_clock_seconds"] = int(seconds)
        fields["file_size_bytes"] = Path(src).stat().st_size
        fields["created_at"] = _utc_now_iso()
        return fields

    def _store_legacy(self, step: Step, ctx: Any, src: Path, *, seconds: float) -> str:
        key = self.key(step, ctx)
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, path)
        fields = self.legacy_meta(step, ctx, src, seconds=seconds)
        self.meta_path(key).write_text(json.dumps(fields, indent=2), encoding="utf-8")
        return key

    # -- housekeeping -----------------------------------------------------------

    def _entries(self) -> list[Path]:
        d = self.directory
        if not d.is_dir():
            return []
        return sorted(p for p in d.glob("*.parquet") if p.is_file() and not p.name.startswith("."))

    def _temp_files(self) -> list[Path]:
        """Temp files of v2 stores (``.<key>.<random>.parquet.tmp`` / ``.meta.json.tmp``):
        a store in progress, or one that was killed before it could clean up."""
        d = self.directory
        if self.key_format != V2 or not d.is_dir():
            return []
        return sorted(p for p in d.glob(".*.tmp") if p.is_file())

    def info(self) -> dict[str, Any]:
        """``entries``, ``bytes`` (parquet plus meta), ``oldest`` and ``newest`` (ISO 8601 UTC)."""
        entries = self._entries()
        total = 0
        mtimes = []
        for p in entries:
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            total += st.st_size
            mtimes.append(st.st_mtime)
            meta = p.with_name(p.name[: -len(".parquet")] + ".meta.json")
            with contextlib.suppress(FileNotFoundError):
                total += meta.stat().st_size
        temp_files = temp_bytes = 0
        for t in self._temp_files():     # they hold C3 weights too: never hide them
            with contextlib.suppress(FileNotFoundError):
                temp_bytes += t.stat().st_size
                temp_files += 1
        return {
            "key_format": self.key_format,
            "root": str(self.root),
            "directory": str(self.directory),
            "entries": len(mtimes),
            "bytes": total,
            "oldest": _iso(min(mtimes)) if mtimes else None,
            "newest": _iso(max(mtimes)) if mtimes else None,
            "temp_files": temp_files,
            "temp_bytes": temp_bytes,
        }

    def clear(self, *, older_than_s: float | None = None) -> dict[str, Any]:
        """Remove entries (all, or those last written more than ``older_than_s`` ago).

        A v2 entry that another process is writing is left alone. Temp files
        of interrupted v2 stores (their key's lock is free) are removed too:
        all of them without ``older_than_s``, otherwise those older than
        ``older_than_s`` or an hour; so are meta files without their parquet.
        Returns ``removed``, ``kept`` and ``bytes`` (freed).
        """
        now = time.time()
        removed = kept = freed = 0
        for p in self._entries():
            key = p.name[: -len(".parquet")]
            try:
                age = now - p.stat().st_mtime
            except FileNotFoundError:
                continue
            if older_than_s is not None and age < older_than_s:
                kept += 1
                continue
            with self._lock(key, blocking=False) as got:
                if not got:
                    kept += 1
                    continue
                for f in (p, self.meta_path(key)):
                    with contextlib.suppress(FileNotFoundError):
                        size = f.stat().st_size
                        f.unlink()
                        freed += size
                removed += 1
                if self.key_format == V2:
                    with contextlib.suppress(FileNotFoundError):
                        (self.directory / ".locks" / f"{key}.lock").unlink()
        d = self.directory
        if d.is_dir():
            for meta in d.glob("*.meta.json"):
                if not meta.with_name(meta.name[: -len(".meta.json")] + ".parquet").exists():
                    with contextlib.suppress(FileNotFoundError):
                        size = meta.stat().st_size
                        if older_than_s is None or now - meta.stat().st_mtime >= older_than_s:
                            meta.unlink()
                            freed += size
            for tmp in self._temp_files():
                key = tmp.name[1:].split(".", 1)[0]
                try:
                    st = tmp.stat()
                except FileNotFoundError:
                    continue
                age = now - st.st_mtime
                if older_than_s is not None and age < min(older_than_s, STALE_TEMP_S):
                    continue
                with self._lock(key, blocking=False) as got:
                    if not got:                  # a store is writing it right now
                        continue
                    with contextlib.suppress(FileNotFoundError):
                        tmp.unlink()
                        freed += st.st_size
                    if not self.path(key).exists():
                        with contextlib.suppress(FileNotFoundError):
                            (d / ".locks" / f"{key}.lock").unlink()
        return {"removed": removed, "kept": kept, "bytes": freed}
