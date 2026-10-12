"""``requirements()``: which datasets and artifacts each catalog variable needs.

Pure manifest metadata (``variables[k].requires``); it never looks at a data
directory (``status()`` does that). A requirement with alternatives
(``any_of``) lists the preferred alternative first: the mirror cache before
the raw provider form, for example ``tiger_roads_cache | tiger_roads_raw``.
``default`` is that preferred choice for every requirement, and the sizes,
artifacts and terms describe it, counting an artifact shared by several
datasets once.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from spacescans.data.manifest import Manifest, coerce_manifest, ordered_unique, select


@dataclass
class DatasetNeed:
    dataset: str
    title: str
    acquire: str                 # mirror | provider-manual | provider-scripted | generated
    artifacts: list[str]         # mirror files that make up the dataset
    pending: list[str]           # those not on the mirror yet
    generate: list[str]          # those spacescans can regenerate locally (prepare templates)
    terms: str | None            # provider terms id, accepted per call only
    download_bytes: int          # mirror bytes, or the provider estimate
    disk_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {"dataset": self.dataset, "title": self.title, "acquire": self.acquire,
                "artifacts": self.artifacts, "pending": self.pending, "generate": self.generate,
                "terms": self.terms, "download_bytes": self.download_bytes,
                "disk_bytes": self.disk_bytes}


@dataclass
class VariableRequirements:
    variable: str
    requires: list[list[str]]    # each requirement's alternatives, preferred first
    datasets: list[str]          # every dataset named, in order
    default: list[str]           # the preferred alternative of each requirement
    artifacts: list[str]         # mirror artifacts of the default datasets
    pending: list[str]           # of those, the ones not on the mirror yet
    provider: list[str]          # default datasets that come from a provider, not the mirror
    terms: list[str]             # their terms ids (scripted downloads need them accepted)
    download_bytes: int          # default choice, shared artifacts once
    disk_bytes: int
    needs: dict[str, DatasetNeed] = field(default_factory=dict)  # every dataset named

    def to_dict(self) -> dict[str, Any]:
        return {"variable": self.variable, "requires": self.requires, "datasets": self.datasets,
                "default": self.default, "artifacts": self.artifacts, "pending": self.pending,
                "provider": self.provider, "terms": self.terms,
                "download_bytes": self.download_bytes, "disk_bytes": self.disk_bytes,
                "needs": {k: v.to_dict() for k, v in self.needs.items()}}


class Requirements(dict):
    """variable key -> :class:`VariableRequirements`, in request order."""

    @property
    def datasets(self) -> list[str]:
        """The default datasets of every variable, each once."""
        return ordered_unique(k for v in self.values() for k in v.default)

    @property
    def artifacts(self) -> list[str]:
        """The mirror artifacts of every variable's default datasets, each once."""
        return ordered_unique(a for v in self.values() for a in v.artifacts)

    @property
    def terms(self) -> list[str]:
        return ordered_unique(t for v in self.values() for t in v.terms)

    def to_dict(self) -> dict[str, Any]:
        return {"variables": {k: v.to_dict() for k, v in self.items()},
                "datasets": self.datasets, "artifacts": self.artifacts, "terms": self.terms}


def _need(man: Manifest, key: str) -> DatasetNeed:
    ds = man.dataset(key)
    arts = [man.artifacts[a] for a in ds.artifacts]
    if ds.acquire == "mirror":
        download = sum(a.bytes for a in arts)
        disk = sum(a.disk_bytes for a in arts)
    else:
        download = int(ds.estimate.get("download_bytes", 0))
        disk = int(ds.estimate.get("disk_bytes", 0))
    return DatasetNeed(dataset=key, title=ds.title, acquire=ds.acquire,
                       artifacts=[a.name for a in arts],
                       pending=[a.name for a in arts if not a.available],
                       generate=[a.name for a in arts if a.generate],
                       terms=ds.terms.id if ds.terms else None,
                       download_bytes=download, disk_bytes=disk)


def requirements(variables: Sequence[str] | str | None = None, *,
                 manifest: Any = None) -> Requirements:
    """The datasets and mirror artifacts each variable needs (all variables by default).

    Unknown variable keys raise :class:`~spacescans.data.UsageError` (kind
    ``config``) naming every unknown key at once.
    """
    man = coerce_manifest(manifest)
    if isinstance(variables, str):
        variables = [variables]
    var_keys, _ = select(man, list(variables) if variables is not None else None, None)
    out = Requirements()
    for v in var_keys:
        reqs = [list(r.any_of) for r in man.variable(v).requires]
        named = ordered_unique(k for alts in reqs for k in alts)
        needs = {k: _need(man, k) for k in named}
        default = ordered_unique(alts[0] for alts in reqs)
        artifacts = ordered_unique(a for k in default for a in needs[k].artifacts)
        provider = [k for k in default if needs[k].acquire != "mirror"]
        out[v] = VariableRequirements(
            variable=v, requires=reqs, datasets=named, default=default, artifacts=artifacts,
            pending=[a for a in artifacts if not man.artifacts[a].available],
            provider=provider,
            terms=ordered_unique(needs[k].terms for k in provider if needs[k].terms),
            download_bytes=sum(man.artifacts[a].bytes for a in artifacts)
            + sum(needs[k].download_bytes for k in provider),
            disk_bytes=sum(man.artifacts[a].disk_bytes for a in artifacts)
            + sum(needs[k].disk_bytes for k in provider),
            needs=needs,
        )
    return out
