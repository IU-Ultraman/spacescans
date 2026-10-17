"""Inspect, validate and read uploaded GeoTIFFs.

Two jobs, both pure functions over rasterio:

  * at upload: say whether the file can be a C3 template at all (has a CRS, is
    north-up with no rotation, lies over the continental US), and describe it
    for the mapping UI;
  * at run time: turn the C3 weights' ``grid_id`` list back into pixel values,
    so the C4 step can read a plain ``(grid_id, value[, year])`` table through
    ``read_table`` with no plugin, exactly as a polygon dataset does.

The ``grid_id`` convention is the pipeline's: 0-based, row-major,
``grid_id = row * width + col`` (``grid_weights`` with ``grid_id_offset: 0``).
The noise reader inverts it the same way.

rasterio is imported inside the functions that need it, so this module imports
with the base install; calling them without the ``[geo]`` extra raises
:class:`spacescans._extras.MissingExtraError` (an ``ImportError``).

Ported verbatim from spacescans-web ``app/raster_values.py``. One addition:
:func:`extract_bands` takes ``full_read_max_cells`` so a caller (the web's
re-export shim, its tests) can choose the block path without patching this
module's global.
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from spacescans.custom.errors import RasterError

# Continental US, WGS84. A raster that does not intersect this cannot link any
# cohort this deployment serves; refusing it at upload saves a run that would
# report 0 % and a user asking why.
CONUS_BBOX = (-125.5, 24.0, -66.5, 49.8)          # west, south, east, north

# Every band a user can pick must be listed, so this matches the per-dataset
# column limit (table.MAX_VALUE_COLS): one result column per band.
MAX_BANDS_LISTED = 40

# Below this many cells a single full-band read is simplest and fastest. Above
# it, only the GeoTIFF blocks that hold a requested cell are decoded, so memory
# follows the cohort's footprint rather than the raster's: a 250 m national grid
# is ~200 M cells (1.5 GB as float64) while a cohort touches a few thousand.
FULL_READ_MAX_CELLS = 20_000_000


def _require_geo() -> None:
    from spacescans._extras import require
    require("geo", "rasterio")


def _open(source: bytes | str | Path):
    import rasterio
    from rasterio.io import MemoryFile

    from spacescans.custom.safety import reject_format_confusion

    # An uploaded raster is validated by extension then opened by content: refuse a VRT (format
    # confusion -> local file read / SSRF) and pin the GTiff driver so GDAL cannot auto-detect VRT
    # or another driver from the bytes. Review finding R2-1.
    reject_format_confusion(source, RasterError)
    if isinstance(source, (str, Path)):
        return rasterio.open(str(source), driver="GTiff")
    return MemoryFile(source).open(driver="GTiff")


def inspect_raster(source: bytes | str | Path) -> dict[str, Any]:
    """Describe a raster and check it can serve as a C3 template.

    ``source`` is the file's bytes or its path. Raises :class:`RasterError` for
    anything that would make ``grid_weights`` fail or link nothing. Returns the
    facts the definition and the upload dialog need, including ``grid_hash``.
    """
    _require_geo()
    import rasterio
    from rasterio.warp import transform_bounds

    if isinstance(source, (bytearray, memoryview)):
        source = bytes(source)
    try:
        src = _open(source)
    except rasterio.errors.RasterioIOError as exc:
        raise RasterError(
            "not a raster rasterio can open — upload a GeoTIFF (.tif)"
        ) from exc

    with src:
        if src.crs is None:
            raise RasterError(
                "the raster has no coordinate reference system; assign one "
                "(e.g. EPSG:4326) and re-export"
            )
        t = src.transform
        if t.b != 0 or t.d != 0:
            raise RasterError("the raster is rotated; only axis-aligned grids are supported")
        if t.e >= 0:
            # exactextract refuses bottom-up rasters ("Incompatible extents"),
            # the way ACAG's ascending-latitude arrays first failed.
            raise RasterError(
                "the raster is stored bottom-up (south row first); re-export it "
                "north-up, which every GIS does by default"
            )
        if src.count < 1:
            raise RasterError("the raster has no bands")

        try:
            w, s, e, n = transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)
        except Exception as exc:  # pragma: no cover - exotic CRS
            raise RasterError(f"could not reproject the raster's extent to WGS84: {exc}") from exc
        cw, cs, ce, cn = CONUS_BBOX
        if e < cw or w > ce or n < cs or s > cn:
            raise RasterError(
                f"the raster covers lon {w:.1f}…{e:.1f}, lat {s:.1f}…{n:.1f}, which "
                "does not touch the continental US"
            )

        bands = []
        for i in range(1, min(src.count, MAX_BANDS_LISTED) + 1):
            desc = src.descriptions[i - 1] if src.descriptions else None
            nd = src.nodatavals[i - 1] if src.nodatavals else None
            bands.append({"index": i, "description": desc or "", "dtype": str(src.dtypes[i - 1]),
                          "nodata": None if nd is None else float(nd)})

        res_x, res_y = src.res
        meta = {
            "width": src.width,
            "height": src.height,
            "cells": src.width * src.height,
            "crs": str(src.crs),
            "transform": [t.a, t.b, t.c, t.d, t.e, t.f],
            "resolution": [abs(res_x), abs(res_y)],
            "resolution_label": _resolution_label(src.crs, abs(res_x), abs(res_y)),
            "band_count": src.count,
            "bands": bands,
            "nodata": None if src.nodata is None else float(src.nodata),
            "bounds_wgs84": [w, s, e, n],
        }
    meta["grid_hash"] = grid_hash(meta)
    return meta


def grid_hash(meta: dict[str, Any]) -> str:
    """Identity of the GRID (crs, transform, shape), not of the pixel values.

    The C3 weights depend only on where the cells are. Two yearly rasters on
    the same grid, or two datasets on the same grid, share one C3 step and one
    cache entry, so this is what the cache tag is built from.

    It hashes ``str(crs)``, whose spelling can differ between rasterio/GDAL
    versions, so do not rely on it across environments.
    """
    sig = f"{meta['crs']}|{'|'.join(f'{v:.12g}' for v in meta['transform'])}|{meta['width']}x{meta['height']}"
    return hashlib.sha256(sig.encode()).hexdigest()[:8]


def _resolution_label(crs, rx: float, ry: float) -> str:
    """"1 km", "250 m", "0.01°", for the dialog and the detail panel."""
    if crs.is_geographic:
        return f"{rx:g}°" if abs(rx - ry) < 1e-9 else f"{rx:g}° × {ry:g}°"
    r = rx if abs(rx - ry) < 1e-6 else None
    if r is None:
        return f"{rx:g} × {ry:g} m"
    return f"{r / 1000:g} km" if r >= 1000 else f"{r:g} m"


def same_grid(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return grid_hash(a) == grid_hash(b)


def extract_values(
    raster_path: str | Path,
    grid_ids: np.ndarray,
    *,
    band: int = 1,
    nodata: float | None = None,
    full_read_max_cells: int | None = None,
) -> np.ndarray:
    """Pixel values at each grid_id (row-major, 0-based). NaN for nodata or an
    id outside the raster."""
    return extract_bands(raster_path, grid_ids, bands=[band], nodata=nodata,
                         full_read_max_cells=full_read_max_cells)[:, 0]


def extract_bands(
    raster_path: str | Path,
    grid_ids: np.ndarray,
    *,
    bands: Sequence[int],
    nodata: float | None | Sequence[float | None] = None,
    full_read_max_cells: int | None = None,
) -> np.ndarray:
    """Pixel values of several bands at each grid_id: shape (len(grid_ids),
    len(bands)), columns in the order of ``bands``. NaN for nodata or an id
    outside the raster.

    ``nodata`` is one value for every band, or a list parallel to ``bands``;
    None (overall or for one band) falls back to that band's own nodata in the
    file. A GeoTIFF normally stores one value for all bands.
    A full read decodes one band at a time, so peak memory is one band, not
    the whole stack; a windowed read pulls every requested band per block.
    Rasters with more than ``full_read_max_cells`` cells (default
    :data:`FULL_READ_MAX_CELLS`) are read block by block.
    """
    _require_geo()
    from rasterio.windows import Window

    max_cells = FULL_READ_MAX_CELLS if full_read_max_cells is None else int(full_read_max_cells)
    bands = [int(b) for b in bands]
    if not bands:
        raise RasterError("no band requested")
    ids = np.asarray(grid_ids, dtype=np.int64)
    out = np.full((ids.shape[0], len(bands)), np.nan)
    with _open(raster_path) as src:
        width, height = src.width, src.height
        for b in bands:
            if not 1 <= b <= src.count:
                raise RasterError(f"band {b} does not exist; the raster has {src.count}")
        given = list(nodata) if isinstance(nodata, (list, tuple)) else [nodata] * len(bands)
        if len(given) != len(bands):
            raise RasterError(f"{len(given)} nodata values for {len(bands)} bands")
        nds = [src.nodatavals[b - 1] if nd is None else nd for b, nd in zip(bands, given)]

        inside = (ids >= 0) & (ids < width * height)
        rows = ids[inside] // width
        cols = ids[inside] % width
        vals = np.full((rows.shape[0], len(bands)), np.nan)

        if width * height <= max_cells:
            for j, b in enumerate(bands):
                arr = src.read(b, masked=False)
                vals[:, j] = arr[rows, cols].astype("float64")
        else:
            bh, bw = src.block_shapes[bands[0] - 1]
            block_of = (rows // bh) * ((width + bw - 1) // bw) + (cols // bw)
            for blk in np.unique(block_of):
                sel = block_of == blk
                r0 = int(rows[sel][0] // bh) * bh
                c0 = int(cols[sel][0] // bw) * bw
                win = Window(c0, r0, min(bw, width - c0), min(bh, height - r0))
                arr = src.read(bands, window=win, masked=False)     # (n_bands, h, w)
                vals[sel, :] = arr[:, rows[sel] - r0, cols[sel] - c0].T.astype("float64")

    for j, nd in enumerate(nds):
        if nd is not None:
            vals[:, j] = np.where(np.isclose(vals[:, j], nd), np.nan, vals[:, j])
    out[inside, :] = vals
    return out
