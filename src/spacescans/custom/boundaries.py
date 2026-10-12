"""Which boundary layers a data directory can actually run a custom C3 on.

Offering a layer whose shapefiles are missing would let a user build a dataset
that can never run.

Ported from spacescans-web ``app/custom_exposomes.provisioned_boundaries``
(758-777) and ``app/variable_registry._unprovisioned`` (72-92); the data
directory is an argument instead of the web's settings.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from spacescans.custom.table import BOUNDARY_SPEC

# Which dataset dir under the data root must hold data for a boundary's C3 step
# to be runnable. Mirrors the provisioning gates of the shipped variables.
BOUNDARY_DATA_DIR = {
    "Tract": "TRACT",
    "BG": "BG",
    "ZCTA5": "ZCTA5",
    "County": "County",
}


def unprovisioned(root: str | os.PathLike) -> bool:
    """True when a dataset dir holds no data yet: absent, or only dotfiles
    (the committed ``.gitkeep`` skeleton, OS cruft like ``.DS_Store``).

    Anything that cannot be listed (a file where a dir belongs, a dir the
    process cannot enter) counts as provisioned, so the caller's own check runs
    and reports the missing artifact by path.
    """
    root = Path(root)
    if not root.exists():
        return True
    try:
        return not any(p for p in root.iterdir() if not p.name.startswith("."))
    except OSError:
        return False


def provisioned_boundaries(data_dir: str | os.PathLike) -> list[dict[str, Any]]:
    """Each boundary layer, its join column and key width, and whether
    ``data_dir`` has its C3 source data."""
    root = Path(os.path.expanduser(os.fspath(data_dir)))
    out = []
    for boundary, spec in BOUNDARY_SPEC.items():
        layer_dir = root / BOUNDARY_DATA_DIR[boundary]
        available = layer_dir.is_dir() and not unprovisioned(layer_dir)
        out.append({
            "boundary": boundary,
            "label": spec["label"],
            "join_col": spec["join_col"],
            "key_len": spec["key_len"],
            "available": available,
        })
    return out
