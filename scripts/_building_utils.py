"""Shared helpers for the building-footprint patient sampling scripts.

Functions:
- quadkeys_for_bbox: bbox -> set of zoom-9 quadkey strings (zero-padded to 9 chars)
- polygon_area_m2: shapely polygon in WGS84 -> area in square meters via equal-area reproject
- filter_polygons_by_area: boolean mask for inclusive [min, max] filter
- iter_geojsonl_gz: stream-parse a gzipped GeoJSONL file yielding feature dicts (one per line)
- representative_lonlat: deterministic centroid (or representative_point fallback) in WGS84
"""
from __future__ import annotations

import gzip
import json
from functools import lru_cache
from pathlib import Path
from typing import Iterator

import mercantile
import pyproj
from shapely.geometry import Polygon
from shapely.ops import transform as shapely_transform


def quadkeys_for_bbox(bbox: tuple[float, float, float, float], zoom: int = 9) -> set[str]:
    """Return all Bing quadkey tile IDs that intersect bbox at the given zoom level.

    bbox: (minx, miny, maxx, maxy) in EPSG:4326.
    zoom: Bing tile zoom level (default 9 — Microsoft GlobalMLBuildingFootprints partition).
    Output strings are zero-padded to width `zoom` chars (US quadkeys are 8-9 chars, MS index uses 9-char padded).
    """
    tiles = mercantile.tiles(*bbox, zooms=[zoom])
    return {mercantile.quadkey(t).zfill(zoom) for t in tiles}


@lru_cache(maxsize=8)
def _get_transformer(source_crs: str, area_crs: str) -> pyproj.Transformer:
    """Cached factory — pyproj Transformer is thread-safe and reusable."""
    return pyproj.Transformer.from_crs(source_crs, area_crs, always_xy=True)


def polygon_area_m2(poly: Polygon, *, source_crs: str = "EPSG:4326", area_crs: str = "EPSG:6346") -> float:
    """Compute polygon area in square meters by reprojecting to an equal-area / UTM CRS.

    Default area_crs is EPSG:6346 (NAD83(2011) / UTM zone 17N), suitable for all of Florida.
    For multi-state rollout, callers should pass an appropriate equal-area CRS.
    """
    transformer = _get_transformer(source_crs, area_crs)
    proj_poly = shapely_transform(transformer.transform, poly)
    return proj_poly.area


def filter_polygons_by_area(
    areas: list[float], *, area_min: float, area_max: float,
) -> list[bool]:
    """Return a boolean mask: True if area_min <= area <= area_max (inclusive bounds)."""
    return [(area_min <= a <= area_max) for a in areas]


def representative_lonlat(poly: Polygon) -> tuple[float, float]:
    """Return a (lon, lat) point guaranteed to be inside the polygon.

    Uses centroid if it lies within the polygon, else falls back to representative_point()
    (guaranteed-inside for any non-empty polygon).
    """
    c = poly.centroid
    if poly.contains(c):
        return (c.x, c.y)
    rp = poly.representative_point()
    return (rp.x, rp.y)


def iter_geojsonl_gz(path: Path) -> Iterator[dict]:
    """Stream-parse a gzipped GeoJSON Lines file. Yields one feature dict per line.

    Microsoft's GlobalMLBuildingFootprints uses .csv.gz extension but contents are GeoJSONL.
    """
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno} malformed JSON: {e}") from e


__all__ = [
    "quadkeys_for_bbox",
    "polygon_area_m2",
    "filter_polygons_by_area",
    "representative_lonlat",
    "iter_geojsonl_gz",
]
