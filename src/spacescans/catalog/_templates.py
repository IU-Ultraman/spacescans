"""The 25 pipeline YAML templates the web runners render.

``templates/c3`` and ``templates/c4`` are byte-for-byte copies of
spacescans-web ``configs/{c3,c4}`` at web commit 3991c3b, minus the three
templates nothing references (``c3/bg_fl_leon_demo``,
``c3/noise_grid_conus_no_bg_geoid``, ``c4/noise_conus_no_bg_geoid``).

Their data paths are relative to the data root (``--data-dir``), in the web's
layout (``BG/C3/...``, ``Community_Organization_Density/C4/...``); the demo
paths inside them (``patient_file``, ``output.path``, C3 weight tables) are
always overwritten by the renderer. They are not the ``init-config`` starter
templates in ``spacescans/resources/configs/templates``, which use another
layout and stay as they are.
"""
from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # importlib.resources.abc is 3.11+; annotations only
    from importlib.resources.abc import Traversable

_PACKAGE = "spacescans.catalog"
_KINDS = ("c3", "c4")


def _root() -> Traversable:
    return resources.files(_PACKAGE).joinpath("templates")


def templates() -> list[str]:
    """Relative paths of every packaged template, e.g. ``"c3/bg_us_demo.yaml"``, sorted."""
    out: list[str] = []
    for kind in _KINDS:
        for entry in _root().joinpath(kind).iterdir():
            if entry.is_file() and entry.name.endswith(".yaml"):
                out.append(f"{kind}/{entry.name}")
    return sorted(out)


def _entry(relpath: str) -> Traversable:
    # Only the listed names: no path traversal, no non-template files.
    if relpath not in templates():
        raise KeyError(f"no packaged template {relpath!r}")
    kind, name = relpath.split("/", 1)
    return _root().joinpath(kind).joinpath(name)


def template_bytes(relpath: str) -> bytes:
    """The template file exactly as shipped."""
    return _entry(relpath).read_bytes()


def template_text(relpath: str) -> str:
    """The template file as text (UTF-8)."""
    return template_bytes(relpath).decode("utf-8")


def template(relpath: str) -> dict:
    """The template parsed with ``yaml.safe_load`` - what the web's renderers start from."""
    import yaml

    return yaml.safe_load(template_text(relpath))


def template_hashes() -> dict[str, str]:
    """``{relpath: sha256 hex}`` of every template, for a hash check against another copy."""
    return {rel: hashlib.sha256(template_bytes(rel)).hexdigest() for rel in templates()}


def templates_dir() -> Path:
    """The packaged templates directory on disk.

    Point ``SPACESCANS_CONFIG_TEMPLATES_DIR`` at it to make the web render from
    the package copy. Raises ``RuntimeError`` when the package is not installed
    as plain files (for example from a zip).
    """
    root = _root()
    if not isinstance(root, Path):
        raise RuntimeError(  # noqa: TRY004  (not a caller's type error)
            "spacescans is not installed as files; templates have no directory")
    return root
