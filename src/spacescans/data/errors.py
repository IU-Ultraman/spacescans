"""Errors raised by spacescans.data.

Every error carries a ``kind`` that matches the bridge protocol's error
classes (design section 3.5), a one-line ``hint`` naming the next command, and
JSON-serialisable ``details``. ``to_dict()`` gives the bridge's error shape.
"""
from __future__ import annotations

from typing import Any


class DataError(Exception):
    """Base class. ``kind`` is the bridge error class."""

    kind = "internal"

    def __init__(self, message: str, *, hint: str | None = None,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "hint": self.hint,
            "details": self.details,
            "type": type(self).__name__,
        }


class ManifestError(DataError):
    """The data manifest is malformed or inconsistent (a packaging bug)."""

    kind = "internal"


class UsageError(DataError):
    """Bad arguments: unknown variable, dataset or terms id; no data dir."""

    kind = "config"


class DataMissingError(DataError):
    """Data needed for the request is not in the data directory."""

    kind = "data_missing"


class NotOnMirrorError(DataMissingError):
    """The artifact is not (yet) downloadable from the configured mirror."""


class DataCorruptError(DataError):
    """A checksum or size does not match, or a file is truncated."""

    kind = "data_corrupt"


class UnsafeArchiveError(DataCorruptError):
    """An archive member failed validation (traversal, link, unknown root)."""


class TermsRequiredError(DataError):
    """A provider download needs the user's explicit acceptance of terms."""

    kind = "terms_required"


class NetworkError(DataError):
    """A download failed after all retries. Re-running resumes it."""

    kind = "network"


class DiskSpaceError(DataError):
    """Not enough free space in the data directory for the plan."""

    kind = "disk_space"


class LockedError(DataError):
    """Another process holds the data directory's download lock."""

    kind = "locked"
