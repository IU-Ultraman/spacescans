"""Data-directory resolution and the tool's own state folder.

The data directory resolves like the pipeline's ``--data-dir``: an explicit
argument first, then ``$SPACESCANS_DATA_DIR`` (``config_resolution.py``).
There is deliberately no implicit default: a 200 GB download must never land
somewhere the user did not choose.

Everything this module writes besides the data itself lives under
``<data_dir>/.spacescans/`` (downloads, receipts, lock, acknowledgements).
The leading dot keeps it out of the web app's dataset-folder checks.
"""
from __future__ import annotations

import os
from pathlib import Path

from spacescans.data.errors import UsageError

ENV_VAR = "SPACESCANS_DATA_DIR"
STATE_DIRNAME = ".spacescans"

# Folders whose contents a sync client may evict or rewrite behind our back
# (iCloud "Optimize Mac Storage", OneDrive/Dropbox/Box/Google Drive Files
# On-Demand). Data, caches and run folders should not live there.
_CLOUD_MARKERS = (
    "Library/Mobile Documents",
    "Library/CloudStorage",
)
_CLOUD_NAMES = ("OneDrive", "Dropbox", "Box", "Google Drive", "iCloud Drive")


def resolve_data_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Return the data directory: ``explicit``, else ``$SPACESCANS_DATA_DIR``.

    The path is expanded (``~``) and made absolute. It need not exist yet.
    Raises :class:`UsageError` when neither is set.
    """
    raw = os.fspath(explicit) if explicit is not None else os.environ.get(ENV_VAR)
    if not raw:
        raise UsageError(
            "no data directory given: pass data_dir=... (CLI: --data-dir DIR) "
            f"or set {ENV_VAR}",
            hint="spacescans data status --data-dir ~/ss-data",
        )
    return Path(raw).expanduser().resolve()


def state_dir(root: Path) -> Path:
    return root / STATE_DIRNAME


def downloads_dir(root: Path) -> Path:
    return state_dir(root) / "downloads"


def receipts_dir(root: Path) -> Path:
    return state_dir(root) / "receipts"


def archives_dir(root: Path) -> Path:
    return state_dir(root) / "archives"


def cloud_sync_warning(path: Path) -> str | None:
    """A warning when ``path`` sits in a cloud-synced folder, else None."""
    p = str(path)
    for marker in _CLOUD_MARKERS:
        if f"/{marker}/" in p + "/":
            return (f"{path} is inside {marker}: a sync client may evict or "
                    "rewrite files there; keep the data directory on a local or external disk")
    parts = path.parts
    for name in _CLOUD_NAMES:
        if any(part == name or part.startswith(name + " -") for part in parts):
            return (f"{path} looks like a {name} folder: a sync client may evict or "
                    "rewrite files there; keep the data directory on a local or external disk")
    return None
