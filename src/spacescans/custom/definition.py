"""Dataset definitions: what an uploaded dataset becomes in the catalog.

A *definition* is the dict the web stores as a library manifest and snapshots
into a task's ``config.json['custom_variables']``, and the dict an R user gets
back from ``ss_custom_polygon()`` / ``ss_custom_raster()`` and passes to
``ss_link(custom = ...)``. It is a documented SUPERSET of a catalog entry: the
catalog-shaped fields (:data:`CATALOG_FIELDS`) render like a shipped variable,
and the private fields (join/key/year columns, file paths, grid, bands) drive
the C3/C4 steps. :func:`catalog_entry` extracts the schema-valid subset.

:func:`define_polygon` and :func:`define_raster` validate and build a
definition and touch no storage. They are ``custom_exposomes.create`` and
``create_raster`` minus the user id and the library directory: the caller says
where the files live (``values_path`` / ``paths``) and, if it keeps a library,
which ``dataset_id`` and ``owner_uid`` to record. Called that way, the result
is the web's manifest field for field, in the same key order.

Ported from spacescans-web ``app/custom_exposomes.py`` (definition parts).
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spacescans.custom.errors import CustomExposomeError, RasterError
from spacescans.custom.keys import EXPERIMENT_KEY, derive_dataset_id, variable_key
from spacescans.custom.table import (
    BOUNDARY_SPEC,
    MAX_VALUE_COLS,
    Source,
    read_source,
    validate_csv,
)
from spacescans.custom.units import selected_labels, selected_units, summarise_units

# The fields the catalog schema allows. A definition is a superset of these:
# the runner needs join_col, values_path and friends, and the schema sets
# additionalProperties:false, so a definition deliberately does not validate
# against it. catalog_entry() extracts the valid subset.
CATALOG_FIELDS = (
    "label", "description", "boundary", "spatial_method", "coverage_years",
    "coverage_region", "experiment", "ontology_id", "data_source", "temporal",
    "variable_type", "display_unit", "value_cols",
)

MAX_LABEL = 80
MAX_DESCRIPTION = 400
MAX_RASTER_YEARS = 30
# A dataset with no year of its own claims the window the shipped variables use.
STATIC_COVERAGE_YEARS = (2013, 2019)

_COLUMN_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
# Columns the runner already uses in the value table (grid_id, year) or in
# result.csv (pid, episode_id; PATID and geoid before the merge renames them).
RESERVED_RASTER_COLS = frozenset({"grid_id", "year", "pid", "episode_id", "patid", "geoid"})


def catalog_entry(definition: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a definition that satisfies the shipped catalog schema."""
    return {k: definition[k] for k in CATALOG_FIELDS if k in definition}


def is_raster(definition: Mapping[str, Any]) -> bool:
    return definition.get("geometry") == "raster"


def feature_rows(definition: Mapping[str, Any], columns: Sequence[str] | None = None, *,
                 names: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    """Feature-dictionary rows of a custom dataset's value columns, from the user's definition.

    The catalog's dictionary (``catalog.feature_rows``) describes the shipped
    columns by name; a custom column must not borrow those descriptions (a
    raster whose column is called ``value`` is not VIIRS radiance). So each
    row comes from the definition (phase-C decision 3): ``short_description``
    is the column's label in ``value_labels``, else the dataset label (with the
    column name added when the dataset has several columns);
    ``detailed_description`` is the dataset description followed by the
    column's unit, when one was given, as ``Unit: <unit>.``.

    ``columns`` restricts the rows to those value columns (default: all, in the
    definition's order); ``names`` maps a column to the result column it
    appears under (a renamed clash), which becomes the row's ``feature_name``.
    The rows use the dictionary's field names (``feature_name``,
    ``short_description``, ``detailed_description``).
    """
    value_cols = [str(c) for c in (definition.get("value_cols") or [])]
    wanted = value_cols if columns is None else [str(c) for c in columns]
    labels = definition.get("value_labels") or {}
    units = definition.get("value_units") or {}
    label = str(definition.get("label") or "").strip()
    description = str(definition.get("description") or "").strip()
    rows = []
    for col in wanted:
        own = str(labels.get(col) or "").strip() if isinstance(labels, Mapping) else ""
        if own:
            short = own
        elif label and len(value_cols) > 1:
            short = f"{label} ({col})"
        else:
            short = label or col
        unit = str(units.get(col) or "").strip() if isinstance(units, Mapping) else ""
        detail = description
        if unit:
            detail = f"{detail} Unit: {unit}." if detail else f"Unit: {unit}."
        rows.append({"feature_name": (names or {}).get(col, col), "short_description": short,
                     "detailed_description": detail})
    return rows


def check_label(name: str | None) -> str:
    """A dataset name, trimmed: non-empty and at most 80 characters (any script)."""
    name = (name or "").strip()
    if not name:
        raise CustomExposomeError("give the dataset a name")
    if len(name) > MAX_LABEL:
        raise CustomExposomeError(f"name is longer than {MAX_LABEL} characters")
    return name


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_path(source: Any) -> bool:
    return isinstance(source, (str, os.PathLike))


def _abspath(source: str | os.PathLike) -> str:
    return os.path.abspath(os.path.expanduser(os.fspath(source)))


# --------------------------------------------------------------------------
# polygon (CSV) datasets
# --------------------------------------------------------------------------

def define_polygon(
    values: Source,
    *,
    boundary: str,
    key_col: str,
    value_cols: Sequence[str],
    year_col: str | None = None,
    label: str | None = None,
    description: str | None = "",
    labels: Mapping[str, str] | None = None,
    units: Mapping[str, str] | None = None,
    uploaded_filename: str | None = None,
    values_path: str | os.PathLike | None = None,
    dataset_id: str | None = None,
    owner_uid: int | str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Validate a CSV of values keyed by a boundary layer and return its definition.

    ``values`` is the CSV's bytes or its path. When it is a path, the
    definition points the C4 step at that file in place (``values_path``) and
    ``uploaded_filename`` defaults to its name; pass ``values_path`` to point
    it elsewhere (the web passes its library copy). ``label`` defaults to the
    file's stem when ``values`` is a path.

    ``dataset_id`` defaults to :func:`~spacescans.custom.keys.derive_dataset_id`
    of the content and the label; ``owner_uid`` is recorded as a string, or
    None. Raises :class:`CustomExposomeError` with a message for the user.

    ``labels`` and ``units`` are keyed by value column; entries for columns
    not selected, and blank ones, are dropped.
    """
    if label is None and _is_path(values):
        label = Path(os.fspath(values)).stem
    name = check_label(label)
    content = read_source(values)
    facts = validate_csv(content, boundary=boundary, key_col=key_col,
                         value_cols=value_cols, year_col=year_col)

    if _is_path(values):
        if values_path is None:
            values_path = _abspath(values)
        if uploaded_filename is None:
            uploaded_filename = Path(os.fspath(values)).name
    if uploaded_filename is None:
        uploaded_filename = "values.csv"
    if dataset_id is None:
        dataset_id = derive_dataset_id(content, name)

    # coverage_years drives the wizard's coverage panel. A static dataset has no
    # years of its own, so it claims the full window the shipped variables use.
    coverage = facts["coverage_years"] if year_col else list(STATIC_COVERAGE_YEARS)

    return {
        # --- catalog-shaped fields, served as-is next to the shipped ones ---
        "label": name,
        "description": ((description or "").strip()
                        or f"User-uploaded {boundary} values.")[:MAX_DESCRIPTION],
        "boundary": boundary,
        "spatial_method": "areal",
        "coverage_years": coverage,
        "coverage_region": "CONUS",
        "experiment": EXPERIMENT_KEY,
        # No ontology_id key at all: the catalog schema requires a non-empty
        # string when the key is present. Uploaded datasets have no ontology
        # node, which is why the web renders them in their own block.
        "data_source": f"Uploaded by user: {uploaded_filename}",
        "temporal": "yearly" if year_col else "static",
        "variable_type": "continuous",
        "display_unit": summarise_units(value_cols, units or {}),
        "value_cols": list(value_cols),
        # --- private: the steps and the library need these ---
        "geometry": "polygon",
        "dataset_id": dataset_id,
        "variable_key": variable_key(dataset_id),
        "owner_uid": None if owner_uid is None else str(owner_uid),
        "join_col": BOUNDARY_SPEC[boundary]["join_col"],
        "key_col": key_col,
        "year_col": year_col,
        "value_labels": selected_labels(labels, value_cols),
        "value_units": selected_units(units, value_cols),
        "values_path": None if values_path is None else str(values_path),
        "row_count": facts["row_count"],
        "distinct_keys": facts["distinct_keys"],
        "sha256": hashlib.sha256(content).hexdigest(),
        "uploaded_filename": uploaded_filename,
        "created_at": created_at or _now(),
    }


# --------------------------------------------------------------------------
# raster (GeoTIFF) datasets
# --------------------------------------------------------------------------

def preview_raster(source: bytes | str | os.PathLike) -> dict[str, Any]:
    """Describe an uploaded GeoTIFF for the upload dialog; raises on an unusable one.

    Raises :class:`CustomExposomeError` (not its :class:`RasterError` subclass),
    as the web does. Needs the ``[geo]`` extra.
    """
    from spacescans.custom import raster
    if isinstance(source, os.PathLike):
        source = os.fspath(source)
    try:
        return raster.inspect_raster(source)
    except RasterError as exc:
        raise CustomExposomeError(str(exc)) from exc


def check_band_columns(bands: Sequence[int], value_cols: Sequence[str] | None
                       ) -> tuple[list[int], list[str]]:
    """Bands distinct and positive, one valid, unused column name per band."""
    if not bands:
        raise CustomExposomeError("select at least one band")
    if len(bands) > MAX_VALUE_COLS:
        raise CustomExposomeError(f"{len(bands)} bands exceeds the limit of {MAX_VALUE_COLS}")
    try:
        bands = [int(b) for b in bands]
    except (TypeError, ValueError):
        raise CustomExposomeError(f"bands must be whole numbers, got {bands!r}") from None
    if any(b < 1 for b in bands):
        raise CustomExposomeError("band numbers start at 1")
    if len(set(bands)) != len(bands):
        raise CustomExposomeError("a band is selected twice")
    cols = [(c or "").strip() for c in (value_cols or [])]
    if len(cols) != len(bands):
        raise CustomExposomeError(
            f"{len(bands)} band(s) but {len(cols)} column name(s); name one column per band"
        )
    for col in cols:
        if not _COLUMN_NAME.fullmatch(col):
            raise CustomExposomeError(
                f"the value column name {col!r} must start with a letter and use only letters, "
                "digits and underscores (it becomes a column in result.csv)"
            )
        if col.lower() in RESERVED_RASTER_COLS:
            raise CustomExposomeError(
                f"column name {col!r} is already used by result.csv; choose another"
            )
    lowered = [c.lower() for c in cols]
    dupes = sorted({c for c in cols if lowered.count(c.lower()) > 1})
    if dupes:
        raise CustomExposomeError(
            f"column name(s) used twice: {', '.join(dupes)} (names are compared "
            "ignoring case, since spreadsheets and stats packages do)"
        )
    return bands, cols


def default_band_columns(bands: Sequence[int]) -> list[str]:
    """Column names when none are given: ``value`` for one band, ``band<N>`` for several."""
    return ["value"] if len(bands) == 1 else [f"band{int(b)}" for b in bands]


def _raster_inputs(rasters: Sequence[Sequence[Any]]) -> list[tuple[str, Any, int | None]]:
    """Normalise ``(source, year)`` and ``(filename, source, year)`` items."""
    out = []
    for item in rasters:
        item = tuple(item)
        if len(item) == 2:
            source, year = item
            filename = Path(os.fspath(source)).name if _is_path(source) else "upload.tif"
        elif len(item) == 3:
            filename, source, year = item
        else:
            raise CustomExposomeError(
                "each raster is (path_or_bytes, year) or (filename, path_or_bytes, year)"
            )
        if isinstance(source, (bytearray, memoryview)):
            source = bytes(source)
        elif isinstance(source, os.PathLike):
            source = os.fspath(source)
        elif not isinstance(source, (bytes, str)):
            raise TypeError(f"expected bytes or a path, got {type(source).__name__}")
        out.append((str(filename), source, year))
    return out


def _file_digests(source: bytes | str, combined) -> tuple[str, int]:
    """sha256 and size of one raster; also feeds ``combined`` with its bytes."""
    if isinstance(source, bytes):
        combined.update(source)
        return hashlib.sha256(source).hexdigest(), len(source)
    h = hashlib.sha256()
    size = 0
    with open(_abspath(source), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            combined.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def define_raster(
    rasters: Sequence[Sequence[Any]],
    *,
    label: str | None = None,
    description: str | None = "",
    bands: Sequence[int] | None = None,
    value_cols: Sequence[str] | None = None,
    labels: Mapping[str, str] | None = None,
    units: Mapping[str, str] | None = None,
    band: int = 1,
    value_col: str = "value",
    value_label: str = "",
    value_unit: str = "",
    paths: Sequence[str | os.PathLike | None] | None = None,
    dataset_id: str | None = None,
    owner_uid: int | str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Validate one GeoTIFF (static) or one per year (yearly) and return its definition.

    ``rasters`` lists ``(source, year)`` or ``(filename, source, year)``;
    ``source`` is a path or the file's bytes. A single file with year None is
    a static dataset; otherwise every file needs a distinct year. All files
    must share one grid (crs, transform, shape) because the C3 weights are
    computed once from the first and reused for every year.

    One or more bands become one result column each: ``bands`` and the
    parallel ``value_cols`` name them (default ``value`` for one band,
    ``band<N>`` for several); ``labels``/``units`` are keyed by column.
    Without ``bands`` the single-band arguments ``band``, ``value_col``,
    ``value_label`` and ``value_unit`` describe the one column, as in the web.
    The C3 weights are per grid, not per band, so every band shares one step.

    The definition points at each raster in place (its absolute path) unless
    ``paths`` (parallel to ``rasters``) says where they will live; the web
    passes its library paths. ``dataset_id`` defaults to a hash of every
    file's bytes and the label. Needs the ``[geo]`` extra.
    """
    from spacescans.custom import raster as rv

    items = _raster_inputs(rasters)
    if label is None and items and _is_path(items[0][1]):
        label = Path(items[0][1]).stem
    name = check_label(label)
    if not items:
        raise CustomExposomeError("upload at least one GeoTIFF")
    if len(items) > MAX_RASTER_YEARS:
        raise CustomExposomeError(f"{len(items)} files exceeds the limit of {MAX_RASTER_YEARS}")
    if bands is None:
        bands = [band]
        value_cols = [value_col]
        labels = {value_col.strip(): value_label} if value_label else {}
        units = {value_col.strip(): value_unit} if value_unit else {}
    elif value_cols is None:
        value_cols = default_band_columns(bands)
    bands, value_cols = check_band_columns(bands, value_cols)
    value_labels = selected_labels(labels, value_cols)
    value_units = selected_units(units, value_cols)

    years = [y for _f, _s, y in items]
    static = len(items) == 1 and years[0] is None
    if not static:
        if any(y is None for y in years):
            raise CustomExposomeError(
                "assign a year to every file, or upload a single file with no year "
                "for a time-invariant dataset"
            )
        if len(set(years)) != len(years):
            raise CustomExposomeError("two files carry the same year")
        bad = [y for y in years if not 1900 <= int(y) <= 2100]
        if bad:
            raise CustomExposomeError(f"unusable year(s): {bad}")

    metas = []
    for filename, source, _year in items:
        try:
            meta = rv.inspect_raster(_abspath(source) if isinstance(source, str) else source)
        except RasterError as exc:
            raise CustomExposomeError(f"{filename}: {exc}") from exc
        missing = [b for b in bands if not 1 <= b <= meta["band_count"]]
        if missing:
            raise CustomExposomeError(
                f"{filename}: band {', '.join(map(str, missing))} does not exist "
                f"(the file has {meta['band_count']})"
            )
        metas.append(meta)
    first = metas[0]
    for (filename, _s, _y), meta in zip(items[1:], metas[1:]):
        if not rv.same_grid(first, meta):
            raise CustomExposomeError(
                f"{filename} is on a different grid from {items[0][0]} "
                f"({meta['width']}x{meta['height']}, {meta['crs']} vs "
                f"{first['width']}x{first['height']}, {first['crs']}); every year "
                "must share one grid"
            )

    if paths is not None and len(paths) != len(items):
        raise CustomExposomeError(f"{len(paths)} paths for {len(items)} rasters")
    digest = hashlib.sha256()
    stored = []
    for i, (filename, source, year) in enumerate(items):
        sha, size = _file_digests(source, digest)
        if paths is not None:
            path = None if paths[i] is None else str(paths[i])
        else:
            path = _abspath(source) if isinstance(source, str) else None
        stored.append({
            "path": path,
            "year": year,
            "uploaded_filename": filename,
            "sha256": sha,
            "bytes": size,
        })
    stored.sort(key=lambda r: (r["year"] is None, r["year"] or 0))
    if dataset_id is None:
        dataset_id = derive_dataset_id(digest.digest(), name)

    coverage = [min(years), max(years)] if not static else list(STATIC_COVERAGE_YEARS)
    listed = {b["index"]: b for b in first.get("bands", [])}
    return {
        # --- catalog-shaped ---
        "label": name,
        "description": ((description or "").strip() or (
            f"User-uploaded {first['resolution_label']} raster"
            + ("" if static else f", {coverage[0]}–{coverage[1]}") + "."
        ))[:MAX_DESCRIPTION],
        "boundary": "Point",
        "spatial_method": "grid",
        "coverage_years": coverage,
        "coverage_region": "CONUS",
        "experiment": EXPERIMENT_KEY,
        "data_source": "Uploaded by user: " + ", ".join(r["uploaded_filename"] for r in stored)[:200],
        "temporal": "static" if static else "yearly",
        "variable_type": "continuous",
        "display_unit": summarise_units(value_cols, value_units),
        "value_cols": list(value_cols),
        # --- private ---
        "geometry": "raster",
        "dataset_id": dataset_id,
        "variable_key": variable_key(dataset_id),
        "owner_uid": None if owner_uid is None else str(owner_uid),
        "join_col": "grid_id",
        "key_col": "grid_id",
        "year_col": None if static else "year",
        "value_labels": value_labels,
        "value_units": value_units,
        "values_path": None,                     # materialised per run
        "rasters": stored,
        "bands": bands,                          # parallel to value_cols
        "band": bands[0],                        # definitions before multi-band read this
        "band_nodata": [listed.get(b, {}).get("nodata", first["nodata"]) for b in bands],
        "grid_hash": first["grid_hash"],
        "grid": {k: first[k] for k in ("width", "height", "crs", "transform",
                                       "resolution", "resolution_label", "nodata",
                                       "bounds_wgs84")},
        "row_count": first["cells"],
        "distinct_keys": first["cells"],
        "sha256": digest.hexdigest(),
        "uploaded_filename": stored[0]["uploaded_filename"],
        "created_at": created_at or _now(),
    }


def raster_bands(definition: Mapping[str, Any]) -> tuple[list[str], list[int], list[float | None]]:
    """(value_cols, bands, nodata) of a raster definition, parallel lists.

    Definitions written before multi-band uploads carry one ``band`` and a
    dataset-level ``grid.nodata``; they read as a one-band list.
    """
    cols = list(definition["value_cols"])
    bands = [int(b) for b in (definition.get("bands") or [definition.get("band", 1)])]
    if len(bands) != len(cols):
        raise ValueError(
            f"{definition.get('variable_key', 'raster dataset')}: {len(bands)} band(s) "
            f"for {len(cols)} value column(s)"
        )
    nodata = definition.get("band_nodata")
    if not nodata or len(nodata) != len(bands):
        nodata = [(definition.get("grid") or {}).get("nodata")] * len(bands)
    return cols, bands, list(nodata)


# --------------------------------------------------------------------------
# definitions handed back from outside (R, JSON)
# --------------------------------------------------------------------------

# Fields JSON writers turn into scalars when they hold one element (R's
# jsonlite with auto_unbox=TRUE), and fields they turn into [] or {} when empty
# or NULL. check_definition undoes both.
_LIST_FIELDS = ("value_cols", "bands", "band_nodata", "coverage_years")
_MAP_FIELDS = ("value_labels", "value_units")
_NULLABLE_FIELDS = ("year_col", "values_path", "owner_uid")


def _sha256_path(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_definition(definition: Mapping[str, Any], *, key: str | None = None,
                     files: bool = True, deep: bool = False) -> dict[str, Any]:
    """A definition received from outside, checked and normalised for the steps.

    For definitions that travelled through JSON and another language (the R
    package hands back what ``define_*`` returned). Undoes the usual JSON
    round-trip damage: a one-element list written as a scalar, ``[]`` for an
    empty object, ``{}`` for null. Then checks the fields the C3/C4 steps read,
    that ``key`` (when given) is the definition's own ``custom_<id>`` key, and,
    with ``files``, that the values file or every raster exists; ``deep`` also
    compares their sha256 with the recorded one, catching a file edited since
    it was defined. Returns a new dict; raises :class:`CustomExposomeError`.
    """
    from spacescans.custom.keys import is_custom_key

    if not isinstance(definition, Mapping):
        raise CustomExposomeError("a custom dataset definition must be an object")
    d = dict(definition)
    for f in _LIST_FIELDS:
        if f in d and d[f] is not None and not isinstance(d[f], (list, tuple)):
            d[f] = [d[f]]
        elif isinstance(d.get(f), tuple):
            d[f] = list(d[f])
    for f in _MAP_FIELDS:
        if d.get(f) in (None, [], ()):
            d[f] = {}
    for f in _NULLABLE_FIELDS:
        if f in d and d[f] in ({}, [], ""):
            d[f] = None
    if "band_nodata" in d and d["band_nodata"] is None:   # [null], unboxed
        d["band_nodata"] = [None]
    if isinstance(d.get("rasters"), Mapping):            # one raster, unboxed
        d["rasters"] = [d["rasters"]]
    if isinstance(d.get("rasters"), list):
        d["rasters"] = [{**r, "year": None} if isinstance(r, Mapping) and r.get("year") in ({}, [])
                        else r for r in d["rasters"]]
    if isinstance(d.get("grid"), Mapping) and d["grid"].get("nodata") in ({}, []):
        d["grid"] = {**d["grid"], "nodata": None}

    name = d.get("variable_key") or key or "custom dataset"
    if key is not None:
        if not is_custom_key(key):
            raise CustomExposomeError(f"{key!r} is not a custom dataset key (custom_<8 hex>)")
        if d.get("variable_key") not in (None, key):
            raise CustomExposomeError(
                f"{key!r} holds the definition of {d['variable_key']!r}; pass each "
                "definition under its own variable_key")

    cols = d.get("value_cols")
    if not cols or not all(isinstance(c, str) and c for c in cols):
        raise CustomExposomeError(f"{name}: value_cols must be a non-empty list of column names")
    if len(cols) > MAX_VALUE_COLS:
        raise CustomExposomeError(f"{name}: {len(cols)} value columns exceeds the limit of {MAX_VALUE_COLS}")
    years = d.get("coverage_years")
    if years is not None:
        if len(years) != 2 or not all(isinstance(y, (int, float)) and not isinstance(y, bool)
                                      and float(y).is_integer() for y in years):
            raise CustomExposomeError(f"{name}: coverage_years must be [first, last]")
        d["coverage_years"] = [int(y) for y in years]

    geometry = d.get("geometry")
    if geometry == "polygon":
        boundary = d.get("boundary")
        if boundary not in BOUNDARY_SPEC:
            raise CustomExposomeError(
                f"{name}: unknown boundary {boundary!r}; expected one of {sorted(BOUNDARY_SPEC)}")
        if d.get("join_col") != BOUNDARY_SPEC[boundary]["join_col"]:
            raise CustomExposomeError(
                f"{name}: a {boundary} dataset joins on {BOUNDARY_SPEC[boundary]['join_col']!r}, "
                f"not {d.get('join_col')!r}")
        if not isinstance(d.get("key_col"), str) or not d["key_col"]:
            raise CustomExposomeError(f"{name}: key_col is missing")
        path = d.get("values_path")
        if not isinstance(path, str) or not path:
            raise CustomExposomeError(f"{name}: values_path is missing; define it from a file path")
        checks = [(path, d.get("sha256"))]
    elif geometry == "raster":
        if not (isinstance(d.get("grid_hash"), str) and re.fullmatch(r"[0-9a-f]{8}", d["grid_hash"])):
            raise CustomExposomeError(f"{name}: grid_hash must be 8 hex characters")
        if d.get("join_col") != "grid_id" or d.get("key_col") != "grid_id":
            raise CustomExposomeError(f"{name}: a raster dataset joins on grid_id")
        rasters = d.get("rasters")
        if not rasters or not all(isinstance(r, Mapping) and isinstance(r.get("path"), str)
                                  and r["path"] for r in rasters):
            raise CustomExposomeError(f"{name}: rasters must list each file's path")
        yearly = any(r.get("year") is not None for r in rasters)
        if yearly != bool(d.get("year_col")):
            raise CustomExposomeError(f"{name}: year_col must be 'year' exactly when the rasters carry years")
        try:
            raster_bands(d)
        except ValueError as exc:
            raise CustomExposomeError(str(exc)) from None
        checks = [(r["path"], r.get("sha256")) for r in rasters]
    else:
        raise CustomExposomeError(f"{name}: geometry must be 'polygon' or 'raster', not {geometry!r}")

    if files or deep:
        for path, sha in checks:
            if not os.path.isfile(path):
                raise CustomExposomeError(f"{name}: file not found: {path}")
            if deep and sha and _sha256_path(path) != sha:
                raise CustomExposomeError(
                    f"{name}: {path} has changed since the dataset was defined; define it again")
    return d
