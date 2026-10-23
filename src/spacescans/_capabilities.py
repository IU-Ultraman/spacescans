"""What this spacescans install is and what it can run.

``capabilities()`` replaces the probes clients used to need (reading module
source for the word "output_grouping", poking at private registries). It is
cheap enough to call once per client session.

Module level imports the standard library only: the R package's wheel build
script imports ``fingerprint_wheel`` from a bare source tree.
"""
from __future__ import annotations

import functools
import hashlib
import importlib
import importlib.metadata
import importlib.util
import os
import platform
import sys
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# The extras declared in pyproject.toml and the modules each one must provide.
EXTRAS: dict[str, tuple[str, ...]] = {
    "geo": ("geopandas", "rasterio", "shapely", "exactextract"),
    "rda": ("pyreadr",),
    "hdf4": ("pyhdf",),
    "nc": ("xarray", "netCDF4"),
}

# Behaviour flags clients may test instead of version numbers.
FLAGS: dict[str, bool] = {
    "episode_grouping": True,        # time.output_grouping = "episode" (0.2.x)
    "config_from_dict": True,        # load_config_dict / Pipeline.from_dict
    "engine_options": True,          # engine.threads / memory_limit / temp_directory
    "unavailable_pattern_error": True,  # registry raises UnavailablePatternError
    # output_level ("participant" | "episode") in orchestrate.link, render_config, RunContext,
    # the merge functions and the bridge's link; participant means in every C4 pattern (0.3)
    "output_level": True,
    "fara_missing_skip": True,       # FARA leaves tracts without a value out of the buffer mean (0.3)
}

_DISTRIBUTIONS = (
    "spacescans", "pydantic", "pyyaml", "pandas", "numpy", "duckdb", "pyarrow",
    "mercantile", "geopandas", "rasterio", "shapely", "exactextract", "pyogrio", "pyproj",
    "pyreadr", "pyhdf", "xarray", "netCDF4",
)


def fingerprint_entries(entries: Iterable[tuple[str, bytes]]) -> str:
    """Hash (relative path, content) pairs into ``"sha256:<hex>"``.

    Paths are POSIX, relative to the ``spacescans`` package directory. Order
    does not matter: entries are sorted by path first.
    """
    h = hashlib.sha256()
    for rel, data in sorted(entries, key=lambda e: e[0]):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(data).digest())
        h.update(b"\n")
    return "sha256:" + h.hexdigest()


def _package_root() -> Path:
    return Path(__file__).resolve().parent


@functools.cache
def code_fingerprint(root: str | os.PathLike | None = None) -> str:
    """Fingerprint of every ``.py`` file under the installed ``spacescans`` package.

    Detects editable or stale installs whose metadata version does not match
    the code that actually runs.
    """
    base = Path(root) if root is not None else _package_root()
    entries = []
    for path in base.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        entries.append((path.relative_to(base).as_posix(), path.read_bytes()))
    return fingerprint_entries(entries)


def fingerprint_wheel(wheel: str | os.PathLike) -> str:
    """The code fingerprint the given wheel will have once installed."""
    entries = []
    with zipfile.ZipFile(wheel) as zf:
        for name in zf.namelist():
            if name.startswith("spacescans/") and name.endswith(".py"):
                entries.append((name[len("spacescans/"):], zf.read(name)))
    return fingerprint_entries(entries)


def extras_status() -> dict[str, dict[str, Any]]:
    """For each extra: whether all its modules are importable, and which are missing."""
    out = {}
    for extra, modules in EXTRAS.items():
        missing = [m for m in modules if importlib.util.find_spec(m) is None]
        out[extra] = {"available": not missing, "missing": missing}
    return out


def package_versions() -> dict[str, str | None]:
    """Installed versions of spacescans and its (optional) dependencies, None if absent."""
    out: dict[str, str | None] = {}
    for dist in _DISTRIBUTIONS:
        try:
            out[dist] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            out[dist] = None
    return out


def native_versions() -> dict[str, str | None]:
    """GDAL / GEOS / PROJ versions as the Python bindings see them (None if unavailable)."""
    probes = {
        "gdal": ("rasterio", "__gdal_version__"),
        "gdal_pyogrio": ("pyogrio", "__gdal_version_string__"),
        "geos": ("shapely", "geos_version_string"),
        "proj": ("pyproj", "proj_version_str"),
    }
    out: dict[str, str | None] = {}
    for key, (module, attr) in probes.items():
        if importlib.util.find_spec(module) is None:
            out[key] = None
            continue
        try:
            out[key] = str(getattr(importlib.import_module(module), attr))
        except Exception as e:  # a broken binary wheel must not break capabilities()
            out[key] = f"error: {type(e).__name__}: {e}"
    return out


def platform_info() -> dict[str, Any]:
    mac = platform.mac_ver()[0] or None
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "macos": mac,
        "platform": platform.platform(),
    }


def python_info() -> dict[str, Any]:
    return {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "executable": sys.executable,
        "prefix": sys.prefix,
    }


def catalog_fingerprint(hashes: dict[str, str] | None = None) -> str:
    """``"sha256:<hex>"`` over the packaged catalog templates.

    Built from ``catalog.template_hashes()`` ({relpath: sha256 hex}) the same
    way as :func:`fingerprint_entries` builds the code fingerprint, so it
    equals ``fingerprint_entries((rel, template_bytes(rel)) ...)``. Two
    installs with the same value render the same pipeline YAMLs.
    """
    if hashes is None:
        from spacescans import catalog
        hashes = catalog.template_hashes()
    h = hashlib.sha256()
    for rel in sorted(hashes):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(bytes.fromhex(hashes[rel]))
        h.update(b"\n")
    return "sha256:" + h.hexdigest()


def catalog_info() -> dict[str, Any]:
    """The catalog this install ships: template fingerprint and counts, file hashes."""
    from importlib import resources

    from spacescans import catalog

    def file_sha(name: str) -> str:
        return hashlib.sha256(resources.files("spacescans.catalog").joinpath(name).read_bytes()).hexdigest()

    hashes = catalog.template_hashes()
    return {
        "hash": catalog_fingerprint(hashes),
        "templates": len(hashes),
        "variables": len(catalog.variables()),
        "experiments": catalog.experiments(),
        "variables_sha256": file_sha("variables.json"),
        "features_sha256": file_sha("features.json"),
    }


def data_release() -> str | None:
    """The data release the packaged data manifest describes (``manifest.json``)."""
    from spacescans.data.manifest import load_manifest

    return load_manifest().data_release


def _guarded(fn) -> Any:
    try:
        return fn()
    except Exception as e:  # a broken catalog or manifest must not break capabilities()
        return {"error": f"{type(e).__name__}: {e}"}


def capabilities() -> dict[str, Any]:
    """Version, code fingerprint, extras, registered patterns, native libraries,
    the catalog fingerprint and the data release."""
    import spacescans
    from spacescans.pipeline import registry

    release = _guarded(data_release)
    return {
        "spacescans": {
            "version": spacescans.__version__,
            "code_fp": code_fingerprint(),
            "path": str(_package_root()),
        },
        "python": python_info(),
        "platform": platform_info(),
        "extras": extras_status(),
        "registry": registry.available(),
        "flags": dict(FLAGS),
        "versions": package_versions(),
        "native": native_versions(),
        "catalog": _guarded(catalog_info),
        "data_release": release if isinstance(release, str) else None,
    }
