"""``status()``: what is in the data directory, what is missing, what to do next.

Dataset statuses (``usable`` means a pipeline run can use the data):

==========================  =======  ==================================================
status                      usable   meaning
==========================  =======  ==================================================
``ready``                   yes      complete; mirror files match their receipts (or,
                                     with ``deep``, their sha256)
``differs_from_reference``  yes      complete, but a provider file differs in size from
                                     the copy the project used (republished upstream?)
``unverified``              yes      complete by name and size, but placed without a
                                     receipt (old fetch script, manual copy, web upload)
``incomplete``              no       some files present, some missing
``corrupt``                 no       a size or checksum does not match
``missing``                 no       nothing present; ``spacescans data fetch`` gets it
``action_required``         no       nothing present and no automatic download: a
                                     provider download, terms, or a file not yet on the
                                     mirror; ``instructions`` say what to do. A
                                     ``scripted`` provider dataset is fetched once its
                                     terms are accepted (``next`` names the command)
==========================  =======  ==================================================

A variable takes the worst status of its requirements; a requirement with
alternatives (``any_of``) takes the best of them.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spacescans.data.layout import LayoutResult, check_layout
from spacescans.data.manifest import (
    Artifact,
    Dataset,
    Manifest,
    coerce_manifest,
    select,
)
from spacescans.data.paths import cloud_sync_warning, resolve_data_dir
from spacescans.data.receipts import read_receipt

USABLE = ("ready", "differs_from_reference", "unverified")
# Best first. A requirement takes the best alternative, a variable the worst requirement.
ORDER = ("ready", "differs_from_reference", "unverified", "incomplete", "missing",
         "action_required", "corrupt")
CHUNK = 8 * 1024 * 1024

EventFn = Callable[[dict], None]


def _rank(status: str) -> int:
    return ORDER.index(status)


def sha256_file(path: Path, *, on_bytes: Callable[[int], None] | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(CHUNK)
            if not buf:
                break
            h.update(buf)
            if on_bytes:
                on_bytes(len(buf))
    return h.hexdigest()


@dataclass
class ArtifactState:
    name: str
    kind: str
    status: str            # mirror status: available | pending_upload
    bytes: int
    disk_bytes: int
    state: str             # placed | verified | present | partial | missing | corrupt
    receipt: bool = False
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # warnings, e.g. a stale receipt
    files_checked: int = 0
    bytes_checked: int = 0

    @property
    def complete(self) -> bool:
        return self.state in ("placed", "verified", "present")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "mirror_status": self.status,
                "bytes": self.bytes, "disk_bytes": self.disk_bytes, "state": self.state,
                "receipt": self.receipt, "problems": self.problems, "notes": self.notes,
                "files_checked": self.files_checked, "bytes_checked": self.bytes_checked}


@dataclass
class DatasetStatus:
    dataset: str
    status: str
    title: str
    acquire: str
    provider: str
    license: str
    redistribution: str
    attribution: str
    source_url: str
    terms: dict | None
    artifacts: list[ArtifactState]
    expected: int
    present: int
    present_bytes: int
    missing: list[str]
    missing_count: int
    problems: list[str]
    warnings: list[str]
    download_bytes: int          # what a fetch would still download (0 if not fetchable)
    disk_bytes: int              # space the dataset needs once in place
    fetchable: bool              # spacescans data fetch can complete it now
    fetch_helps: bool            # a fetch gets at least part of what is missing
    needed_artifacts: list[str]  # mirror artifacts a fetch would (re)place
    instructions: list[str]
    next: str
    scripted: bool = False       # fetch downloads it from the provider once its terms are accepted
    generate: list[str] = field(default_factory=list)  # needed artifacts ``prepare`` can regenerate

    @property
    def usable(self) -> bool:
        return self.status in USABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset, "status": self.status, "usable": self.usable,
            "title": self.title, "acquire": self.acquire, "provider": self.provider,
            "license": self.license, "redistribution": self.redistribution,
            "attribution": self.attribution, "source_url": self.source_url, "terms": self.terms,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "expected": self.expected, "present": self.present, "present_bytes": self.present_bytes,
            "missing": self.missing, "missing_count": self.missing_count,
            "problems": self.problems, "warnings": self.warnings,
            "download_bytes": self.download_bytes, "disk_bytes": self.disk_bytes,
            "fetchable": self.fetchable, "fetch_helps": self.fetch_helps,
            "needed_artifacts": self.needed_artifacts,
            "instructions": self.instructions, "next": self.next,
            "scripted": self.scripted, "generate": self.generate,
        }


@dataclass
class VariableStatus:
    variable: str
    status: str
    datasets: list[str]              # every dataset named in its requirements
    chosen: list[str]                # the alternative used for each requirement
    not_usable: list[str]            # chosen datasets that are not usable
    download_bytes: int
    disk_bytes: int
    next: str

    @property
    def usable(self) -> bool:
        return self.status in USABLE

    def to_dict(self) -> dict[str, Any]:
        return {"variable": self.variable, "status": self.status, "usable": self.usable,
                "datasets": self.datasets, "chosen": self.chosen, "not_usable": self.not_usable,
                "download_bytes": self.download_bytes, "disk_bytes": self.disk_bytes,
                "next": self.next}


@dataclass
class StatusReport:
    data_dir: str
    data_release: str
    deep: bool
    variables: list[VariableStatus]
    datasets: list[DatasetStatus]
    download_bytes: int
    disk_bytes: int
    free_bytes: int | None
    next_steps: list[dict[str, Any]]
    warnings: list[str]
    requested_datasets: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """Every requested variable, and every dataset asked for by name, is usable."""
        return all(v.usable for v in self.variables) and all(
            d.usable for d in self.datasets if d.dataset in self.requested_datasets)

    def variable(self, key: str) -> VariableStatus:
        return next(v for v in self.variables if v.variable == key)

    def dataset(self, key: str) -> DatasetStatus:
        return next(d for d in self.datasets if d.dataset == key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_dir": self.data_dir, "data_release": self.data_release, "deep": self.deep,
            "ready": self.ready,
            "variables": [v.to_dict() for v in self.variables],
            "datasets": [d.to_dict() for d in self.datasets],
            "download_bytes": self.download_bytes, "disk_bytes": self.disk_bytes,
            "free_bytes": self.free_bytes, "next_steps": self.next_steps,
            "warnings": self.warnings, "requested_datasets": self.requested_datasets,
        }


# ---------------------------------------------------------------------------

def _check_artifact(root: Path, art: Artifact, layout: LayoutResult, *, deep: bool,
                    on_event: EventFn | None) -> ArtifactState:
    st = ArtifactState(name=art.name, kind=art.kind, status=art.status, bytes=art.bytes,
                       disk_bytes=art.disk_bytes, state="missing")
    receipt = read_receipt(root, art.name)
    if receipt is not None and receipt.get("sha256") != art.sha256:
        st.notes.append("its receipt is for a different version of this artifact "
                        f"(data release {receipt.get('data_release')}); ignored")
        receipt = None
    if receipt is not None:
        st.receipt = True
        bad: list[str] = []
        for rel, size, sha in receipt["files"]:
            p = root / rel
            try:
                actual = p.stat().st_size
            except OSError:
                bad.append(f"{rel}: missing")
                continue
            if actual != size:
                bad.append(f"{rel}: {actual:,} bytes, receipt says {size:,}")
                continue
            if deep and sha:
                if on_event:
                    on_event({"ev": "verify", "artifact": art.name, "file": rel, "bytes": size})
                if sha256_file(p) != sha:
                    bad.append(f"{rel}: sha256 differs from the receipt")
                    continue
                st.bytes_checked += size
            st.files_checked += 1
        if bad:
            st.state = "corrupt"
            st.problems.extend(bad[:20])
            if len(bad) > 20:
                st.problems.append(f"... and {len(bad) - 20} more")
        else:
            st.state = "verified" if deep else "placed"
        return st
    # No (valid) receipt: judge by what is on disk.
    if art.kind == "bare":
        p = root / art.relpath
        try:
            size = p.stat().st_size
        except OSError:
            return st
        if size != art.bytes:
            st.state = "corrupt"
            st.problems.append(f"{art.relpath}: {size:,} bytes, expected {art.bytes:,}")
            return st
        if deep:
            if on_event:
                on_event({"ev": "verify", "artifact": art.name, "file": art.relpath, "bytes": size})
            if sha256_file(p) != art.sha256:
                st.state = "corrupt"
                st.problems.append(f"{art.relpath}: sha256 does not match the manifest")
                return st
            st.files_checked, st.bytes_checked = 1, size
            st.state = "verified"
        else:
            st.state = "present"
        return st
    # tar without a (valid) receipt: the dataset's layout rules decide.
    if layout.complete and not layout.size_problems:
        st.state = "present"
    elif layout.present:
        st.state = "partial"
    return st


def dataset_status(manifest: Manifest, key: str, root: Path, *, deep: bool = False,
                   on_event: EventFn | None = None) -> DatasetStatus:
    ds: Dataset = manifest.dataset(key)
    layout = check_layout(manifest, ds, root, deep=deep)
    arts = [_check_artifact(root, manifest.artifacts[a], layout, deep=deep, on_event=on_event)
            for a in ds.artifacts]

    problems = list(layout.size_problems) + list(layout.layout_problems)
    for a in arts:
        problems.extend(f"{a.name}: {p}" for p in a.problems if a.state == "corrupt")
    warnings = list(layout.warnings)
    warnings.extend(f"{a.name}: {n}" for a in arts for n in a.notes)

    # Artifacts a fetch would have to (re)place. A tar whose tree is
    # incomplete has state "partial"; a damaged one "corrupt": neither is complete.
    needed = [a for a in arts if not a.complete]
    fetchable = ds.acquire == "mirror" and bool(needed) and all(
        manifest.artifacts[a.name].available for a in needed)
    fetch_helps = ds.acquire == "mirror" and any(
        manifest.artifacts[a.name].available for a in needed)
    corrupt = bool(layout.size_problems) or any(a.state == "corrupt" for a in arts)

    if layout.present == 0 and not any(a.receipt for a in arts):
        status = "missing" if fetchable else "action_required"
    elif corrupt:
        status = "corrupt"
    elif not layout.complete:
        status = "incomplete"
    elif ds.acquire == "mirror":
        status = "ready" if all(a.state in ("placed", "verified") for a in arts) else "unverified"
    elif layout.reference_mismatch:
        status = "differs_from_reference"
    else:
        status = "ready"
    if layout.reference_mismatch:
        warnings.extend(layout.reference_mismatch[:5])

    if status in USABLE:
        download = 0
    elif ds.acquire == "mirror":  # only what the mirror can serve now
        download = sum(manifest.artifacts[a.name].bytes for a in needed
                       if manifest.artifacts[a.name].available)
    else:
        download = int(ds.estimate.get("download_bytes", 0))
    if ds.acquire == "mirror":
        disk = sum(manifest.artifacts[a].disk_bytes for a in ds.artifacts)
    else:
        disk = int(ds.estimate.get("disk_bytes", 0) or layout.present_bytes)

    instructions = list(ds.instructions)
    if ds.acquire == "mirror":
        for a in needed:
            art = manifest.artifacts[a.name]
            if not art.available and art.interim:
                instructions.append(f"{art.name}: {art.interim}")
    generate = [a.name for a in needed if ds.acquire == "mirror"
                and not manifest.artifacts[a.name].available and manifest.artifacts[a.name].generate]
    next_step = _dataset_next(ds, status, fetchable, fetch_helps, needed, manifest)

    return DatasetStatus(
        dataset=key, status=status, title=ds.title, acquire=ds.acquire, provider=ds.provider,
        license=ds.license, redistribution=ds.redistribution, attribution=ds.attribution,
        source_url=ds.source_url, terms=ds.terms.to_dict() if ds.terms else None,
        artifacts=arts, expected=layout.expected, present=layout.present,
        present_bytes=layout.present_bytes, missing=layout.missing,
        missing_count=layout.missing_count, problems=problems, warnings=warnings,
        download_bytes=download, disk_bytes=disk, fetchable=fetchable, fetch_helps=fetch_helps,
        needed_artifacts=[a.name for a in needed] if ds.acquire == "mirror" else [],
        instructions=instructions, next=next_step, scripted=ds.scripted, generate=generate,
    )


def _dataset_next(ds: Dataset, status: str, fetchable: bool, fetch_helps: bool,
                  needed: list[ArtifactState], manifest: Manifest) -> str:
    if status in USABLE:
        if status == "unverified":
            return f"optional: spacescans data verify --dataset {ds.key}"
        return ""
    if fetchable:
        return f"spacescans data fetch --dataset {ds.key}"
    if ds.acquire == "mirror":
        pending = [a.name for a in needed if not manifest.artifacts[a.name].available]
        head = f"spacescans data fetch --dataset {ds.key}; then " if fetch_helps else ""
        gens = sorted({manifest.artifacts[a].generate for a in pending if manifest.artifacts[a].generate})
        if gens and len(gens) == 1 and all(manifest.artifacts[a].generate for a in pending):
            return (head + f"spacescans data prepare {gens[0]} (not yet on the mirror: "
                    + ", ".join(pending) + ")")
        return (head + "not yet on the mirror: " + ", ".join(pending) +
                "; see instructions (or fetch with --mirror DIR)")
    if ds.acquire == "generated":
        return "produced on first use; see instructions"
    if ds.scripted:
        return f"spacescans data fetch --dataset {ds.key} --accept-terms {ds.terms.id}"
    return "manual download from the provider; see instructions"


def status(variables: Sequence[str] | str | None = None, *,
           datasets: Sequence[str] | str | None = None,
           data_dir: str | os.PathLike[str] | None = None,
           deep: bool = False,
           manifest: Any = None,
           on_event: EventFn | None = None) -> StatusReport:
    """Check the data directory for the given variables and/or datasets.

    With neither ``variables`` nor ``datasets``, every catalog variable is
    checked. ``deep=True`` also hashes every file that has a recorded sha256
    (receipts, bare mirror files) and checks provider file signatures; that
    reads the whole tree. Never writes anything.
    """
    man = coerce_manifest(manifest)
    root = resolve_data_dir(data_dir)
    var_keys, ds_keys = select(man, variables, datasets)

    wanted: list[str] = []
    for v in var_keys:
        for req in man.variable(v).requires:
            wanted.extend(req.any_of)
    wanted.extend(ds_keys)
    wanted = list(dict.fromkeys(wanted))
    ds_status = {k: dataset_status(man, k, root, deep=deep, on_event=on_event) for k in wanted}

    var_status: list[VariableStatus] = []
    for v in var_keys:
        chosen, all_ds = [], []
        for req in man.variable(v).requires:
            all_ds.extend(req.any_of)
            best = min(req.any_of, key=lambda k: (_rank(ds_status[k].status),
                                                  0 if ds_status[k].fetchable else 1))
            chosen.append(best)
        worst = max((ds_status[k].status for k in chosen), key=_rank)
        not_usable = [k for k in chosen if not ds_status[k].usable]
        dl = _sum_artifact_bytes(man, [ds_status[k] for k in not_usable])
        disk = sum(ds_status[k].disk_bytes for k in chosen)
        var_status.append(VariableStatus(
            variable=v, status=worst, datasets=list(dict.fromkeys(all_ds)), chosen=chosen,
            not_usable=not_usable, download_bytes=dl, disk_bytes=disk,
            next=_variable_next(v, [ds_status[k] for k in not_usable]),
        ))

    in_use = list(dict.fromkeys([k for vs in var_status for k in vs.chosen] + ds_keys))
    need = [ds_status[k] for k in in_use if not ds_status[k].usable]
    total_dl = _sum_artifact_bytes(man, need)
    total_disk = sum(ds_status[k].disk_bytes for k in in_use)

    warnings = []
    cw = cloud_sync_warning(root)
    if cw:
        warnings.append(cw)
    if not root.exists():
        warnings.append(f"data directory {root} does not exist yet; fetch creates it")
    free = _free_bytes(root)

    report = StatusReport(
        data_dir=str(root), data_release=man.data_release, deep=deep,
        variables=var_status, datasets=[ds_status[k] for k in wanted],
        download_bytes=total_dl, disk_bytes=total_disk, free_bytes=free,
        next_steps=_next_steps(var_status, ds_keys, ds_status, in_use),
        warnings=warnings, requested_datasets=list(ds_keys),
    )
    return report


def _sum_artifact_bytes(man: Manifest, statuses: Sequence[DatasetStatus]) -> int:
    """Download bytes, counting an artifact shared by several datasets once."""
    seen: set[str] = set()
    total = 0
    for d in statuses:
        if d.acquire != "mirror":
            total += d.download_bytes
            continue
        for name in d.needed_artifacts:
            if name not in seen and man.artifacts[name].available:
                seen.add(name)
                total += man.artifacts[name].bytes
    return total


def _variable_next(v: str, not_usable: Sequence[DatasetStatus]) -> str:
    if not not_usable:
        return ""
    if all(d.fetchable for d in not_usable):
        return f"spacescans data fetch {v}"
    if all(d.fetchable or d.scripted for d in not_usable):
        ids = list(dict.fromkeys(d.terms["id"] for d in not_usable if d.scripted and d.terms))
        return f"spacescans data fetch {v}" + "".join(f" --accept-terms {i}" for i in ids)
    manual = [d.dataset for d in not_usable if not d.fetchable]
    head = "spacescans data fetch " + v + "; then " if any(d.fetch_helps for d in not_usable) else ""
    return head + "manual step for dataset " + ", ".join(manual) + " (see its instructions)"


def _next_steps(var_status: Sequence[VariableStatus], ds_keys: Sequence[str],
                ds_status: Mapping[str, DatasetStatus], in_use: Sequence[str]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    fetch_vars = [v.variable for v in var_status
                  if v.not_usable and any(ds_status[k].fetch_helps for k in v.not_usable)]
    fetch_ds = [k for k in ds_keys if not ds_status[k].usable and ds_status[k].fetch_helps]
    if fetch_vars or fetch_ds:
        cmd = "spacescans data fetch " + " ".join(fetch_vars)
        if fetch_ds:
            cmd += "".join(f" --dataset {k}" for k in fetch_ds)
        steps.append({"action": "fetch", "variables": fetch_vars, "datasets": fetch_ds,
                      "command": " ".join(cmd.split())})
    for k in in_use:
        d = ds_status[k]
        if d.usable or d.fetchable:
            continue
        step = {"action": "manual", "dataset": k, "status": d.status,
                "terms": d.terms, "instructions": d.instructions}
        if d.scripted and d.terms:
            # Additive keys: spacescans can download it once the terms are accepted.
            step.update(scripted=True, accept_terms=[d.terms["id"]],
                        command=f"spacescans data fetch --dataset {k} --accept-terms {d.terms['id']}")
        elif d.generate:
            step.update(generate=list(d.generate), command="spacescans data prepare templates")
        steps.append(step)
    return steps


def _free_bytes(root: Path) -> int | None:
    p = root
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None
