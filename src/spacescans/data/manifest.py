"""Load and check the data manifest (``manifest.json`` next to this file).

The manifest is metadata only. It has four parts:

* ``mirrors``   - where project-mirrored files are downloaded from;
* ``artifacts`` - the mirrored files (archive or bare file, sha256, sizes);
* ``datasets``  - what the pipeline reads under the data root, how it is
  acquired (mirror, provider download, generated locally), licence and
  attribution notes, and the layout rules ``status()`` checks;
* ``variables`` - which datasets each catalog variable needs.

The structural contract is ``manifest.schema.json`` (checked in the test
suite with jsonschema, a dev-only dependency). At runtime this module checks
what the code relies on, with the standard library only, so the module works
on a base install.
"""
from __future__ import annotations

import itertools
import json
import re
import string
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from spacescans.data.errors import ManifestError, UsageError

MANIFEST_PATH = Path(__file__).with_name("manifest.json")
SCHEMA_PATH = Path(__file__).with_name("manifest.schema.json")

ARTIFACT_KINDS = ("tar", "bare")
ARTIFACT_STATUSES = ("available", "pending_upload")
ACQUIRE_MODES = ("mirror", "provider-manual", "provider-scripted", "generated")
RULE_TYPES = ("files", "count", "one_per", "daily", "nonempty_dir")
MIRROR_TYPES = ("sharepoint-share", "http", "file")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Mirror:
    key: str
    type: str
    url: str
    label: str = ""
    range: bool = False
    note: str = ""


@dataclass(frozen=True)
class Artifact:
    name: str
    kind: str                      # "tar" | "bare"
    status: str                    # "available" | "pending_upload"
    mirror: str
    sha256: str
    bytes: int
    extracted_bytes: int | None = None
    payload_bytes: int | None = None   # tar: extracted size without macOS junk
    files: int | None = None           # tar: regular payload members
    roots: tuple[str, ...] = ()        # tar: every member lies under one of these
    dest: str | None = None            # bare: folder (relative to the data root)
    note: str = ""
    interim: str = ""                  # pending_upload: how to get it meanwhile

    @property
    def available(self) -> bool:
        return self.status == "available"

    @property
    def disk_bytes(self) -> int:
        """Bytes the artifact occupies once placed or extracted."""
        if self.kind == "bare":
            return self.bytes
        return int(self.payload_bytes or self.extracted_bytes or 0)

    @property
    def relpath(self) -> str | None:
        """Where a bare artifact lands, relative to the data root."""
        return f"{self.dest}/{self.name}" if self.kind == "bare" else None


@dataclass(frozen=True)
class Terms:
    id: str
    url: str
    summary: str
    license: str = ""
    account: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "url": self.url, "summary": self.summary,
                "license": self.license, "account": self.account}


@dataclass(frozen=True)
class Dataset:
    key: str
    title: str
    acquire: str
    provider: str
    license: str
    redistribution: str
    attribution: str
    artifacts: tuple[str, ...] = ()
    source_url: str = ""
    terms: Terms | None = None
    instructions: tuple[str, ...] = ()
    estimate: Mapping[str, int] = field(default_factory=dict)
    layout: tuple[Mapping[str, Any], ...] = ()

    @property
    def fetchable(self) -> bool:
        """True when ``fetch()`` can download it from the mirror."""
        return self.acquire == "mirror"


@dataclass(frozen=True)
class Requirement:
    """One entry of a variable's ``requires``: any one of the alternatives."""

    any_of: tuple[str, ...]

    @property
    def label(self) -> str:
        return self.any_of[0] if len(self.any_of) == 1 else " | ".join(self.any_of)


@dataclass(frozen=True)
class VariableNeeds:
    key: str
    requires: tuple[Requirement, ...]


@dataclass(frozen=True)
class Manifest:
    schema_version: int
    data_release: str
    mirrors: Mapping[str, Mirror]
    lists: Mapping[str, Any]
    artifacts: Mapping[str, Artifact]
    datasets: Mapping[str, Dataset]
    variables: Mapping[str, VariableNeeds]
    boundaries: Mapping[str, str]
    path: Path | None = None

    # -- lookups -------------------------------------------------------
    def dataset(self, key: str) -> Dataset:
        try:
            return self.datasets[key]
        except KeyError:
            raise UsageError(
                f"unknown dataset {key!r}; known datasets: {', '.join(self.datasets)}",
                details={"unknown": [key], "known": list(self.datasets)},
            ) from None

    def variable(self, key: str) -> VariableNeeds:
        try:
            return self.variables[key]
        except KeyError:
            raise UsageError(
                f"unknown variable {key!r}; known variables: {', '.join(self.variables)}",
                hint="spacescans data status (lists every variable)",
                details={"unknown": [key], "known": list(self.variables)},
            ) from None

    def artifact(self, name: str) -> Artifact:
        try:
            return self.artifacts[name]
        except KeyError:
            raise UsageError(
                f"unknown artifact {name!r}; known artifacts: {', '.join(self.artifacts)}",
                details={"unknown": [name], "known": list(self.artifacts)},
            ) from None

    def artifacts_of(self, dataset_key: str) -> list[Artifact]:
        return [self.artifacts[a] for a in self.dataset(dataset_key).artifacts]

    def datasets_of_artifact(self, name: str) -> list[str]:
        return [k for k, d in self.datasets.items() if name in d.artifacts]

    def terms_ids(self) -> dict[str, str]:
        """terms id -> dataset key."""
        return {d.terms.id: k for k, d in self.datasets.items() if d.terms}

    def resolve_values(self, spec: Any) -> list[str]:
        """Expand a ``values`` spec: a list, ``{"range": [lo, hi]}`` or ``"@list"``."""
        return [str(v) for v in _resolve_values(spec, self.lists, set())]

    def expand(self, template: str, vars_spec: Mapping[str, Any] | None) -> Iterator[tuple[str, dict[str, str]]]:
        """Yield (expanded template, bindings) for the cartesian product of vars."""
        names = [n for _, n, _, _ in string.Formatter().parse(template) if n]
        vars_spec = dict(vars_spec or {})
        missing = [n for n in names if n not in vars_spec]
        if missing:
            raise ManifestError(f"template {template!r} uses undefined variables {missing}")
        keys = list(dict.fromkeys(names))
        pools = [self.resolve_values(vars_spec[k]) for k in keys]
        for combo in itertools.product(*pools):
            binding = dict(zip(keys, combo))
            yield template.format(**binding), binding


def _resolve_values(spec: Any, lists: Mapping[str, Any], seen: set[str]) -> list[Any]:
    if isinstance(spec, str):
        if not spec.startswith("@"):
            raise ManifestError(f"values spec {spec!r}: a string must be '@listname'")
        name = spec[1:]
        if name in seen:
            raise ManifestError(f"list {name!r} refers to itself")
        if name not in lists:
            raise ManifestError(f"unknown list {spec!r}")
        return _resolve_values(lists[name], lists, seen | {name})
    if isinstance(spec, Mapping):
        if set(spec) != {"range"}:
            raise ManifestError(f"values spec {spec!r}: only {{'range': [lo, hi]}} is allowed")
        lo, hi = spec["range"]
        if not (isinstance(lo, int) and isinstance(hi, int) and lo <= hi):
            raise ManifestError(f"bad range {spec!r}")
        return list(range(lo, hi + 1))
    if isinstance(spec, (list, tuple)) and spec:
        return list(spec)
    raise ManifestError(f"bad values spec {spec!r}")


# ---------------------------------------------------------------------------
# loading

def load_manifest(path: str | Path | None = None) -> Manifest:
    """Load and check a manifest. ``None`` loads the packaged one (cached)."""
    if path is None:
        return _packaged_manifest()
    return parse_manifest(json.loads(Path(path).read_text(encoding="utf-8")), path=Path(path))


@lru_cache(maxsize=1)
def _packaged_manifest() -> Manifest:
    return parse_manifest(json.loads(MANIFEST_PATH.read_text(encoding="utf-8")), path=MANIFEST_PATH)


def coerce_manifest(manifest: Manifest | str | Path | Mapping[str, Any] | None) -> Manifest:
    """Accept a Manifest, a path, a raw dict or None (packaged)."""
    if manifest is None or isinstance(manifest, (str, Path)):
        return load_manifest(manifest)
    if isinstance(manifest, Manifest):
        return manifest
    if isinstance(manifest, Mapping):
        return parse_manifest(manifest)
    raise TypeError(f"manifest must be a Manifest, path or dict, not {type(manifest).__name__}")


def _req(obj: Mapping[str, Any], key: str, typ: type | tuple[type, ...], where: str) -> Any:
    if key not in obj:
        raise ManifestError(f"{where}: missing {key!r}")
    val = obj[key]
    if not isinstance(val, typ) or (typ is int and isinstance(val, bool)):
        raise ManifestError(f"{where}: {key!r} must be {getattr(typ, '__name__', typ)}")
    return val


def _relpath_ok(p: str) -> bool:
    parts = p.split("/")
    return bool(p) and not p.startswith("/") and ".." not in parts and "" not in parts and "\\" not in p


def parse_manifest(raw: Mapping[str, Any], *, path: Path | None = None) -> Manifest:
    """Build a :class:`Manifest` from its JSON form, checking cross-references."""
    if not isinstance(raw, Mapping):
        raise ManifestError("manifest must be a JSON object")
    if raw.get("schema_version") != 1:
        raise ManifestError(f"unsupported manifest schema_version {raw.get('schema_version')!r} (expected 1)")
    release = _req(raw, "data_release", str, "manifest")
    lists = dict(raw.get("lists") or {})

    mirrors: dict[str, Mirror] = {}
    for key, m in _req(raw, "mirrors", Mapping, "manifest").items():
        where = f"mirror {key!r}"
        mtype = _req(m, "type", str, where)
        if mtype not in MIRROR_TYPES:
            raise ManifestError(f"{where}: type must be one of {MIRROR_TYPES}")
        mirrors[key] = Mirror(key=key, type=mtype, url=_req(m, "url", str, where),
                              label=m.get("label", ""), range=bool(m.get("range", False)),
                              note=m.get("note", ""))

    artifacts: dict[str, Artifact] = {}
    for name, a in _req(raw, "artifacts", Mapping, "manifest").items():
        where = f"artifact {name!r}"
        if "/" in name or name.startswith("."):
            raise ManifestError(f"{where}: name must be a plain file name")
        kind = _req(a, "kind", str, where)
        status_ = _req(a, "status", str, where)
        if kind not in ARTIFACT_KINDS:
            raise ManifestError(f"{where}: kind must be one of {ARTIFACT_KINDS}")
        if status_ not in ARTIFACT_STATUSES:
            raise ManifestError(f"{where}: status must be one of {ARTIFACT_STATUSES}")
        mirror = _req(a, "mirror", str, where)
        if mirror not in mirrors:
            raise ManifestError(f"{where}: unknown mirror {mirror!r}")
        sha = _req(a, "sha256", str, where)
        if not _SHA256.match(sha):
            raise ManifestError(f"{where}: sha256 must be 64 lowercase hex digits")
        size = _req(a, "bytes", int, where)
        roots = tuple(a.get("roots") or ())
        dest = a.get("dest")
        if kind == "tar":
            if not roots or not all(isinstance(r, str) and _relpath_ok(r) for r in roots):
                raise ManifestError(f"{where}: a tar artifact needs 'roots' (relative paths)")
            _req(a, "payload_bytes", int, where)
            _req(a, "files", int, where)
        else:
            if not (isinstance(dest, str) and _relpath_ok(dest)):
                raise ManifestError(f"{where}: a bare artifact needs 'dest' (a relative folder)")
        if status_ == "pending_upload" and not a.get("interim"):
            raise ManifestError(f"{where}: a pending_upload artifact needs 'interim' instructions")
        artifacts[name] = Artifact(
            name=name, kind=kind, status=status_, mirror=mirror, sha256=sha, bytes=size,
            extracted_bytes=a.get("extracted_bytes"), payload_bytes=a.get("payload_bytes"),
            files=a.get("files"), roots=roots, dest=dest, note=a.get("note", ""),
            interim=a.get("interim", ""),
        )

    datasets: dict[str, Dataset] = {}
    used_artifacts: dict[str, str] = {}
    for key, d in _req(raw, "datasets", Mapping, "manifest").items():
        where = f"dataset {key!r}"
        acquire = _req(d, "acquire", str, where)
        if acquire not in ACQUIRE_MODES:
            raise ManifestError(f"{where}: acquire must be one of {ACQUIRE_MODES}")
        arts = tuple(d.get("artifacts") or ())
        for a in arts:
            if a not in artifacts:
                raise ManifestError(f"{where}: unknown artifact {a!r}")
            if a in used_artifacts:
                raise ManifestError(f"{where}: artifact {a!r} already belongs to {used_artifacts[a]!r}")
            used_artifacts[a] = key
        if acquire == "mirror" and not arts:
            raise ManifestError(f"{where}: a mirror dataset needs 'artifacts'")
        if acquire != "mirror" and not d.get("layout"):
            raise ManifestError(f"{where}: a {acquire} dataset needs 'layout' rules")
        terms = None
        if d.get("terms"):
            t = d["terms"]
            terms = Terms(id=_req(t, "id", str, f"{where} terms"), url=_req(t, "url", str, f"{where} terms"),
                          summary=_req(t, "summary", str, f"{where} terms"),
                          license=t.get("license", ""), account=bool(t.get("account", False)))
        layout = tuple(d.get("layout") or ())
        for i, rule in enumerate(layout):
            _check_rule(rule, f"{where} layout[{i}]", lists)
        datasets[key] = Dataset(
            key=key, title=_req(d, "title", str, where), acquire=acquire,
            provider=_req(d, "provider", str, where), license=_req(d, "license", str, where),
            redistribution=_req(d, "redistribution", str, where),
            attribution=_req(d, "attribution", str, where), artifacts=arts,
            source_url=d.get("source_url", ""), terms=terms,
            instructions=tuple(d.get("instructions") or ()),
            estimate=dict(d.get("estimate") or {}), layout=layout,
        )
    orphans = sorted(set(artifacts) - set(used_artifacts))
    if orphans:
        raise ManifestError(f"artifacts not used by any dataset: {orphans}")
    seen_terms: dict[str, str] = {}
    for key, d in datasets.items():
        if d.terms:
            if d.terms.id in seen_terms:
                raise ManifestError(f"terms id {d.terms.id!r} used by {seen_terms[d.terms.id]!r} and {key!r}")
            seen_terms[d.terms.id] = key

    variables: dict[str, VariableNeeds] = {}
    for key, v in _req(raw, "variables", Mapping, "manifest").items():
        where = f"variable {key!r}"
        reqs = []
        for item in _req(v, "requires", list, where):
            alts = (item,) if isinstance(item, str) else tuple((item or {}).get("any_of") or ())
            if not alts:
                raise ManifestError(f"{where}: bad requirement {item!r}")
            for alt in alts:
                if alt not in datasets:
                    raise ManifestError(f"{where}: unknown dataset {alt!r}")
            reqs.append(Requirement(any_of=alts))
        variables[key] = VariableNeeds(key=key, requires=tuple(reqs))

    boundaries = dict(raw.get("boundaries") or {})
    for b, ds in boundaries.items():
        if ds not in datasets:
            raise ManifestError(f"boundary {b!r}: unknown dataset {ds!r}")

    return Manifest(schema_version=1, data_release=release, mirrors=mirrors, lists=lists,
                    artifacts=artifacts, datasets=datasets, variables=variables,
                    boundaries=boundaries, path=path)


def _check_rule(rule: Mapping[str, Any], where: str, lists: Mapping[str, Any]) -> None:
    kind = rule.get("rule")
    if kind not in RULE_TYPES:
        raise ManifestError(f"{where}: rule must be one of {RULE_TYPES}")
    for key in ("path", "dir"):
        if key in rule and not _relpath_ok(str(rule[key])):
            raise ManifestError(f"{where}: {key} {rule[key]!r} must be a relative path without '..'")
    for spec in (rule.get("vars") or {}).values():
        _resolve_values(spec, lists, set())
    for key in ("values", "years"):
        if key in rule:
            _resolve_values(rule[key], lists, set())
    required = {"files": ("path",), "count": ("dir", "glob", "expect"),
                "one_per": ("dir", "regex", "group", "values"),
                "daily": ("path", "years"), "nonempty_dir": ("path",)}[kind]
    for key in required:
        if key not in rule:
            raise ManifestError(f"{where}: {kind} rule needs {key!r}")
    for key in ("regex",):
        if key in rule:
            try:
                re.compile(rule[key])
            except re.error as e:
                raise ManifestError(f"{where}: bad regex: {e}") from None


def ordered_unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def select(manifest: Manifest, variables: Sequence[str] | str | None,
           datasets: Sequence[str] | str | None) -> tuple[list[str], list[str]]:
    """Normalise a (variables, datasets) request.

    Returns (variable keys, dataset keys). With neither given, every variable.
    Unknown names raise :class:`UsageError` listing all unknown names at once.
    """
    if isinstance(variables, str):
        variables = [variables]
    if isinstance(datasets, str):
        datasets = [datasets]
    if variables is None and datasets is None:
        variables = list(manifest.variables)
    var_keys = ordered_unique(variables or [])
    ds_keys = ordered_unique(datasets or [])
    bad_v = [v for v in var_keys if v not in manifest.variables]
    bad_d = [d for d in ds_keys if d not in manifest.datasets]
    if bad_v or bad_d:
        parts = []
        if bad_v:
            parts.append(f"unknown variable(s) {', '.join(map(repr, bad_v))}; known: {', '.join(manifest.variables)}")
        if bad_d:
            parts.append(f"unknown dataset(s) {', '.join(map(repr, bad_d))}; known: {', '.join(manifest.datasets)}")
        raise UsageError("; ".join(parts),
                         details={"unknown_variables": bad_v, "unknown_datasets": bad_d})
    return var_keys, ds_keys
