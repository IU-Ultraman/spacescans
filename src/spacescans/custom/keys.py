"""Variable keys and dataset ids of uploaded datasets.

A dataset id is 8 lowercase hex characters; its catalog key is ``custom_<id>``,
which satisfies the catalog's ``^[a-z][a-z0-9_]*$`` key pattern.

Ported from spacescans-web ``app/custom_exposomes.py`` (keys section). Where a
dataset is *stored*, and who owns it, stays in the web.
"""
from __future__ import annotations

import hashlib
import re

from spacescans.catalog.recipes import CUSTOM_EXPERIMENT

# The experiment every uploaded dataset belongs to, as the catalog records it.
EXPERIMENT_KEY = CUSTOM_EXPERIMENT
# Key under which a web task's config.json records the definitions it selected.
TASK_CUSTOM_KEY = "custom_variables"
KEY_PREFIX = "custom_"

_DATASET_ID = re.compile(r"[0-9a-f]{8}")


def variable_key(dataset_id: str) -> str:
    """``custom_<dataset_id>``."""
    return f"{KEY_PREFIX}{dataset_id}"


def dataset_id_from_key(key: str) -> str | None:
    """The dataset id of a custom key, or None when ``key`` is not one."""
    if not key.startswith(KEY_PREFIX):
        return None
    candidate = key[len(KEY_PREFIX):]
    return candidate if _DATASET_ID.fullmatch(candidate) else None


def is_custom_key(key: str) -> bool:
    return dataset_id_from_key(key) is not None


def is_dataset_id(value: str) -> bool:
    return isinstance(value, str) and _DATASET_ID.fullmatch(value) is not None


def derive_dataset_id(content: bytes, label: str, *, salt: str = "", attempt: int = 0) -> str:
    """A content + name hash, 8 hex characters.

    With ``salt=str(uid)`` and ``attempt=k`` this is exactly the web's k-th
    candidate (``custom_exposomes._derive_id``), which re-rolls ``attempt``
    until it finds a free library directory. Without a salt it is a stable id
    for a file and a name: the same upload under the same name gets the same
    key, so a client without a library (R) can recompute it.
    """
    base = hashlib.sha256(content + label.encode() + salt.encode()).hexdigest()
    return hashlib.sha256(f"{base}:{attempt}".encode()).hexdigest()[:8]
