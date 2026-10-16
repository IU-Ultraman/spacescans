"""Registered datasets: user data validated and prepared once, then linked by name.

A *registered dataset* is data a user brings to a linkage, stored with its
metadata under a datasets root so that any later link (Python, R through the
bridge, the web app) can use it by name or id::

    from spacescans import custom, orchestrate

    custom.register(["pm25_daily.csv"], name="pm25 daily", kind="polygon_table",
                    temporal="daily", key={"boundary": "tract", "column": "GEOID"})
    orchestrate.link(cohort, ["ndi"], custom=["pm25 daily"])

Three kinds (``kind``) and their time structure (``temporal``):

``"polygon_table"``
    A CSV (or Parquet) table keyed by a 2010 Census geography: tract, block
    group, ZCTA or county. ``static`` (one row per geography), ``yearly`` (a
    year column) or ``daily`` (a date column). Prepared as a normalised
    Parquet table.
``"raster"``
    GeoTIFFs on one common grid: one file (``static``), or one per year
    (``yearly``), per two-week period (``biweekly``) or per day (``daily``).
    The period of each file comes from its name (a year, or a date such as
    ``20160101`` / ``2016-01-01``) or from a small manifest CSV passed with
    the rasters. Prepared as validated copies plus a period index.
``"features"``
    Point, line or polygon geometries (one geometry family per dataset) in a
    GeoPackage, GeoJSON or zipped Shapefile, ``static`` or ``yearly`` (one
    file per year, or a year column). The linked value is the distance in
    meters from each location to the nearest feature. Prepared as GeoParquet
    in EPSG:5070 (one file per year for a yearly dataset).

Registration validates everything it can before anything is stored, and the
error messages name columns, files, counts and expected formats but never a
data value. The prepared files and ``dataset.json`` (the
:class:`DatasetRecord`) go to ``<root>/<id>/``; a dataset becomes visible only
once it is complete (it is prepared in a hidden directory and renamed into
place). Linkage reads the prepared files and nothing else.

Root: the ``root`` argument, else ``$SPACESCANS_DATASETS_DIR``, else
``platformdirs.user_data_dir("spacescans")/datasets`` (the same directory is
computed when platformdirs is not installed).

``visibility`` (``"private"`` or ``"deployment"``) and ``owner`` are recorded
for the web app, which enforces them; the package itself does not.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
import warnings
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from spacescans.custom.errors import CustomExposomeError

__all__ = [
    "DATASETS_DIR_ENV", "KINDS", "TEMPORAL", "VISIBILITY", "RECORD_FILE", "MAX_NAME",
    "DatasetError", "DatasetNotFoundError", "DatasetExistsError", "DatasetRecord",
    "datasets_root", "register", "list_datasets", "get_dataset", "remove_dataset",
    "update_dataset", "dataset_definition", "resolve_custom", "link_overlay",
]

#: Environment variable naming the datasets root.
DATASETS_DIR_ENV = "SPACESCANS_DATASETS_DIR"
#: The kinds of dataset, and the time structures each accepts.
KINDS: tuple[str, ...] = ("polygon_table", "raster", "features")
TEMPORAL: Mapping[str, tuple[str, ...]] = {
    "polygon_table": ("static", "yearly", "daily"),
    "raster": ("static", "yearly", "biweekly", "daily"),
    "features": ("static", "yearly"),
}
VISIBILITY: tuple[str, ...] = ("private", "deployment")
#: The metadata a dataset carries besides its data (``register(metadata=...)``).
METADATA_FIELDS: tuple[str, ...] = ("source", "version", "description", "license_note",
                                    "visibility", "owner")
_METADATA_LIMITS = {"source": 400, "version": 100, "description": 2000, "license_note": 2000,
                    "owner": 200}
RECORD_FILE = "dataset.json"
#: Version of the dataset.json layout.
RECORD_FORMAT = 1
MAX_NAME = 80
_LOCK_FILE = ".registry.lock"
_TMP_PREFIX = ".tmp-"
_TRASH_PREFIX = ".trash-"
_STALE_TMP_S = 24 * 3600
_ID = re.compile(r"[0-9a-f]{32}")
_VARIABLE_KEY = re.compile(r"custom_[0-9a-f]{8}")


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

class DatasetError(CustomExposomeError):
    """A dataset could not be registered, found, changed or removed.

    A :class:`~spacescans.custom.CustomExposomeError` (a ``ValueError``), so
    the bridge reports it as a ``config`` error and the web as a 400. The
    message names files, columns and counts, never a data value.
    """


class DatasetNotFoundError(DatasetError, LookupError):
    """No dataset in the root has this name or id."""


class DatasetExistsError(DatasetError):
    """A dataset with this name already exists in the root (``overwrite=False``)."""


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------

@dataclass(frozen=True, eq=False)
class DatasetRecord:
    """One registered dataset, as stored in ``<root>/<id>/dataset.json``.

    ``id``: uuid4 hex (32 characters); ``name``: unique per root (compared
    ignoring case); ``kind`` / ``temporal``: see the module docstring;
    ``key``: how places and times are read from the files (tables: the
    boundary, the key column and the year or date column; rasters and
    features: ``None`` or the options given at registration); ``variables``:
    the linked columns, ``[{name, unit, description}]``; ``source``,
    ``version``, ``description``, ``license_note``: free text;
    ``visibility``: ``"private"`` or ``"deployment"`` (enforced by the web
    app); ``owner``: an optional string; ``created_utc``: ISO 8601;
    ``files``: the prepared files, relative to the dataset directory;
    ``checksum``: sha256 of the original files' bytes, in the order of
    ``details["originals"]`` (for one file, that file's sha256);
    ``spacescans_version``: the package version that prepared it.

    ``details`` holds kind-specific facts the steps and a user interface need
    (grid, bands, periods, geometry family, row and feature counts, the
    prepared files' sha256); never data values. ``root`` is where the record
    was read from (not stored).
    """

    id: str
    name: str
    kind: str
    temporal: str
    key: dict | None
    variables: list[dict]
    source: str | None
    version: str | None
    description: str | None
    license_note: str | None
    visibility: str
    owner: str | None
    created_utc: str
    files: list[str]
    checksum: str
    spacescans_version: str
    details: dict = field(default_factory=dict)
    root: Path | None = field(default=None, repr=False)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DatasetRecord):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __hash__(self) -> int:
        return hash(self.id)

    @property
    def directory(self) -> Path:
        """``<root>/<id>``, where the prepared files are."""
        if self.root is None:
            raise DatasetError(f"dataset {self.name!r} has no root; read it with get_dataset()")
        return Path(self.root) / self.id

    @property
    def dataset_id(self) -> str:
        """The 8-character id the catalog uses (the first 8 hex of ``id``)."""
        return self.id[:8]

    @property
    def variable_key(self) -> str:
        """The dataset's variable key in a link: ``custom_<first 8 hex of id>``."""
        from spacescans.custom.keys import variable_key
        return variable_key(self.dataset_id)

    @property
    def value_cols(self) -> list[str]:
        """The names of the linked columns, in order."""
        return [v["name"] for v in self.variables]

    def path(self, relative: str) -> Path:
        """Absolute path of one prepared file."""
        return self.directory / Path(relative)

    def to_dict(self) -> dict[str, Any]:
        """The JSON-ready record (the contract's fields, then ``details``)."""
        return {
            "id": self.id, "name": self.name, "kind": self.kind, "temporal": self.temporal,
            "key": _jsonable(self.key), "variables": _jsonable(self.variables),
            "source": self.source, "version": self.version, "description": self.description,
            "license_note": self.license_note, "visibility": self.visibility,
            "owner": self.owner, "created_utc": self.created_utc,
            "files": list(self.files), "checksum": self.checksum,
            "spacescans_version": self.spacescans_version,
            "details": _jsonable(self.details),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, root: str | os.PathLike | None = None
                  ) -> DatasetRecord:
        """A record from :meth:`to_dict` output (or a ``dataset.json``)."""
        try:
            return cls(
                id=str(data["id"]), name=str(data["name"]), kind=str(data["kind"]),
                temporal=str(data["temporal"]),
                key=dict(data["key"]) if isinstance(data.get("key"), Mapping) else None,
                variables=[dict(v) for v in data.get("variables") or []],
                source=data.get("source"), version=data.get("version"),
                description=data.get("description"), license_note=data.get("license_note"),
                visibility=str(data.get("visibility") or "private"),
                owner=None if data.get("owner") is None else str(data["owner"]),
                created_utc=str(data.get("created_utc") or ""),
                files=[str(f) for f in data.get("files") or []],
                checksum=str(data.get("checksum") or ""),
                spacescans_version=str(data.get("spacescans_version") or ""),
                details=dict(data.get("details") or {}),
                root=None if root is None else Path(root),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DatasetError(f"not a dataset record ({type(exc).__name__}: {exc})") from None

    def definition(self) -> dict[str, Any]:
        """The linkable definition: what ``link(custom=...)`` and a web task snapshot hold.

        A dict in the shape of :func:`spacescans.custom.define_polygon` /
        :func:`~spacescans.custom.define_raster` output (a superset of a
        catalog entry plus the fields the steps read), with absolute paths to
        the prepared files and their sha256, so ``check_definition(deep=True)``
        catches a prepared file changed since registration. ``registered``
        names the record it came from.
        """
        from spacescans.custom import prepare
        return prepare.definition_of(self)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "item") and callable(value.item):      # numpy scalars
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return value


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _package_version() -> str:
    try:
        from spacescans import __version__
    except Exception:  # noqa: BLE001 - a broken install still registers
        return "unknown"
    return str(__version__)


def _write_json(path: Path, payload: Any) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def check_name(name: Any) -> str:
    """A dataset name: text of 1-80 characters, trimmed, no control characters.

    It may not look like a dataset id (32 hex characters) or a variable key
    (``custom_<8 hex>``), which :func:`get_dataset` also accepts.
    """
    if not isinstance(name, str):
        raise DatasetError("give the dataset a name (a string)")
    name = name.strip()
    if not name:
        raise DatasetError("give the dataset a name")
    if len(name) > MAX_NAME:
        raise DatasetError(f"the dataset name is longer than {MAX_NAME} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise DatasetError("the dataset name contains a control character")
    if _ID.fullmatch(name) or _VARIABLE_KEY.fullmatch(name):
        raise DatasetError("the dataset name looks like a dataset id or variable key; "
                           "choose a descriptive name")
    return name


def _choice(what: str, value: Any, allowed: Sequence[str], *, context: str = "") -> str:
    if isinstance(value, str) and value in allowed:
        return value
    shown = repr(value) if value is None or isinstance(value, str) else type(value).__name__
    raise DatasetError(f"{what} must be one of {', '.join(map(repr, allowed))}{context}, "
                       f"not {shown}")


def _metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Checked metadata: known fields only, strings within limits, visibility checked."""
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise DatasetError("metadata must be a mapping of "
                           + ", ".join(METADATA_FIELDS))
    unknown = sorted(set(metadata) - set(METADATA_FIELDS))
    if unknown:
        raise DatasetError(f"unknown metadata field(s): {', '.join(map(str, unknown))} "
                           f"(known: {', '.join(METADATA_FIELDS)})")
    out: dict[str, Any] = {}
    for field_name in METADATA_FIELDS:
        if field_name not in metadata:
            continue
        value = metadata[field_name]
        if value is None:
            out[field_name] = None
            continue
        if field_name == "owner" and isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            raise DatasetError(f"metadata field {field_name!r} must be a string")
        value = value.strip()
        if field_name == "visibility":
            out[field_name] = _choice("visibility", value, VISIBILITY)
            continue
        limit = _METADATA_LIMITS[field_name]
        if len(value) > limit:
            raise DatasetError(f"metadata field {field_name!r} is longer than {limit} characters")
        out[field_name] = value or None
    return out


# ---------------------------------------------------------------------------
# the root
# ---------------------------------------------------------------------------

def _user_data_dir() -> Path:
    """``platformdirs.user_data_dir("spacescans")``, computed the same way without it."""
    try:
        import platformdirs
    except ImportError:
        pass
    else:
        return Path(platformdirs.user_data_dir("spacescans"))
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "spacescans"
    if sys.platform == "win32":  # pragma: no cover - Windows only
        base = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / "spacescans" / "spacescans"
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    return Path(xdg if xdg else home / ".local" / "share") / "spacescans"


def datasets_root(root: str | os.PathLike | None = None) -> Path:
    """The datasets root: ``root``, else ``$SPACESCANS_DATASETS_DIR``, else the
    per-user data directory's ``datasets`` folder. Absolute; it need not exist."""
    if root is not None:
        path = Path(os.fspath(root)).expanduser()
    elif os.environ.get(DATASETS_DIR_ENV, "").strip():
        path = Path(os.environ[DATASETS_DIR_ENV].strip()).expanduser()
    else:
        path = _user_data_dir() / "datasets"
    return path.absolute()


def _ensure_root(root: Path, *, explicit: bool) -> None:
    if root.exists():
        if not root.is_dir():
            raise DatasetError(f"the datasets root {root} is not a directory")
        return
    # The per-user default root holds the user's own data: private to them.
    root.mkdir(parents=True, exist_ok=True, mode=0o755 if explicit else 0o700)


@contextlib.contextmanager
def _locked(root: Path) -> Iterator[None]:
    """An exclusive lock on the root for the short critical sections (names, renames)."""
    path = root / _LOCK_FILE
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":  # pragma: no cover - Windows only
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if sys.platform == "win32":  # pragma: no cover - Windows only
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _dataset_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith(".") and _ID.fullmatch(p.name))


def _read_record(directory: Path, root: Path) -> DatasetRecord | None:
    path = directory / RECORD_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        record = DatasetRecord.from_dict(data, root=root)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        warnings.warn(f"skipping unreadable dataset record in {directory} "
                      f"({type(exc).__name__})", stacklevel=3)
        return None
    if record.id != directory.name:
        warnings.warn(f"skipping dataset record in {directory}: its id does not match "
                      "the directory", stacklevel=3)
        return None
    return record


def _records(root: Path) -> list[DatasetRecord]:
    out = [r for r in (_read_record(d, root) for d in _dataset_dirs(root)) if r is not None]
    return sorted(out, key=lambda r: (r.created_utc, r.name.casefold(), r.id))


def _same_name(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def _sweep(root: Path) -> None:
    """Remove leftovers: retired datasets, and preparations abandoned a day ago."""
    now = time.time()
    for entry in root.iterdir():
        try:
            if entry.name.startswith(_TRASH_PREFIX) or (
                    entry.name.startswith(_TMP_PREFIX)
                    and now - entry.stat().st_mtime > _STALE_TMP_S):
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def _new_id(root: Path) -> str:
    """A uuid4 hex whose first 8 characters no dataset in the root uses yet."""
    taken = {d.name[:8] for d in _dataset_dirs(root)}
    taken |= {e.name[len(_TMP_PREFIX):][:8] for e in root.iterdir()
              if e.name.startswith(_TMP_PREFIX)}
    while True:
        candidate = uuid.uuid4().hex
        if candidate[:8] not in taken:
            return candidate


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Input:
    """One file given to :func:`register`, with the period given for it (if any)."""

    path: Path
    period: Any = None

    @property
    def name(self) -> str:
        return self.path.name


def _inputs(paths: Any) -> list[Input]:
    """Files from a path, a list of paths, or a ``{path: period}`` mapping."""
    if isinstance(paths, (str, os.PathLike)):
        items: list[tuple[Any, Any]] = [(paths, None)]
    elif isinstance(paths, Mapping):
        items = list(paths.items())
    elif isinstance(paths, Iterable):
        items = [(p, None) for p in paths]
    else:
        raise DatasetError("paths must be a file path, a list of file paths or a "
                           "{path: period} mapping")
    if not items:
        raise DatasetError("give at least one file")
    out: list[Input] = []
    seen: set[Path] = set()
    for raw, period in items:
        if not isinstance(raw, (str, os.PathLike)):
            raise DatasetError("each file must be given as a path")
        path = Path(os.fspath(raw)).expanduser().absolute()
        if not path.is_file():
            raise DatasetError(f"file not found: {path}")
        if path in seen:
            raise DatasetError(f"{path.name} is given twice")
        seen.add(path)
        out.append(Input(path, period))
    return out


def sha256_of(path: Path) -> tuple[str, int]:
    """(sha256 hex, size in bytes) of a file, read in 1 MiB chunks."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def combined_checksum(originals: Sequence[Mapping[str, Any]]) -> str:
    """sha256 of the original files' bytes in order (one file: that file's sha256)."""
    if len(originals) == 1:
        return str(originals[0]["sha256"])
    h = hashlib.sha256()
    for item in originals:
        with open(item["path"], "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def register(paths: Any, *, name: str, kind: str, temporal: str,
             key: Any = None, variables: Any = None,
             metadata: Mapping[str, Any] | None = None,
             root: str | os.PathLike | None = None, overwrite: bool = False) -> DatasetRecord:
    """Validate, prepare and store a dataset; return its record.

    ``paths``: the file(s) (see the module docstring for each kind), or a
    ``{path: period}`` mapping that gives each raster its year or start date
    (or each yearly features file its year) instead of the file name or a
    manifest. ``name``: unique in the root (ignoring case). ``kind`` and
    ``temporal``: see :data:`KINDS` and :data:`TEMPORAL`.

    ``key``: for a ``polygon_table``, the geography and the key column, as
    ``{"boundary": "tract" | "block_group" | "zcta" | "county", "column":
    <key column>}`` plus ``"year_column"`` (yearly) or ``"date_column"``
    (daily), which default to a column called ``year`` / ``date``; a bare
    boundary name also works when the key column has the boundary's usual
    name (GEOID10, GEOID, ZCTA5CE10, ...). For ``features``: optional
    ``{"year_column": ..., "layer": ...}``; for a ``raster``: unused.

    ``variables``: the linked columns, a list of names or of
    ``{"name", "unit", "description"}`` (tables may add ``"column"``, the
    file's column, when the name differs; rasters may add ``"band"``).
    Defaults: every numeric column of a table; band 1 of a raster, named after
    the dataset; for features one distance column ``<name>_dist_m`` (meters).

    ``metadata``: ``source``, ``version``, ``description``, ``license_note``,
    ``visibility`` (``"private"``, the default, or ``"deployment"``) and
    ``owner``. ``overwrite=True`` replaces a dataset of the same name (the
    new one gets a new id).

    Raises :class:`DatasetError` (a ``CustomExposomeError``): nothing is
    stored then. ``MissingExtraError`` when a raster or features dataset is
    registered without the ``[geo]`` extra.
    """
    from spacescans.custom import prepare

    name = check_name(name)
    kind = _choice("kind", kind, KINDS)
    temporal = _choice("temporal", temporal, TEMPORAL[kind], context=f" for a {kind}")
    meta = _metadata(metadata)
    inputs = _inputs(paths)
    root_dir = datasets_root(root)
    _ensure_root(root_dir, explicit=root is not None or bool(os.environ.get(DATASETS_DIR_ENV)))

    with _locked(root_dir):
        _sweep(root_dir)
        existing = next((r for r in _records(root_dir) if _same_name(r.name, name)), None)
        if existing is not None and not overwrite:
            raise DatasetExistsError(f"a dataset named {existing.name!r} already exists in "
                                     f"{root_dir}; choose another name or pass overwrite=True")
        dataset_id = _new_id(root_dir)
        work = root_dir / f"{_TMP_PREFIX}{dataset_id}"
        work.mkdir()

    try:
        prepared = prepare.prepare(kind, inputs, work, name=name, temporal=temporal, key=key,
                                   variables=variables)
        record = DatasetRecord(
            id=dataset_id, name=name, kind=kind, temporal=temporal, key=prepared.key,
            variables=prepared.variables, source=meta.get("source"),
            version=meta.get("version"),
            description=meta.get("description") or prepared.description,
            license_note=meta.get("license_note"),
            visibility=meta.get("visibility") or "private", owner=meta.get("owner"),
            created_utc=_now_utc(), files=prepared.files,
            checksum=combined_checksum(prepared.originals),
            spacescans_version=_package_version(),
            details={**prepared.details,
                     "originals": [{k: v for k, v in o.items() if k != "path"}
                                   for o in prepared.originals]},
            root=root_dir)
        payload = {"record_format": RECORD_FORMAT, **record.to_dict()}
        _write_json(work / RECORD_FILE, payload)
        retired: Path | None = None
        with _locked(root_dir):
            existing = next((r for r in _records(root_dir) if _same_name(r.name, name)), None)
            if existing is not None:
                if not overwrite:
                    raise DatasetExistsError(
                        f"a dataset named {existing.name!r} was registered in {root_dir} "
                        "meanwhile; choose another name or pass overwrite=True")
                retired = root_dir / f"{_TRASH_PREFIX}{existing.id}"
                os.rename(existing.directory, retired)
            try:
                os.rename(work, root_dir / dataset_id)
            except OSError:
                if retired is not None:
                    os.rename(retired, existing.directory)
                raise
        if retired is not None:
            shutil.rmtree(retired, ignore_errors=True)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return record


def list_datasets(root: str | os.PathLike | None = None) -> list[DatasetRecord]:
    """Every dataset in the root, oldest first (an absent root holds none)."""
    return _records(datasets_root(root))


def _lookup(ref: str, records: Sequence[DatasetRecord]) -> DatasetRecord | None:
    if _ID.fullmatch(ref):
        return next((r for r in records if r.id == ref), None)
    if _VARIABLE_KEY.fullmatch(ref):
        return next((r for r in records if r.variable_key == ref), None)
    return next((r for r in records if _same_name(r.name, ref.strip())), None)


def get_dataset(name_or_id: str | DatasetRecord,
                root: str | os.PathLike | None = None) -> DatasetRecord:
    """The dataset with this name (ignoring case), id or variable key.

    A :class:`DatasetRecord` passed in is read again from its own root.
    Raises :class:`DatasetNotFoundError`.
    """
    if isinstance(name_or_id, DatasetRecord):
        root = name_or_id.root if name_or_id.root is not None else root
        name_or_id = name_or_id.id
    if not isinstance(name_or_id, str) or not name_or_id.strip():
        raise DatasetError("name a dataset by its name or id (a string)")
    root_dir = datasets_root(root)
    records = _records(root_dir)
    found = _lookup(name_or_id, records)
    if found is None:
        known = ", ".join(repr(r.name) for r in records[:10])
        more = f" and {len(records) - 10} more" if len(records) > 10 else ""
        raise DatasetNotFoundError(
            f"no dataset named {name_or_id.strip()!r} in {root_dir}"
            + (f" (registered: {known}{more})" if records else " (it holds no datasets)"))
    return found


def remove_dataset(name_or_id: str | DatasetRecord,
                   root: str | os.PathLike | None = None) -> DatasetRecord:
    """Delete a dataset (its prepared files and record); return the removed record.

    Definitions taken from it earlier (a web task's snapshot) point to files
    that no longer exist and fail their file check.
    """
    record = get_dataset(name_or_id, root)
    root_dir = Path(record.root)
    with _locked(root_dir):
        if not record.directory.is_dir():
            raise DatasetNotFoundError(f"dataset {record.name!r} was removed meanwhile")
        retired = root_dir / f"{_TRASH_PREFIX}{record.id}"
        os.rename(record.directory, retired)
    shutil.rmtree(retired, ignore_errors=True)
    return record


def update_dataset(name_or_id: str | DatasetRecord, *, name: str | None = None,
                   metadata: Mapping[str, Any] | None = None,
                   variables: Sequence[Mapping[str, Any]] | None = None,
                   root: str | os.PathLike | None = None) -> DatasetRecord:
    """Change a dataset's name, metadata (including ``visibility``) or the units and
    descriptions of its variables; the data and the id stay. Returns the new record.

    ``variables`` lists ``{"name", "unit"?, "description"?}`` for existing
    variables only (names cannot change: they are result columns).
    """
    record = get_dataset(name_or_id, root)
    root_dir = Path(record.root)
    changes = _metadata(metadata)
    new_vars = [dict(v) for v in record.variables]
    if variables is not None:
        from spacescans.custom.prepare import check_variable_text
        by_name = {v["name"]: v for v in new_vars}
        for item in variables:
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
                raise DatasetError("each variable change is a mapping with the variable's name")
            target = by_name.get(item["name"])
            if target is None:
                raise DatasetError(f"{record.name!r} has no variable {item['name']!r}")
            extra = sorted(set(item) - {"name", "unit", "description"})
            if extra:
                raise DatasetError(f"only unit and description of a variable can change "
                                   f"(got {', '.join(extra)})")
            unit, description = check_variable_text(item.get("unit", target.get("unit")),
                                                     item.get("description",
                                                              target.get("description")))
            target["unit"], target["description"] = unit, description
    with _locked(root_dir):
        current = _read_record(record.directory, root_dir)
        if current is None:
            raise DatasetNotFoundError(f"dataset {record.name!r} was removed meanwhile")
        new_name = current.name
        if name is not None:
            new_name = check_name(name)
            clash = next((r for r in _records(root_dir)
                          if r.id != current.id and _same_name(r.name, new_name)), None)
            if clash is not None:
                raise DatasetExistsError(f"a dataset named {clash.name!r} already exists in "
                                         f"{root_dir}")
        data = current.to_dict()
        data["name"] = new_name
        data["variables"] = new_vars if variables is not None else data["variables"]
        for field_name, value in changes.items():
            data[field_name] = value if field_name != "visibility" else (value or "private")
        updated = DatasetRecord.from_dict(data, root=root_dir)
        _write_json(updated.directory / RECORD_FILE,
                    {"record_format": RECORD_FORMAT, **updated.to_dict()})
    return updated


def dataset_definition(name_or_id: str | DatasetRecord,
                       root: str | os.PathLike | None = None) -> dict[str, Any]:
    """The linkable definition of a registered dataset (:meth:`DatasetRecord.definition`).

    Pass it in ``link(custom={record.variable_key: definition})`` or snapshot
    it in a web task's ``config.custom_variables``.
    """
    if isinstance(name_or_id, DatasetRecord) and name_or_id.root is not None:
        return name_or_id.definition()
    return get_dataset(name_or_id, root).definition()


def resolve_custom(items: Any, root: str | os.PathLike | None = None) -> dict[str, dict]:
    """``{variable_key: definition}`` for a list of datasets.

    Each item is a registered dataset's name, id or variable key, a
    :class:`DatasetRecord`, or a definition (a mapping from
    :func:`~spacescans.custom.define_polygon` / ``define_raster`` or
    :func:`dataset_definition`). A single name may be passed as a string.
    The definitions are returned as given (not yet checked); the same
    dataset listed twice appears once.
    """
    if isinstance(items, (str, DatasetRecord)) or (
            isinstance(items, Mapping) and "variable_key" in items):
        items = [items]
    if isinstance(items, Mapping) or not isinstance(items, Iterable):
        raise DatasetError("custom datasets are given as a list of names, ids, records or "
                           "definitions")
    out: dict[str, dict] = {}
    for item in items:
        if isinstance(item, DatasetRecord):
            definition = dataset_definition(item, root)
        elif isinstance(item, str):
            definition = get_dataset(item, root).definition()
        elif isinstance(item, Mapping):
            definition = dict(item)
            if not isinstance(definition.get("variable_key"), str):
                raise DatasetError("a custom definition in the list has no variable_key; pass "
                                   "it as {variable_key: definition} or define it again")
        else:
            raise DatasetError(f"cannot use a {type(item).__name__} as a custom dataset; give "
                               "a name, an id, a record or a definition")
        key = definition["variable_key"]
        if key in out and out[key].get("sha256") != definition.get("sha256"):
            raise DatasetError(f"two different datasets in the list share the key {key}")
        out.setdefault(key, definition)
    return out


def link_overlay(custom: Any, keys: Sequence[str], *, root: str | os.PathLike | None = None
                 ) -> tuple[list[str], dict[str, dict], list[dict]]:
    """``orchestrate.link``'s custom hook: the selection, its checked definitions, warnings.

    ``custom`` is the mapping ``{variable_key: definition}`` (as before:
    definitions of variables not in ``keys`` are dropped with a
    ``custom_unused`` warning) or a list for :func:`resolve_custom` (names,
    ids, records, definitions), whose datasets are added to the selection
    after ``keys``. Every definition is checked deeply
    (:func:`~spacescans.custom.check_definition` with ``deep=True``: its files
    exist and still have their recorded sha256).
    """
    from spacescans.custom.definition import check_definition

    keys = list(keys)
    if custom is None:
        return keys, {}, []
    overlay: dict[str, dict] = {}
    warns: list[dict] = []
    if isinstance(custom, Mapping) and "variable_key" not in custom:
        for key, definition in custom.items():
            if key not in keys:
                warns.append({"code": "custom_unused", "variable": key,
                              "message": f"{key}: a definition was given but the variable "
                                         "was not selected; it was ignored"})
                continue
            overlay[key] = check_definition(definition, key=key, deep=True)
        return keys, overlay, warns
    for key, definition in resolve_custom(custom, root).items():
        overlay[key] = check_definition(definition, key=key, deep=True)
        if key not in keys:
            keys.append(key)
    return keys, overlay, warns
