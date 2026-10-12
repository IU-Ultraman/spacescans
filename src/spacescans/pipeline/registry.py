# src/spacescans/pipeline/registry.py
"""Pattern and plugin registries with explicit init()."""
from __future__ import annotations
from typing import Any, Callable
import importlib

from spacescans._extras import INSTALL_DOCS_URL, MissingExtraError, extra_for_module

_PATTERN_REGISTRY: dict[str, Callable] = {}
_READER_PLUGINS: dict[str, type] = {}
_PREP_PLUGINS: dict[str, Callable] = {}
_initialized = False

# Optional module -> the exception that stopped it importing during init().
_UNAVAILABLE: dict[str, BaseException] = {}


class UnavailablePatternError(MissingExtraError, KeyError):
    """A known linkage pattern or plugin whose module could not be imported.

    Raised by get_pattern()/get_reader()/get_prep() instead of a bare KeyError
    when the requested name belongs to an optional module that init() could not
    import (usually a missing pip extra). It subclasses KeyError, so existing
    ``except KeyError`` handlers keep working, and MissingExtraError, so callers
    can tell "install an extra" apart from a typo. The message still starts with
    "Unknown linkage pattern" / "Unknown reader plugin" / "Unknown prep plugin".
    """


def register_pattern(name: str):
    def decorator(fn):
        if name in _PATTERN_REGISTRY:
            raise ValueError(f"Pattern already registered: {name}")
        _PATTERN_REGISTRY[name] = fn
        return fn
    return decorator


def register_reader(name: str):
    def decorator(cls):
        if name in _READER_PLUGINS:
            raise ValueError(f"Reader plugin already registered: {name}")
        _READER_PLUGINS[name] = cls
        return cls
    return decorator


def register_prep(name: str):
    def decorator(fn):
        if name in _PREP_PLUGINS:
            raise ValueError(f"Prep plugin already registered: {name}")
        _PREP_PLUGINS[name] = fn
        return fn
    return decorator


def get_pattern(name: str) -> Callable:
    if name not in _PATTERN_REGISTRY:
        _raise_unknown("pattern", f"Unknown linkage pattern: {name}. Registered: {sorted(_PATTERN_REGISTRY)}", name)
    return _PATTERN_REGISTRY[name]


def get_reader(name: str) -> type:
    if name not in _READER_PLUGINS:
        _raise_unknown("reader", f"Unknown reader plugin: {name}. Registered: {sorted(_READER_PLUGINS)}", name)
    return _READER_PLUGINS[name]


def get_prep(name: str) -> Callable:
    if name not in _PREP_PLUGINS:
        _raise_unknown("prep", f"Unknown prep plugin: {name}. Registered: {sorted(_PREP_PLUGINS)}", name)
    return _PREP_PLUGINS[name]


# Module groups — each tuple is (modules to try, extras name for error context).
# Base modules: always loaded.
_BASE_MODULES = [
    "spacescans.linkage.yearly_areal_linkage",
    "spacescans.linkage.yearly_areal_bg_vintage_linkage",
    "spacescans.linkage.static_areal_linkage",
    "spacescans.linkage.cbp_fallback_linkage",
    "spacescans.linkage.faqsd_linkage",
    "spacescans.linkage.precomputed_areal_linkage",
    "spacescans.linkage.precomputed_static_linkage",
    "spacescans.plugins.readers.faqsd",
]
# Optional modules: try-import, swallow MissingExtraError so init() always succeeds.
_OPTIONAL_MODULES = [
    "spacescans.linkage.boundary_overlap_linkage",
    "spacescans.linkage.boundary_overlap_fast_linkage",
    "spacescans.linkage.grid_weights_linkage",
    "spacescans.linkage.gridded_linkage",
    "spacescans.linkage.proximity_linkage",
    "spacescans.linkage.nhd_proximity_linkage",
    "spacescans.linkage.tiger_proximity_linkage",
    "spacescans.linkage.acag_linkage",
    "spacescans.linkage.fara_linkage",
    "spacescans.plugins.readers.acag",
    "spacescans.plugins.readers.temis",
    "spacescans.plugins.readers.nhd",
    "spacescans.plugins.readers.noise",
    "spacescans.plugins.readers.prism",
    "spacescans.plugins.readers.tiger_roads",
    "spacescans.plugins.readers.vnl",
    "spacescans.plugins.readers.modis_ndvi",
    "spacescans.plugins.readers.modis_part1",
    "spacescans.plugins.readers.prism_part1",
    "spacescans.plugins.prep.prism",
    "spacescans.plugins.prep.modis",
    "spacescans.plugins.prep.tiger",
]

# What each optional module registers, as (kind, name). It lets get_pattern()
# explain a name that is missing because its module failed to import.
# tests/test_registry_available.py checks it against the decorators.
_OPTIONAL_PROVIDES: dict[str, tuple[str, str]] = {
    "spacescans.linkage.boundary_overlap_linkage": ("pattern", "boundary_overlap"),
    "spacescans.linkage.boundary_overlap_fast_linkage": ("pattern", "boundary_overlap_fast"),
    "spacescans.linkage.grid_weights_linkage": ("pattern", "grid_weights"),
    "spacescans.linkage.gridded_linkage": ("pattern", "gridded"),
    "spacescans.linkage.proximity_linkage": ("pattern", "proximity"),
    "spacescans.linkage.nhd_proximity_linkage": ("pattern", "nhd_proximity"),
    "spacescans.linkage.tiger_proximity_linkage": ("pattern", "tiger_proximity"),
    "spacescans.linkage.acag_linkage": ("pattern", "acag_multi"),
    "spacescans.linkage.fara_linkage": ("pattern", "fara_tract"),
    "spacescans.plugins.readers.acag": ("reader", "acag"),
    "spacescans.plugins.readers.temis": ("reader", "temis"),
    "spacescans.plugins.readers.nhd": ("reader", "nhd"),
    "spacescans.plugins.readers.noise": ("reader", "noise"),
    "spacescans.plugins.readers.prism": ("reader", "prism"),
    "spacescans.plugins.readers.tiger_roads": ("reader", "tiger_roads"),
    "spacescans.plugins.readers.vnl": ("reader", "vnl"),
    "spacescans.plugins.readers.modis_ndvi": ("reader", "modis_ndvi"),
    "spacescans.plugins.readers.modis_part1": ("reader", "modis_part1"),
    "spacescans.plugins.readers.prism_part1": ("reader", "prism_part1"),
    "spacescans.plugins.prep.prism": ("prep", "prism"),
    "spacescans.plugins.prep.modis": ("prep", "modis"),
    "spacescans.plugins.prep.tiger": ("prep", "tiger"),
}


def init() -> None:
    global _initialized
    if _initialized:
        return
    for mod in _BASE_MODULES:
        importlib.import_module(mod)
    for mod in _OPTIONAL_MODULES:
        try:
            importlib.import_module(mod)
        except (MissingExtraError, ModuleNotFoundError) as e:
            # Extra not installed or module file doesn't exist yet; init() still
            # succeeds. Remember why, so a later lookup can say so.
            _UNAVAILABLE[mod] = e
    _initialized = True


def _failure_info(err: BaseException) -> dict[str, Any]:
    extra = getattr(err, "extra", None)
    missing = list(getattr(err, "missing", ()) or ())
    if not missing and isinstance(err, ModuleNotFoundError) and err.name:
        missing = [err.name]
    if extra is None:
        extra = next((x for x in map(extra_for_module, missing) if x), None)
    first_line = str(err).strip().splitlines()[0] if str(err).strip() else type(err).__name__
    return {"error": f"{type(err).__name__}: {first_line}", "extra": extra, "missing": missing}


def _raise_unknown(kind: str, message: str, name: str) -> None:
    for mod, (k, provided) in _OPTIONAL_PROVIDES.items():
        if k == kind and provided == name and mod in _UNAVAILABLE:
            err = _UNAVAILABLE[mod]
            info = _failure_info(err)
            how = (f"install it with `pip install 'spacescans-pipeline[{info['extra']}]'`"
                   if info["extra"] else "see " + INSTALL_DOCS_URL)
            raise UnavailablePatternError(
                f"{message}. '{name}' is provided by {mod}, which could not be imported "
                f"({info['error']}); {how}.",
                extra=info["extra"], missing=info["missing"],
            ) from err
    raise KeyError(message)


def available() -> dict[str, Any]:
    """What this install can run: registered names and why optional modules failed.

    Calls init() first. ``unavailable`` maps each optional module that could not
    be imported to ``{"error", "extra", "missing", "provides"}``.
    """
    init()
    return {
        "patterns": sorted(_PATTERN_REGISTRY),
        "readers": sorted(_READER_PLUGINS),
        "preps": sorted(_PREP_PLUGINS),
        "unavailable": {
            mod: {**_failure_info(err), "provides": list(_OPTIONAL_PROVIDES.get(mod, ()))}
            for mod, err in sorted(_UNAVAILABLE.items())
        },
    }
