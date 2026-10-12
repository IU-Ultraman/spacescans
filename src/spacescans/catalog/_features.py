"""The per-column feature dictionary that accompanies every result.

Ported from spacescans-web ``backend/app/feature_dictionary.py`` (web commit
3991c3b). ``features.json`` is a byte-for-byte copy of the web's
``backend/app/data/feature_dictionary.json``, which the web's
``scripts/extend_ontology.py`` generates; edit it there and copy it over until
that script writes into this package.
"""
from __future__ import annotations

import csv
import json
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path

_PACKAGE = "spacescans.catalog"
FEATURES_FILE = "features.json"
FEATURE_FIELDNAMES = ("feature_name", "short_description", "detailed_description")


@dataclass(frozen=True)
class Feature:
    """One exposure column: its short label and its one-sentence definition."""

    name: str
    label: str
    definition: str


@lru_cache(maxsize=1)
def _entries() -> OrderedDict[str, dict]:
    text = resources.files(_PACKAGE).joinpath(FEATURES_FILE).read_text(encoding="utf-8")
    return json.loads(text, object_pairs_hook=OrderedDict)["features"]


def features() -> OrderedDict[str, Feature]:
    """Every documented column, in file order."""
    return OrderedDict(
        (name, Feature(name=name, label=entry["label"], definition=entry["definition"]))
        for name, entry in _entries().items()
    )


def feature_rows(columns: Iterable[str]) -> list[dict[str, str]]:
    """Dictionary rows for ``columns``, in the order given (the web's ``rows_for``).

    Columns with no entry are skipped rather than emitted blank: callers pass a
    result's full header, which includes the cohort's own columns.
    """
    entries: Mapping[str, dict] = _entries()
    return [
        {
            "feature_name": col,
            "short_description": entries[col]["label"],
            "detailed_description": entries[col]["definition"],
        }
        for col in columns
        if col in entries
    ]


def write_feature_csv(columns: Iterable[str], path: str | Path) -> Path:
    """Write the dictionary for ``columns`` (the web's ``write_csv``, same bytes).

    Always writes the header, even when no column matched, so downstream code
    can read the file unconditionally.
    """
    out_path = Path(path)
    rows = feature_rows(columns)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FEATURE_FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return out_path
