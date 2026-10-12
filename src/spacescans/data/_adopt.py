"""``adopt()``: write receipts for mirror data already in place without one.

Data placed by the web app's ``fetch_distribution.sh``, its upload page, or
by hand has no receipt, so ``status()`` calls it ``unverified``. ``adopt``
checks it against the manifest and records a receipt, after which
``status`` reports ``ready`` and ``verify`` can detect later changes:

* a bare file must match the manifest's size and sha256 (a strong check);
* an archive's tree (the files under its roots, macOS metadata and
  ``.part`` files ignored) must match the manifest's file count and payload
  size exactly; every file is then hashed into the receipt. This is weaker
  than a per-file inventory, which the mirror does not publish yet, so the
  receipt records ``origin: "adopt"``.

Nothing is downloaded; nothing outside ``.spacescans/receipts`` is written.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spacescans.data._status import StatusReport, sha256_file, status
from spacescans.data.lock import DataLock
from spacescans.data.manifest import Artifact, coerce_manifest, select
from spacescans.data.paths import resolve_data_dir
from spacescans.data.receipts import read_receipt, write_receipt


@dataclass
class AdoptItem:
    artifact: str
    dataset: str
    result: str          # adopted | would_adopt | has_receipt | missing | mismatch
    detail: str = ""
    files: int = 0
    bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"artifact": self.artifact, "dataset": self.dataset, "result": self.result,
                "detail": self.detail, "files": self.files, "bytes": self.bytes}


@dataclass
class AdoptReport:
    data_dir: str
    dry_run: bool
    items: list[AdoptItem] = field(default_factory=list)
    status: StatusReport | None = None

    @property
    def ok(self) -> bool:
        return not any(i.result == "mismatch" for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {"data_dir": self.data_dir, "dry_run": self.dry_run, "ok": self.ok,
                "items": [i.to_dict() for i in self.items],
                "status": self.status.to_dict() if self.status is not None else None}


def _is_junk(name: str) -> bool:
    return name.startswith("._") or name == ".DS_Store" or name.endswith(".part")


def _tree(root: Path, roots: Sequence[str]) -> list[tuple[str, int]]:
    out = []
    for r in roots:
        base = root / r
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__MACOSX"]
            for name in filenames:
                if _is_junk(name):
                    continue
                p = Path(dirpath) / name
                out.append((p.relative_to(root).as_posix(), p.stat().st_size))
    return sorted(out)


def _check(root: Path, art: Artifact) -> tuple[str, str, list[tuple[str, int]]]:
    if art.kind == "bare":
        p = root / art.relpath
        if not p.is_file():
            return "missing", f"{art.relpath} not found", []
        size = p.stat().st_size
        if size != art.bytes:
            return "mismatch", f"{art.relpath}: {size:,} bytes, expected {art.bytes:,}", []
        return "ok", "", [(art.relpath, size)]
    files = _tree(root, art.roots)
    if not files:
        return "missing", f"nothing under {', '.join(art.roots)}", []
    total = sum(s for _, s in files)
    if len(files) != art.files or total != art.payload_bytes:
        detail = (f"{len(files):,} files / {total:,} bytes under {', '.join(art.roots)}; "
                  f"the archive holds {art.files:,} files / {art.payload_bytes:,} bytes")
        return "mismatch", detail, files
    return "ok", "", files


def adopt(variables: Sequence[str] | str | None = None, *,
          datasets: Sequence[str] | str | None = None,
          data_dir: str | os.PathLike[str] | None = None,
          dry_run: bool = False,
          on_event: Callable[[dict], None] | None = None,
          manifest: Any = None) -> AdoptReport:
    """Check mirror data that has no receipt against the manifest and record receipts.

    With ``dry_run`` only counts and sizes are compared and nothing is
    hashed or written.
    """
    man = coerce_manifest(manifest)
    root = resolve_data_dir(data_dir)
    var_keys, ds_keys = select(man, variables, datasets)
    wanted: list[str] = []
    for v in var_keys:
        for req in man.variable(v).requires:
            wanted.extend(req.any_of)
    wanted = [k for k in dict.fromkeys(wanted + ds_keys) if man.datasets[k].acquire == "mirror"]
    report = AdoptReport(data_dir=str(root), dry_run=dry_run)
    emit = on_event or (lambda e: None)

    def run() -> None:
        for key in wanted:
            for name in man.datasets[key].artifacts:
                art = man.artifacts[name]
                receipt = read_receipt(root, name)
                if receipt is not None and receipt.get("sha256") == art.sha256:
                    report.items.append(AdoptItem(name, key, "has_receipt"))
                    continue
                verdict, detail, files = _check(root, art)
                item = AdoptItem(name, key, verdict, detail, files=len(files),
                                 bytes=sum(s for _, s in files))
                report.items.append(item)
                if verdict != "ok":
                    continue
                if dry_run:
                    item.result = "would_adopt"
                    continue
                digests = []
                for rel, size in files:
                    emit({"ev": "verify", "artifact": name, "file": rel, "bytes": size})
                    digests.append((rel, size, sha256_file(root / rel)))
                if art.kind == "bare" and digests[0][2] != art.sha256:
                    item.result, item.detail = "mismatch", f"{art.relpath}: sha256 does not match the manifest"
                    continue
                write_receipt(root, art, files=digests, source=f"adopted from {root}",
                              origin="adopt", data_release=man.data_release)
                item.result = "adopted"

    if dry_run:
        run()
    else:
        with DataLock(root, purpose="adopt"):
            run()
    report.status = status(var_keys or None, datasets=ds_keys or None, data_dir=root, manifest=man) \
        if (var_keys or ds_keys) else None
    return report
