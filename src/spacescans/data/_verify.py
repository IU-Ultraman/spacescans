"""``verify()``: sha256 checks of the data directory (and of loose archives).

``status(deep=True)`` does the same per-dataset work; ``verify`` adds a
per-artifact summary, an ``ok`` verdict (nothing corrupt), and can check
archive files a user downloaded by hand against the manifest.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from spacescans.data.manifest import coerce_manifest
from spacescans.data._status import StatusReport, sha256_file, status


@dataclass
class ArchiveCheck:
    path: str
    artifact: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "artifact": self.artifact, "ok": self.ok, "detail": self.detail}


@dataclass
class VerifyReport:
    status: StatusReport
    archives: list[ArchiveCheck] = field(default_factory=list)

    @property
    def corrupt(self) -> list[str]:
        return [d.dataset for d in self.status.datasets if d.status == "corrupt"]

    @property
    def ok(self) -> bool:
        """Nothing that could be checked failed (missing data is not a failure)."""
        return not self.corrupt and all(a.ok for a in self.archives)

    def artifacts(self) -> list[dict[str, Any]]:
        out = []
        for d in self.status.datasets:
            for a in d.artifacts:
                out.append({"dataset": d.dataset, **a.to_dict()})
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "corrupt": self.corrupt, "artifacts": self.artifacts(),
                "archives": [a.to_dict() for a in self.archives],
                "status": self.status.to_dict()}


def verify(variables: Sequence[str] | str | None = None, *,
           datasets: Sequence[str] | str | None = None,
           data_dir: str | os.PathLike[str] | None = None,
           deep: bool = True,
           archives: Sequence[str | os.PathLike[str]] = (),
           on_event: Callable[[dict], None] | None = None,
           manifest: Any = None) -> VerifyReport:
    """Hash every placed file that has a recorded sha256 and compare.

    ``deep=False`` compares sizes only (what ``status()`` does by default).
    ``archives`` are archive files outside the data directory to check by
    name, size and sha256 against the manifest. Never writes anything.
    """
    man = coerce_manifest(manifest)
    st = status(variables, datasets=datasets, data_dir=data_dir, deep=deep,
                manifest=man, on_event=on_event)
    checks: list[ArchiveCheck] = []
    for raw in archives:
        p = Path(raw).expanduser()
        art = man.artifact(p.name)
        if not p.is_file():
            checks.append(ArchiveCheck(str(p), art.name, False, "file not found"))
            continue
        size = p.stat().st_size
        if size != art.bytes:
            checks.append(ArchiveCheck(str(p), art.name, False,
                                       f"{size:,} bytes, expected {art.bytes:,} (truncated?)"))
            continue
        if on_event:
            on_event({"ev": "verify", "artifact": art.name, "file": str(p), "bytes": size})
        sha = sha256_file(p)
        checks.append(ArchiveCheck(str(p), art.name, sha == art.sha256,
                                   "sha256 matches" if sha == art.sha256 else "sha256 does not match"))
    return VerifyReport(status=st, archives=checks)
