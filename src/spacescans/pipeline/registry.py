# src/spacescans/pipeline/registry.py
"""Pattern and plugin registries with explicit init()."""
from __future__ import annotations
from typing import Callable
import importlib

_PATTERN_REGISTRY: dict[str, Callable] = {}
_READER_PLUGINS: dict[str, type] = {}
_PREP_PLUGINS: dict[str, Callable] = {}
_initialized = False


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
        raise KeyError(
            f"Unknown linkage pattern: {name}. Registered: {sorted(_PATTERN_REGISTRY)}"
        )
    return _PATTERN_REGISTRY[name]


def get_reader(name: str) -> type:
    if name not in _READER_PLUGINS:
        raise KeyError(
            f"Unknown reader plugin: {name}. Registered: {sorted(_READER_PLUGINS)}"
        )
    return _READER_PLUGINS[name]


def get_prep(name: str) -> Callable:
    if name not in _PREP_PLUGINS:
        raise KeyError(
            f"Unknown prep plugin: {name}. Registered: {sorted(_PREP_PLUGINS)}"
        )
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


def init() -> None:
    global _initialized
    if _initialized:
        return
    from spacescans._extras import MissingExtraError
    for mod in _BASE_MODULES:
        importlib.import_module(mod)
    for mod in _OPTIONAL_MODULES:
        try:
            importlib.import_module(mod)
        except (MissingExtraError, ModuleNotFoundError):
            pass  # Extra not installed or module file doesn't exist yet; init() still succeeds.
    _initialized = True
