"""Registered rasters: GeoTIFFs on one grid, one file or one per year, two weeks or day.

:func:`prepare_raster` checks every file the way the upload path does
(:func:`spacescans.custom.inspect_raster`: a CRS, north-up, not rotated, over
the continental US), that all files share one grid (the C3 grid weights are
computed once, from the first file, and serve every period) and that every
linked band exists in every file. It assigns each file its period
(:mod:`spacescans.custom.periods`), copies the files into
``rasters/<nnnnn>_<name>`` in period order and writes the period index
``periods.json``.

Each variable is one band (``{"name", "unit", "description", "band"}``;
default: band 1, named after the dataset). Nodata is each file's own.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from spacescans.custom.periods import assign_periods, read_manifest, split_manifest
from spacescans.custom.prepare import (
    MAX_VARIABLES,
    Prepared,
    common_definition,
    normalise_variables,
    public_variables,
    slug,
)
from spacescans.custom.registry import DatasetError, sha256_of

if TYPE_CHECKING:  # pragma: no cover
    from spacescans.custom.registry import DatasetRecord, Input

RASTER_DIR = "rasters"
PERIODS_FILE = "periods.json"
#: Most files one raster dataset may hold (about 27 years of daily files).
MAX_RASTER_FILES = 10_000
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _copy(src: Path, dst: Path) -> tuple[str, int]:
    """Copy a file, returning the sha256 and size of what was copied."""
    h = hashlib.sha256()
    size = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for chunk in iter(lambda: fin.read(1 << 20), b""):
            h.update(chunk)
            fout.write(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _bands(vars_: list[dict] | None, name: str) -> list[dict]:
    if vars_ is None:
        return [{"name": slug(name), "unit": None, "description": None, "band": 1}]
    out = []
    for i, var in enumerate(vars_):
        band = var.get("band", i + 1)
        if isinstance(band, bool) or not isinstance(band, int) or band < 1:
            raise DatasetError(f"variable {var['name']!r}: band must be a whole number from 1")
        out.append({**var, "band": band})
    bands = [v["band"] for v in out]
    if len(set(bands)) != len(bands):
        raise DatasetError("a band is linked twice")
    return out


def prepare_raster(inputs: Sequence[Input], work: Path, *, name: str, temporal: str, key: Any,
                   variables: Any) -> Prepared:
    """Check the GeoTIFFs and write the copies and ``periods.json`` into ``work``."""
    from spacescans.custom import raster as rv
    from spacescans.custom.errors import RasterError

    if key not in (None, {}):
        raise DatasetError("a raster dataset takes no key; its periods come from the file "
                           "names, a manifest CSV or a {path: period} mapping")
    given = {i.path: i.period for i in inputs}
    data, manifest_path = split_manifest([i.path for i in inputs], (".tif", ".tiff"))
    if not data:
        raise DatasetError("give at least one GeoTIFF (.tif)")
    if len(data) > MAX_RASTER_FILES:
        raise DatasetError(f"{len(data):,} files exceed the limit of {MAX_RASTER_FILES:,}")
    names = [p.name for p in data]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise DatasetError(f"two files are named {dupes[0]}; give each file a distinct name")
    if manifest_path is not None and given.get(manifest_path) is not None:
        raise DatasetError("the manifest takes no period of its own")
    if temporal == "static":
        if len(data) != 1 or manifest_path is not None:
            raise DatasetError("a static raster dataset is exactly one GeoTIFF")
        if given[data[0]] is not None:
            raise DatasetError("a static raster has no period")
        periods: list[Any] = [None]
    else:
        manifest = read_manifest(manifest_path, temporal) if manifest_path else None
        periods = assign_periods([(p.name, given[p]) for p in data], temporal, manifest)
    order = sorted(range(len(data)), key=lambda i: (
        0 if periods[i] is None else (periods[i].year or 0),
        "" if periods[i] is None or periods[i].start is None else periods[i].start.isoformat()))
    vars_ = _bands(normalise_variables(variables, extra=("band",)), name)
    if len(vars_) > MAX_VARIABLES:  # pragma: no cover - normalise_variables checks
        raise DatasetError("too many bands")
    bands = [v["band"] for v in vars_]

    metas = []
    for i in order:
        path = data[i]
        try:
            meta = rv.inspect_raster(str(path))
        except RasterError as exc:
            raise DatasetError(f"{path.name}: {exc}") from None
        missing = [b for b in bands if b > meta["band_count"]]
        if missing:
            raise DatasetError(f"{path.name}: band {', '.join(map(str, missing))} does not exist "
                               f"(the file has {meta['band_count']})")
        if metas and not rv.same_grid(metas[0], meta):
            first = data[order[0]].name
            raise DatasetError(
                f"{path.name} is on a different grid from {first} ({meta['width']}x"
                f"{meta['height']}, {meta['crs']} vs {metas[0]['width']}x{metas[0]['height']}, "
                f"{metas[0]['crs']}); every file must share one grid")
        metas.append(meta)

    (work / RASTER_DIR).mkdir()
    entries, originals, prepared_sha = [], [], {}
    for n, i in enumerate(order, start=1):
        path = data[i]
        stem = _SAFE.sub("_", path.name)[-100:]
        rel = f"{RASTER_DIR}/{n:05d}_{stem}"
        sha, size = _copy(path, work / rel)
        entry: dict[str, Any] = {"file": rel, "source_name": path.name, "sha256": sha,
                                 "bytes": size}
        if periods[i] is not None:
            entry.update(periods[i].to_dict())
        entries.append(entry)
        originals.append({"name": path.name, "sha256": sha, "bytes": size, "path": path})
        prepared_sha[rel] = sha
    index = {"temporal": temporal, "files": entries}
    (work / PERIODS_FILE).write_text(json.dumps(index, indent=1) + "\n", encoding="utf-8")
    prepared_sha[PERIODS_FILE] = sha256_of(work / PERIODS_FILE)[0]
    if manifest_path is not None:
        sha, size = sha256_of(manifest_path)
        originals.append({"name": manifest_path.name, "sha256": sha, "bytes": size,
                          "path": manifest_path})

    first = metas[0]
    details: dict[str, Any] = {
        "grid_hash": first["grid_hash"],
        "grid": {k: first[k] for k in ("width", "height", "crs", "transform", "resolution",
                                       "resolution_label", "nodata", "bounds_wgs84")},
        "band_count": first["band_count"],
        "bands": bands,
        "file_count": len(entries),
        "prepared_sha256": prepared_sha,
    }
    if temporal == "yearly":
        years = [e["year"] for e in entries]
        details["coverage_years"] = [min(years), max(years)]
    elif temporal in ("daily", "biweekly"):
        details["coverage_dates"] = [entries[0]["start"], entries[-1]["end"]]
        details["coverage_years"] = [int(entries[0]["start"][:4]), int(entries[-1]["end"][:4])]
    span = details.get("coverage_dates") or details.get("coverage_years")
    when = f", {span[0]} to {span[1]}" if span else ""
    description = (f"Registered {temporal} raster ({first['resolution_label']}, "
                   f"{len(entries)} file{'s' if len(entries) != 1 else ''}{when}).")
    return Prepared(key=None, variables=public_variables(vars_), files=[e["file"] for e in entries]
                    + [PERIODS_FILE], details=details, originals=originals,
                    description=description, extras={"bands": vars_})


def raster_definition(record: DatasetRecord) -> dict[str, Any]:
    """The linkable definition of a registered raster dataset."""
    from spacescans.custom.definition import STATIC_COVERAGE_YEARS

    try:
        index = json.loads(record.path(PERIODS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DatasetError(f"dataset {record.name!r}: its period index is missing or "
                           "unreadable; register it again") from None
    details = record.details
    rasters = []
    for entry in index.get("files") or []:
        item: dict[str, Any] = {"path": os.fspath(record.path(entry["file"])),
                                "year": entry.get("year"),
                                "uploaded_filename": entry.get("source_name"),
                                "sha256": entry.get("sha256"), "bytes": entry.get("bytes")}
        if record.temporal in ("daily", "biweekly"):
            item["start"], item["end"] = entry.get("start"), entry.get("end")
        rasters.append(item)
    bands = [int(b) for b in details.get("bands") or [1]]
    years = (details.get("coverage_years") if record.temporal != "static"
             else list(STATIC_COVERAGE_YEARS))
    d = common_definition(record, geometry="raster", boundary="Point", spatial_method="grid",
                          coverage_years=years)
    grid = dict(details.get("grid") or {})
    d.update({
        "join_col": "grid_id",
        "key_col": "grid_id",
        "year_col": "year" if record.temporal == "yearly" else None,
        "date_col": None,
        "values_path": None,
        "rasters": rasters,
        "bands": bands,
        "band": bands[0],
        # None: each file's own nodata (files of a series may differ)
        "band_nodata": [None] * len(bands),
        "grid_hash": details.get("grid_hash"),
        "grid": grid,
        "row_count": (grid.get("width") or 0) * (grid.get("height") or 0),
        "distinct_keys": (grid.get("width") or 0) * (grid.get("height") or 0),
        "sha256": record.checksum,
    })
    if record.temporal in ("daily", "biweekly"):
        d["coverage_dates"] = list(details.get("coverage_dates") or [])
    return d
