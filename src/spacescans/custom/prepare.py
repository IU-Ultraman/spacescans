"""Preparing registered datasets, and turning a record into a linkable definition.

:func:`prepare` is the kind-specific half of :func:`spacescans.custom.register`:
it validates the input files and writes the prepared files into a work
directory (``prepare_table``, ``prepare_raster``, ``features``).
:func:`definition_of` is the other direction: a stored record becomes the
definition dict the steps read (a superset of a catalog entry, like
``define_polygon`` / ``define_raster`` output).

The variable helpers here are shared by the three kinds: a variable is
``{"name", "unit", "description"}``; its name becomes a result column and a
SQL identifier in the steps, so it must start with a letter and use only
letters, digits and underscores.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from spacescans.custom.registry import DatasetError

if TYPE_CHECKING:  # pragma: no cover
    from spacescans.custom.registry import DatasetRecord, Input

#: Most linked columns one dataset may have (the upload path's limit).
MAX_VARIABLES = 40
MAX_VARIABLE_NAME = 60
MAX_UNIT = 50
MAX_VARIABLE_DESCRIPTION = 400
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
#: Names the steps' tables or the result already use.
RESERVED_NAMES = frozenset({
    "pid", "patid", "episode_id", "geoid", "geo_key", "grid_id", "weight", "year", "date",
    "start_date", "end_date", "period_id", "overlap_days", "start", "end", "long", "lat",
    "longitude", "latitude", "startdate", "enddate", "value_aw",
})


@dataclass
class Prepared:
    """What a kind's preparation produced in the work directory.

    ``key``: the record's ``key``; ``variables``: ``[{name, unit,
    description}]``; ``files``: prepared files relative to the dataset
    directory; ``details``: kind-specific facts (no data values; includes
    ``prepared_sha256``); ``originals``: ``[{name, sha256, bytes, path}]`` in
    checksum order; ``description``: the default description.
    """

    key: dict | None
    variables: list[dict]
    files: list[str]
    details: dict
    originals: list[dict]
    description: str
    extras: dict = field(default_factory=dict)


def prepare(kind: str, inputs: Sequence[Input], work: Path, *, name: str, temporal: str,
            key: Any, variables: Any) -> Prepared:
    """Validate ``inputs`` and write the prepared files of a ``kind`` dataset to ``work``."""
    if kind == "polygon_table":
        from spacescans.custom.prepare_table import prepare_table
        return prepare_table(inputs, work, name=name, temporal=temporal, key=key,
                             variables=variables)
    if kind == "raster":
        from spacescans.custom.prepare_raster import prepare_raster
        return prepare_raster(inputs, work, name=name, temporal=temporal, key=key,
                              variables=variables)
    if kind == "features":
        from spacescans.custom.features import prepare_features
        return prepare_features(inputs, work, name=name, temporal=temporal, key=key,
                                variables=variables)
    raise DatasetError(f"unknown kind {kind!r}")  # pragma: no cover - checked by register


def definition_of(record: DatasetRecord) -> dict[str, Any]:
    """The linkable definition of a stored record (see :meth:`DatasetRecord.definition`)."""
    if record.root is None:
        raise DatasetError(f"dataset {record.name!r} has no root; read it with get_dataset()")
    if record.kind == "polygon_table":
        from spacescans.custom.prepare_table import table_definition
        return table_definition(record)
    if record.kind == "raster":
        from spacescans.custom.prepare_raster import raster_definition
        return raster_definition(record)
    if record.kind == "features":
        from spacescans.custom.features import features_definition
        return features_definition(record)
    raise DatasetError(f"dataset {record.name!r} has an unknown kind {record.kind!r}")


# ---------------------------------------------------------------------------
# variables
# ---------------------------------------------------------------------------

def slug(text: str, *, fallback: str = "value") -> str:
    """A column name from free text: lowercase letters, digits and underscores."""
    out = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if not out:
        out = fallback
    if not out[0].isalpha():
        out = f"v_{out}"
    out = out[:MAX_VARIABLE_NAME].rstrip("_")
    if out in RESERVED_NAMES:
        out = f"{out}_value"
    return out


def check_variable_name(name: Any) -> str:
    """A variable name: an identifier of at most 60 characters, not a reserved column."""
    if not isinstance(name, str) or not name.strip():
        raise DatasetError("each variable needs a name")
    name = name.strip()
    if not _IDENTIFIER.fullmatch(name):
        raise DatasetError(f"the variable name {name!r} must start with a letter and use only "
                           "letters, digits and underscores (it becomes a result column)")
    if len(name) > MAX_VARIABLE_NAME:
        raise DatasetError(f"the variable name {name!r} is longer than {MAX_VARIABLE_NAME} "
                           "characters")
    if name.lower() in RESERVED_NAMES:
        raise DatasetError(f"the variable name {name!r} is used by the linkage tables or the "
                           "result; choose another")
    return name


def check_variable_text(unit: Any, description: Any) -> tuple[str | None, str | None]:
    """A variable's unit (at most 50 characters) and description (at most 400)."""
    out = []
    for what, value, limit in (("unit", unit, MAX_UNIT),
                               ("description", description, MAX_VARIABLE_DESCRIPTION)):
        if value is None:
            out.append(None)
            continue
        if not isinstance(value, str):
            raise DatasetError(f"a variable's {what} must be a string")
        value = value.strip()
        if len(value) > limit:
            raise DatasetError(f"a variable's {what} is longer than {limit} characters")
        out.append(value or None)
    return out[0], out[1]


def normalise_variables(variables: Any, *, extra: Sequence[str] = ()) -> list[dict] | None:
    """``variables`` as a list of ``{name, unit, description, <extra>...}``; None stays None.

    Accepts a name, a list of names, or a list of mappings. Names are checked
    (:func:`check_variable_name`) and must differ ignoring case.
    """
    if variables is None:
        return None
    if isinstance(variables, (str, Mapping)):
        variables = [variables]
    if not isinstance(variables, Sequence) or not variables:
        raise DatasetError("variables must be a non-empty list of names or of "
                           "{name, unit, description} mappings")
    if len(variables) > MAX_VARIABLES:
        raise DatasetError(f"{len(variables)} variables exceed the limit of {MAX_VARIABLES}")
    out: list[dict] = []
    allowed = {"name", "unit", "description", *extra}
    for item in variables:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, Mapping):
            raise DatasetError("each variable is a name or a {name, unit, description} mapping")
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise DatasetError(f"unknown variable field(s): {', '.join(map(str, unknown))} "
                               f"(known: {', '.join(sorted(allowed))})")
        entry: dict[str, Any] = {"name": check_variable_name(item.get("name"))}
        entry["unit"], entry["description"] = check_variable_text(item.get("unit"),
                                                                  item.get("description"))
        for key in extra:
            if key in item:
                entry[key] = item[key]
        out.append(entry)
    lowered = [v["name"].lower() for v in out]
    dupes = sorted({v["name"] for v in out if lowered.count(v["name"].lower()) > 1})
    if dupes:
        raise DatasetError(f"variable name(s) used twice: {', '.join(dupes)} (compared "
                           "ignoring case)")
    return out


def public_variables(variables: Sequence[Mapping[str, Any]]) -> list[dict]:
    """The record's form of the variables: ``{name, unit, description}`` only."""
    return [{"name": v["name"], "unit": v.get("unit"), "description": v.get("description")}
            for v in variables]


def labels_and_units(record: DatasetRecord) -> tuple[dict[str, str], dict[str, str]]:
    """A definition's ``value_labels`` (from descriptions) and ``value_units``."""
    labels = {v["name"]: v["description"] for v in record.variables if v.get("description")}
    units = {v["name"]: v["unit"] for v in record.variables if v.get("unit")}
    return labels, units


def common_definition(record: DatasetRecord, *, geometry: str, boundary: str,
                      spatial_method: str, coverage_years: Sequence[int]) -> dict[str, Any]:
    """The fields every registered dataset's definition shares, in definition order."""
    from spacescans.custom.definition import MAX_DESCRIPTION
    from spacescans.custom.keys import EXPERIMENT_KEY
    from spacescans.custom.units import summarise_units

    cols = record.value_cols
    labels, units = labels_and_units(record)
    originals = record.details.get("originals") or []
    filenames = ", ".join(str(o.get("name")) for o in originals)
    return {
        # --- catalog-shaped ---
        "label": record.name,
        "description": (record.description or f"Registered dataset {record.name}.")[
            :MAX_DESCRIPTION],
        "boundary": boundary,
        "spatial_method": spatial_method,
        "coverage_years": [int(coverage_years[0]), int(coverage_years[1])],
        "coverage_region": "CONUS",
        "experiment": EXPERIMENT_KEY,
        "data_source": (record.source or f"Registered by user: {filenames}")[:200],
        "temporal": record.temporal,
        "variable_type": "continuous",
        "display_unit": summarise_units(cols, units),
        "value_cols": list(cols),
        # --- private ---
        "geometry": geometry,
        "dataset_id": record.dataset_id,
        "variable_key": record.variable_key,
        "owner_uid": record.owner,
        "value_labels": labels,
        "value_units": units,
        "registered": {"id": record.id, "name": record.name, "version": record.version,
                       "root": str(record.root), "checksum": record.checksum},
        "uploaded_filename": filenames[:200] or record.name,
        "created_at": record.created_utc,
    }
