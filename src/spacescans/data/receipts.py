"""Receipts: the record that an artifact was placed completely and correctly.

One JSON file per artifact under ``<data_dir>/.spacescans/receipts/``. It is
written last, after every file is in place, so its presence marks the
artifact as complete. It lists each placed file with its size and sha256, so
the tree can be re-verified after the archive itself is deleted.
"""
from __future__ import annotations

import getpass
import json
import os
import socket
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from spacescans.data.paths import receipts_dir, state_dir

RECEIPT_VERSION = 1


def receipt_path(root: Path, artifact: str) -> Path:
    return receipts_dir(root) / f"{artifact}.json"


def read_receipt(root: Path, artifact: str) -> dict[str, Any] | None:
    """The receipt, or None if absent or unreadable."""
    p = receipt_path(root, artifact)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("receipt_version") != RECEIPT_VERSION:
        return None
    if not isinstance(data.get("files"), list):
        return None
    return data


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=False)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def write_receipt(root: Path, artifact: Any, *, files: Iterable[tuple[str, int, str]],
                  source: str, origin: str, data_release: str,
                  skipped: Iterable[str] = ()) -> dict[str, Any]:
    """Write the receipt for ``artifact`` (a manifest Artifact)."""
    from spacescans import __version__

    data = {
        "receipt_version": RECEIPT_VERSION,
        "artifact": artifact.name,
        "kind": artifact.kind,
        "sha256": artifact.sha256,
        "bytes": artifact.bytes,
        "data_release": data_release,
        "origin": origin,            # "fetch" | "adopt"
        "source": source,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "spacescans_version": __version__,
        "files": [[rel, int(size), sha] for rel, size, sha in files],
        "skipped": sorted(skipped),
    }
    write_json_atomic(receipt_path(root, artifact.name), data)
    return data


def remove_receipt(root: Path, artifact: str) -> None:
    receipt_path(root, artifact).unlink(missing_ok=True)


def record_acknowledgement(root: Path, terms: Any) -> None:
    """Record, locally only, that the user accepted ``terms`` in this call."""
    path = state_dir(root) / "acknowledgements.json"
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            entries = []
    except (OSError, ValueError):
        entries = []
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - no user database
        user = ""
    entries.append({
        "terms_id": terms.id,
        "url": terms.url,
        "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "os_user": user,
        "host": socket.gethostname(),
    })
    write_json_atomic(path, entries)
