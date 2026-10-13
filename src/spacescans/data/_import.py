"""``import_files()``: place provider files the user downloaded by hand.

For datasets spacescans does not download (VNL needs an EOG login; TEMIS,
FAQSD and ACAG come from their providers), the user downloads the files and
this puts them where the readers look. The destination comes from the
dataset's layout rules:

* ``files``   - the file name must be one of the expected names;
* ``one_per`` - the name must match the rule's regex (VNL: one per year);
* ``daily``   - ``{var}{yyyymmdd}.ext`` lands in the per-variable, per-year folder;
* ``import`` rules (manifest) - a regex whose groups name the folder: ACAG's
  ``V5NA05[.02].Hybrid<SP>[-<SP>][.FromBiomass].xNorthAmerica.<range>.nc``
  goes to ``ACAG/C4/xNorthAmerica/<SP>[_bm]/BiWeekly/``; files of other years
  are rejected.

``paths`` may name folders: they are walked recursively (macOS metadata and
hidden files skipped), so a downloaded ACAG tree can be imported in one go.
A file whose rule declares a signature (TEMIS HDF4, ACAG HDF5) must start
with it; an HTML error page saved under a data file's name is rejected.
A ``.gz`` file is unpacked while it streams (VNL ships ``.tif.gz``, FAQSD
``.txt.gz``). Every file is written to ``<target>.part`` and moved into place
with ``os.replace``.
"""
from __future__ import annotations

import gzip
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spacescans.data._status import StatusReport, status
from spacescans.data.errors import DataCorruptError, UsageError
from spacescans.data.extract import FILE_MODE
from spacescans.data.lock import DataLock
from spacescans.data.manifest import Dataset, Manifest, coerce_manifest
from spacescans.data.paths import resolve_data_dir

CHUNK = 4 * 1024 * 1024


@dataclass
class ImportItem:
    source: str
    target: str | None
    result: str            # placed | skipped_same_size | rejected
    detail: str = ""
    bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "target": self.target, "result": self.result,
                "detail": self.detail, "bytes": self.bytes}


@dataclass
class ImportReport:
    dataset: str
    data_dir: str
    items: list[ImportItem] = field(default_factory=list)
    status: StatusReport | None = None

    @property
    def ok(self) -> bool:
        return not any(i.result == "rejected" for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {"dataset": self.dataset, "data_dir": self.data_dir, "ok": self.ok,
                "items": [i.to_dict() for i in self.items],
                "status": self.status.to_dict() if self.status is not None else None}


def _targets(man: Manifest, ds: Dataset):
    """Yield (function name -> relative target path or None, expected signature),
    one per import or layout rule."""
    for imp in ds.imports:
        years = man.resolve_values(imp.years) if imp.years is not None else None
        yield ((lambda name, imp=imp, years=years: imp.route(name, years)[0]),
               bytes.fromhex(imp.magic) if imp.magic else b"")
    for rule in ds.layout:
        kind = rule["rule"]
        if kind == "files":
            by_name = {Path(rel).name: rel for rel, _ in man.expand(rule["path"], rule.get("vars"))}
            yield (lambda name, m=by_name: m.get(name)), b""
        elif kind == "one_per":
            rx = re.compile(rule["regex"])
            yield (lambda name, rx=rx, d=rule["dir"]: f"{d}/{name}" if rx.match(name) else None), b""
        elif kind == "daily":
            tpl = rule["path"]
            var_values = {k: man.resolve_values(v) for k, v in (rule.get("vars") or {}).items()}
            base = Path(tpl).name
            pattern = re.escape(base)
            for k, vals in var_values.items():
                pattern = pattern.replace(re.escape("{" + k + "}"),
                                          f"(?P<{k}>{'|'.join(map(re.escape, vals))})", 1)
                pattern = pattern.replace(re.escape("{" + k + "}"), f"(?P={k})")
            pattern = pattern.replace(re.escape("{yyyymmdd}"), r"(?P<yyyymmdd>\d{8})")
            rx = re.compile("^" + pattern + "$")

            def daily(name, rx=rx, tpl=tpl):
                m = rx.match(name)
                if not m:
                    return None
                b = m.groupdict()
                return tpl.format(**b, year=b["yyyymmdd"][:4])
            yield daily, bytes.fromhex(rule["magic"]) if rule.get("magic") else b""


def import_files(dataset: str, paths: Sequence[str | os.PathLike[str]], *,
                 data_dir: str | os.PathLike[str] | None = None,
                 move: bool = False, manifest: Any = None) -> ImportReport:
    """Place hand-downloaded provider files of ``dataset`` into the data directory.

    Files whose names the dataset does not expect are rejected (and left
    alone). A target that already exists with the same size is skipped.
    ``move=True`` removes each source after it is placed.
    """
    man = coerce_manifest(manifest)
    root = resolve_data_dir(data_dir)
    ds = man.dataset(dataset)
    if ds.acquire == "mirror":
        raise UsageError(f"{dataset} comes from the project mirror; use fetch (or fetch --mirror DIR)",
                         hint=f"spacescans data fetch --dataset {dataset}")
    matchers = list(_targets(man, ds))
    if not matchers:
        raise UsageError(
            f"spacescans cannot tell where {dataset} files go from their names; place them by hand",
            details={"instructions": list(ds.instructions)})
    report = ImportReport(dataset=dataset, data_dir=str(root))
    root.mkdir(parents=True, exist_ok=True)
    with DataLock(root, purpose="import"):
        for src in _expand_paths(paths):
            gz = src.name.endswith(".gz")
            name = src.name[:-3] if gz else src.name
            hit = next(((t, mg) for t, mg in ((f(name), mg) for f, mg in matchers) if t), None)
            target, magic = hit if hit else (None, b"")
            if not src.is_file():
                report.items.append(ImportItem(str(src), target, "rejected", "file not found"))
                continue
            if target is None:
                report.items.append(ImportItem(str(src), None, "rejected",
                                               f"{name} is not a file {dataset} expects"
                                               + _why(man, ds, name)))
                continue
            if magic and _head(src, len(magic), gunzip=gz) != magic:
                report.items.append(ImportItem(str(src), target, "rejected",
                                               f"{name} does not start with the {dataset} file "
                                               f"signature {magic.hex()} (an error page?)"))
                continue
            dest = root / target
            if dest.is_file() and not gz and dest.stat().st_size == src.stat().st_size:
                report.items.append(ImportItem(str(src), target, "skipped_same_size",
                                               bytes=dest.stat().st_size))
                continue
            size = _place(src, dest, gunzip=gz)
            report.items.append(ImportItem(str(src), target, "placed", bytes=size))
            if move:
                src.unlink()
    report.status = status(datasets=[dataset], data_dir=root, manifest=man)
    return report


def _junk(name: str) -> bool:
    return name.startswith(".") or name.endswith(".part") or name == "__MACOSX"


def _expand_paths(paths: Sequence[str | os.PathLike[str]]):
    """Files as given; folders walked recursively in name order, junk skipped."""
    for raw in paths:
        src = Path(raw).expanduser()
        if not src.is_dir():
            yield src
            continue
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = sorted(d for d in dirnames if not _junk(d))
            for f in sorted(filenames):
                if not _junk(f):
                    yield Path(dirpath) / f


def _why(man: Manifest, ds: Dataset, name: str) -> str:
    for imp in ds.imports:
        years = man.resolve_values(imp.years) if imp.years is not None else None
        target, why = imp.route(name, years)
        if target is None and why != "name does not match":
            return f" ({why})"
    return ""


def _head(path: Path, n: int, *, gunzip: bool) -> bytes:
    try:
        if gunzip:
            with gzip.open(path, "rb") as f:
                return f.read(n)
        with open(path, "rb") as f:
            return f.read(n)
    except (OSError, EOFError):
        return b""


def _place(src: Path, dest: Path, *, gunzip: bool) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        if gunzip:
            try:
                with gzip.open(src, "rb") as fi, open(part, "wb") as fo:
                    shutil.copyfileobj(fi, fo, CHUNK)
            except (OSError, EOFError) as e:
                raise DataCorruptError(f"could not unpack {src.name}: {e}",
                                       hint="download it again", details={"path": str(src)}) from e
        else:
            shutil.copyfile(src, part)
        os.chmod(part, FILE_MODE)
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)
    return dest.stat().st_size
