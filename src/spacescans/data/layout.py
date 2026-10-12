"""Layout rules: is a dataset's tree complete under the data root?

Each dataset in the manifest carries ``layout`` rules (bare mirror files get
an implicit exact-name-and-size rule). The rules replace the web's eight
presence-only pre-flights (``variable_registry.py:72-358``) with checks of
exact names, counts and sizes:

``files``        every expansion of ``path`` exists as a regular file;
                 optional exact ``bytes`` (a mismatch is corruption) and
                 ``reference_bytes`` (a mismatch means the provider's copy
                 differs from the one the project used, not corruption)
``count``        each expansion of ``dir`` holds ``expect`` files matching
                 ``glob`` (and ``regex``)
``one_per``      exactly one file per value of a regex group (VNL: one per
                 year; the reader reads every ``*.tif``, so a second product
                 for the same year would be read too)
``daily``        one file per calendar day per variable, minus known gaps
``nonempty_dir`` the folder exists and is not empty

Only names, sizes and (with ``deep``) magic bytes are checked here; sha256
checks against receipts live in :mod:`spacescans.data._status`.
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import itertools
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spacescans.data.manifest import Dataset, Manifest

MAX_LISTED = 20


@dataclass
class LayoutResult:
    expected: int = 0
    present: int = 0
    present_bytes: int = 0
    missing: list[str] = field(default_factory=list)         # what is absent
    missing_count: int = 0
    size_problems: list[str] = field(default_factory=list)   # corruption
    layout_problems: list[str] = field(default_factory=list)  # e.g. two products for one year
    reference_mismatch: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    present_files: list[str] = field(default_factory=list)   # relpaths found by 'files' rules

    @property
    def complete(self) -> bool:
        return self.present >= self.expected and not self.layout_problems

    def add_missing(self, item: str, n: int = 1) -> None:
        self.missing_count += n
        if len(self.missing) < MAX_LISTED:
            self.missing.append(item)


def _is_junk(name: str) -> bool:
    return name.startswith("._") or name == ".DS_Store" or name.endswith(".part")


def _list_files(d: Path) -> list[os.DirEntry]:
    try:
        with os.scandir(d) as it:
            return [e for e in it if not _is_junk(e.name) and e.is_file()]
    except (FileNotFoundError, NotADirectoryError):
        return []
    except PermissionError:
        return []


def _read_magic(path: Path, n: int) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


def bare_rules(manifest: Manifest, ds: Dataset) -> list[dict[str, Any]]:
    """Implicit rules for a dataset's bare mirror files: exact name and size."""
    rules = []
    for name in ds.artifacts:
        a = manifest.artifacts[name]
        if a.kind == "bare":
            rules.append({"rule": "files", "path": a.relpath, "bytes": {a.relpath: a.bytes}})
    return rules


def check_layout(manifest: Manifest, ds: Dataset, root: Path, *, deep: bool = False) -> LayoutResult:
    res = LayoutResult()
    for rule in list(ds.layout) + bare_rules(manifest, ds):
        kind = rule["rule"]
        if kind == "files":
            _files(manifest, rule, root, res)
        elif kind == "count":
            _count(manifest, rule, root, res, deep)
        elif kind == "one_per":
            _one_per(manifest, rule, root, res)
        elif kind == "daily":
            _daily(manifest, rule, root, res, deep)
        elif kind == "nonempty_dir":
            _nonempty(manifest, rule, root, res)
    return res


def _files(manifest: Manifest, rule: Mapping[str, Any], root: Path, res: LayoutResult) -> None:
    exact = rule.get("bytes") or {}
    ref = rule.get("reference_bytes") or {}
    for rel, _ in manifest.expand(rule["path"], rule.get("vars")):
        res.expected += 1
        p = root / rel
        try:
            st = p.stat()
        except OSError:
            res.add_missing(rel)
            continue
        if not p.is_file():
            res.add_missing(rel)
            continue
        res.present += 1
        res.present_bytes += st.st_size
        res.present_files.append(rel)
        if rel in exact and st.st_size != exact[rel]:
            res.size_problems.append(f"{rel}: {st.st_size:,} bytes, expected {exact[rel]:,}")
        elif rel in ref and st.st_size != ref[rel]:
            res.reference_mismatch.append(
                f"{rel}: {st.st_size:,} bytes; the project's copy was {ref[rel]:,}")


def _count(manifest: Manifest, rule: Mapping[str, Any], root: Path, res: LayoutResult, deep: bool) -> None:
    expect = rule["expect"]
    regex = re.compile(rule["regex"]) if rule.get("regex") else None
    magic = bytes.fromhex(rule["magic"]) if (deep and rule.get("magic")) else None
    dir_tpl, glob_tpl = rule["dir"], rule["glob"]
    combined = dir_tpl + "\0" + glob_tpl
    for expanded, binding in manifest.expand(combined, rule.get("vars")):
        rel_dir, pattern = expanded.split("\0")
        if isinstance(expect, Mapping):
            key = next((binding[k] for k in binding if binding[k] in expect), None)
            n_exp = int(expect.get(key, 0)) if key is not None else 0
            if not n_exp:
                continue
        else:
            n_exp = int(expect)
        entries = [e for e in _list_files(root / rel_dir) if fnmatch.fnmatchcase(e.name, pattern)]
        if regex is not None:
            odd = [e.name for e in entries if not regex.search(e.name)]
            if odd:
                res.warnings.append(f"{rel_dir}: {len(odd)} file(s) with unexpected names, e.g. {odd[0]}")
            entries = [e for e in entries if regex.search(e.name)]
        n = len(entries)
        res.expected += n_exp
        res.present += min(n, n_exp)
        res.present_bytes += sum(e.stat().st_size for e in entries)
        if n < n_exp:
            res.add_missing(f"{rel_dir}/{pattern}: {n} of {n_exp} files", n_exp - n)
        elif n > n_exp:
            res.warnings.append(f"{rel_dir}/{pattern}: {n} files, expected {n_exp}")
        if magic is not None:
            bad = [e.name for e in entries if _read_magic(Path(e.path), len(magic)) != magic]
            if bad:
                res.size_problems.append(
                    f"{rel_dir}: {len(bad)} file(s) without the expected file signature, e.g. {bad[0]}")


def _one_per(manifest: Manifest, rule: Mapping[str, Any], root: Path, res: LayoutResult) -> None:
    rel_dir = rule["dir"]
    regex = re.compile(rule["regex"])
    group = rule["group"]
    values = manifest.resolve_values(rule["values"])
    entries = _list_files(root / rel_dir)
    by_value: dict[str, list[os.DirEntry]] = {}
    matched: set[str] = set()
    for e in entries:
        m = regex.match(e.name)
        if m:
            by_value.setdefault(m.group(group), []).append(e)
            matched.add(e.name)
    for v in values:
        res.expected += 1
        hits = by_value.get(v, [])
        if not hits:
            res.add_missing(f"{rel_dir}: no file for {group} {v}")
        elif len(hits) > 1:
            names = ", ".join(sorted(e.name for e in hits))
            res.layout_problems.append(
                f"{rel_dir}: {len(hits)} files for {group} {v} ({names}); keep exactly one")
        else:
            res.present += 1
            res.present_bytes += hits[0].stat().st_size
    if rule.get("warn_other"):
        other = [e.name for e in entries
                 if e.name not in matched and fnmatch.fnmatchcase(e.name, rule["warn_other"])]
        if other:
            res.warnings.append(
                f"{rel_dir}: {len(other)} other {rule['warn_other']} file(s) the reader would also read, "
                f"e.g. {other[0]}")


def _days(year: int) -> Iterable[_dt.date]:
    d = _dt.date(year, 1, 1)
    one = _dt.timedelta(days=1)
    while d.year == year:
        yield d
        d += one


def _daily(manifest: Manifest, rule: Mapping[str, Any], root: Path, res: LayoutResult, deep: bool) -> None:
    gaps = set(rule.get("known_gaps") or ())
    magic = bytes.fromhex(rule["magic"]) if (deep and rule.get("magic")) else None
    years = [int(y) for y in manifest.resolve_values(rule["years"])]
    tpl = rule["path"]
    var_spec = dict(rule.get("vars") or {})
    # Expand the non-date variables; {year} and {yyyymmdd} are filled per day.
    names = [k for k in var_spec]
    pools = [manifest.resolve_values(var_spec[k]) for k in names]
    for combo in itertools.product(*pools) if pools else [()]:
        binding = dict(zip(names, combo))
        for year in years:
            dir_rel = os.path.dirname(tpl.format(**binding, year=year, yyyymmdd="00000000"))
            present_names = {e.name: e for e in _list_files(root / dir_rel)}
            want = 0
            missing_here = []
            for day in _days(year):
                stamp = day.strftime("%Y%m%d")
                if stamp in gaps:
                    continue
                want += 1
                fname = os.path.basename(tpl.format(**binding, year=year, yyyymmdd=stamp))
                e = present_names.get(fname)
                if e is None:
                    missing_here.append(fname)
                    continue
                res.present += 1
                res.present_bytes += e.stat().st_size
                if magic is not None and _read_magic(Path(e.path), len(magic)) != magic:
                    res.size_problems.append(f"{dir_rel}/{fname}: not an HDF4 file (bad signature)")
            res.expected += want
            if missing_here:
                if len(missing_here) == want:
                    res.add_missing(f"{dir_rel}/: no files ({want} expected)", want)
                else:
                    res.add_missing(f"{dir_rel}/: {len(missing_here)} of {want} days missing, "
                                    f"e.g. {missing_here[0]}", len(missing_here))


def _nonempty(manifest: Manifest, rule: Mapping[str, Any], root: Path, res: LayoutResult) -> None:
    for rel, _ in manifest.expand(rule["path"], rule.get("vars")):
        res.expected += 1
        d = root / rel
        try:
            with os.scandir(d) as it:
                ok = any(not e.name.startswith(".") for e in it)
        except OSError:
            ok = False
        if ok:
            res.present += 1
        else:
            res.add_missing(f"{rel}/ (empty or absent)")
