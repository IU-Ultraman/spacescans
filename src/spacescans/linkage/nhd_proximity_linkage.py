"""NHD blue-space proximity (C3-equivalent): per-geoid distances to nearest
flow / water / area / coast NHD feature, plus combined min (dist_blue_m).

Output schema matches v1 proximity_blue_by_category_m.pkl:
    geoid (int), dist_flow_m, dist_water_m, dist_area_m, dist_coast_m,
    dist_blue_m (float64)

Caching strategy: per-tile × per-category filtered + projected feature
geometries cached as parquet on disk. The tile grid is a fixed 0.5° lat/lon
grid covering the patient bbox, so cache is cohort-independent for the same
NHD GDB. Heavy work (sjoin_nearest with patient buffers) stays cohort-specific.
"""
from __future__ import annotations

from spacescans._extras import require
require("geo", "geopandas", "rasterio", "shapely", "exactextract")

import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import force_2d, make_valid
from shapely.geometry import box

from spacescans.io.writers import write_table
from spacescans.linkage.helpers import load_patients
from spacescans.models.config import DatasetConfig
from spacescans.models.protocols import AggregationEngine
from spacescans.pipeline.registry import register_pattern

_CRS_LL = 4326
_CRS_M = 5070  # NAD83 / Conus Albers (meters)
_GRID_DEG = 0.5
_BUFFER_M = 15000  # padding around tile when reading GDB

# FCODE prefix filters (matches v1)
_FLOW_PREFIX = {460, 336, 558}       # Stream/River, Canal/Ditch, Artificial Path
_WATER_PREFIX = {390, 436, 466, 493}  # Lake/Pond, Reservoir, Swamp/Marsh, Estuary
_AREA_PREFIX = {312, 493, 460}       # Bay/Inlet, Estuary, areal Stream/River
# "coast" = Coastline (FType 566). It DOES exist in NHDPlus HR, but in the
# NHDFlowline layers — the original code looked in NHDLine, where there are no
# 566 features, so coast matched nothing (99999 everywhere) or, via the old v1
# FCODE-relax fallback, arbitrary NHDLine lines (garbage inland). Point coast at
# the flowline layers instead. (Distance is identical to the NHDArea SeaOcean
# 445 polygon edge, since 566 IS that shoreline, but the lines are far lighter
# to read per tile than the giant ocean polygon.)
_COAST_PREFIX = {566}                # Coastline (in NHDFlowline)

_CATEGORIES = ("flow", "water", "area", "coast")


def _fcode_mask(gdf: gpd.GeoDataFrame, keep_prefix: set[int] | None) -> np.ndarray:
    if keep_prefix is None:
        return np.ones(len(gdf), dtype=bool)
    col = next((c for c in gdf.columns if c.lower() == "fcode"), None)
    if col is None:
        return np.ones(len(gdf), dtype=bool)
    fc = pd.to_numeric(gdf[col], errors="coerce").fillna(-1).astype(int)
    return (fc // 100).isin(keep_prefix).values


def _prepare_lines(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame | None:
    if gdf is None or len(gdf) == 0:
        return None
    gdf = gdf.copy()
    gdf["geometry"] = gdf.geometry.apply(
        lambda g: force_2d(g) if g is not None and not g.is_empty else None
    )
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].to_crs(_CRS_M)
    lines = []
    for geom in gdf.geometry:
        if geom.geom_type in ("LineString", "MultiLineString"):
            lines.append(geom)
        elif geom.geom_type == "GeometryCollection":
            for part in geom.geoms:
                if "LineString" in part.geom_type:
                    lines.append(part)
    if not lines:
        return None
    result = gpd.GeoDataFrame(geometry=gpd.GeoSeries(lines, crs=_CRS_M))
    result = result[~result.geometry.is_empty]
    return result if len(result) > 0 else None


def _prepare_areas(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame | None:
    if gdf is None or len(gdf) == 0:
        return None
    gdf = gdf.copy()
    gdf["geometry"] = gdf.geometry.apply(
        lambda g: force_2d(g) if g is not None and not g.is_empty else None
    )
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].to_crs(_CRS_M)
    polys = []
    for geom in gdf.geometry:
        if geom.geom_type in ("Polygon", "MultiPolygon"):
            polys.append(make_valid(geom))
        elif geom.geom_type == "GeometryCollection":
            for part in geom.geoms:
                if "Polygon" in part.geom_type:
                    polys.append(make_valid(part))
    if not polys:
        return None
    result = gpd.GeoDataFrame(geometry=gpd.GeoSeries(polys, crs=_CRS_M))
    result = result[~result.geometry.is_empty]
    return result if len(result) > 0 else None


def _tile_bbox_ll(tile_geom) -> tuple[float, float, float, float]:
    """Tile + 15km buffer in projected CRS, then back to WGS84 bbox."""
    tgdf = gpd.GeoDataFrame(geometry=[tile_geom], crs=_CRS_LL).to_crs(_CRS_M)
    tgdf["geometry"] = tgdf.geometry.buffer(_BUFFER_M)
    return tuple(tgdf.to_crs(_CRS_LL).total_bounds)


def _read_layer_bbox(
    gdb_path: Path,
    layer_name: str,
    bbox: tuple[float, float, float, float],
    keep_prefix: set[int] | None,
    target: str,
) -> gpd.GeoDataFrame | None:
    try:
        gdf = gpd.read_file(str(gdb_path), layer=layer_name, bbox=bbox, engine="pyogrio")
    except Exception:
        return None
    if gdf is None or len(gdf) == 0:
        return None
    if keep_prefix is not None:
        gdf = gdf[_fcode_mask(gdf, keep_prefix)]
        if len(gdf) == 0:
            return None
    return _prepare_lines(gdf) if target == "line" else _prepare_areas(gdf)


def _category_layers(avail: list[str]) -> dict[str, list[str]]:
    flow = [l for l in ["NetworkNHDFlowline", "NonNetworkNHDFlowline", "NHDFlowline"] if l in avail]
    water = ["NHDWaterbody"] if "NHDWaterbody" in avail else []
    area = ["NHDArea"] if "NHDArea" in avail else []
    # coast = Coastline (566), which lives in the NHDFlowline layers (same
    # source as flow, filtered to FCode 566) — NOT NHDLine.
    coast = flow
    return {"flow": flow, "water": water, "area": area, "coast": coast}


def _category_target(cat: str) -> str:
    # flow and coast are both NHDFlowline lines; water/area are polygons.
    return "line" if cat in ("flow", "coast") else "area"


def _category_prefix(cat: str) -> set[int]:
    return {"flow": _FLOW_PREFIX, "water": _WATER_PREFIX,
            "area": _AREA_PREFIX, "coast": _COAST_PREFIX}[cat]


def _snap_to_grid(v: float, grid_deg: float = _GRID_DEG) -> float:
    """Floor a coordinate to the global lattice origin (a multiple of grid_deg)."""
    return float(np.floor(v / grid_deg) * grid_deg)


def _global_tile_index(sw_lon: float, sw_lat: float,
                       grid_deg: float = _GRID_DEG) -> tuple[int, int]:
    """Global 0.5° lattice index (gx, gy) of a tile, from its SW corner.

    Cohort-independent: a given patch of ground always yields the same
    (gx, gy), so the feature cache is safe to share across cohorts. The
    pre-fix key was a cohort-relative ``tile_id`` (index into a per-cohort
    bbox grid), which made the same filename mean different geography per
    cohort and served one cohort's cached tiles to another.
    """
    return int(round(sw_lon / grid_deg)), int(round(sw_lat / grid_deg))


def _cache_path(cache_dir: Path, gx: int, gy: int, cat: str) -> Path:
    return cache_dir / f"tile_gx{gx}_gy{gy}_{cat}.parquet"


def _load_or_compute_tile_category(
    gdb_path: Path,
    bbox: tuple[float, float, float, float],
    layers: list[str],
    cat: str,
    cache_path: Path | None,
) -> gpd.GeoDataFrame | None:
    """Return prepared (CRS_M) features for one (tile, category). Cache-aware."""
    if cache_path is not None and cache_path.exists():
        try:
            return gpd.read_parquet(cache_path)
        except Exception:
            pass  # corrupt cache, fall through

    # Computing a tile needs the GDB. Guard explicitly: _read_layer_bbox
    # swallows read errors, so a missing GDB would otherwise yield an EMPTY
    # tile — silently wrong distances — instead of a diagnosable failure.
    if not Path(gdb_path).exists():
        raise FileNotFoundError(
            f"NHD tile cache miss ({cache_path.name if cache_path else bbox}) "
            f"and no NHDPlus GDB at {gdb_path}. Extract the full nhd_features "
            "cache archive, or provide the raw GDB for on-demand tiling."
        )

    target = _category_target(cat)
    keep_prefix = _category_prefix(cat)

    parts = []
    for ln in layers:
        g = _read_layer_bbox(gdb_path, ln, bbox, keep_prefix, target)
        if g is not None:
            parts.append(g)
    if not parts and cat != "coast":
        # Relax FCODE filter (matches v1 fallback). Skip for `coast`: a tile
        # with no Coastline (566) features must stay empty (→ NaN → 99999),
        # not fall back to arbitrary NHDLine features, which would hand inland
        # points a spurious "coastline" distance.
        for ln in layers:
            g = _read_layer_bbox(gdb_path, ln, bbox, None, target)
            if g is not None:
                parts.append(g)

    if not parts:
        result = None
    elif len(parts) == 1:
        result = parts[0]
    else:
        result = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=_CRS_M)

    if cache_path is not None and result is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            result.to_parquet(cache_path)
        except Exception:
            pass
    elif cache_path is not None and result is None:
        # Cache an empty marker so we don't redo the GDB read on warm
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            empty = gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=_CRS_M))
            empty.to_parquet(cache_path)
        except Exception:
            pass

    return result


def _compute_tile_count(extent_deg: float, grid_deg: float = _GRID_DEG) -> int:
    """Number of tiles needed to cover a 1-D extent at ``grid_deg`` resolution.

    Always returns >= 1 — for cohorts spanning less than one grid cell
    (e.g. a single county), ``np.arange(xmin, xmax + grid_deg*0.5, grid_deg)``
    used to return a single boundary value and ``xs[:-1]`` was empty, so
    no tiles were created. Every patient ended up with NaN tile_id and
    ``astype(int)`` then crashed. Locking the invariant here is a
    regression unit-level guard for the d076d4a fix.

    Non-finite extents (empty cohort → NaN bbox) collapse to 1 tile.
    """
    if not np.isfinite(extent_deg):
        return 1
    return max(1, int(np.ceil(extent_deg / grid_deg)))


def _nearest_dist(pts_m: gpd.GeoDataFrame, features_m: gpd.GeoDataFrame | None) -> np.ndarray:
    n = len(pts_m)
    if features_m is None or len(features_m) == 0:
        return np.full(n, np.nan)
    joined = gpd.sjoin_nearest(
        pts_m[["geometry"]].reset_index(drop=True),
        features_m[["geometry"]].reset_index(drop=True),
        how="left",
        distance_col="_d",
    )
    joined = joined[~joined.index.duplicated(keep="first")]
    return joined["_d"].values.astype(float)


@register_pattern("nhd_proximity")
def run_nhd_proximity(config: DatasetConfig, engine: AggregationEngine) -> Path:
    """Compute per-geoid distance to nearest NHD flow/water/area/coast feature."""
    t_total = time.perf_counter()

    patients = load_patients(config)
    pts_ll = gpd.GeoDataFrame(
        patients,
        geometry=gpd.points_from_xy(
            patients[config.buffer.long_col],
            patients[config.buffer.lat_col],
        ),
        crs=f"EPSG:{_CRS_LL}",
    )
    valid = (
        np.isfinite(pts_ll.geometry.x) & np.isfinite(pts_ll.geometry.y) &
        (pts_ll.geometry.x.abs() <= 180) & (pts_ll.geometry.y.abs() <= 90)
    )
    pts_ll = pts_ll[valid].reset_index(drop=True)
    pts_m = pts_ll.to_crs(_CRS_M)

    gdb_path = Path(config.source.file)
    cache_dir = Path(config.source.road_cache_dir) if config.source.road_cache_dir else None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        print(f"[nhd_proximity] feature cache: {cache_dir}", flush=True)

    # 1) Detect available NHD layers. When the GDB is absent (cache-only
    #    deployment: the distributed nhd_features archive is extracted and
    #    every tile hits), assume the canonical NHDPlus HR layer names — the
    #    layer list is only consulted on a cache MISS, which then raises a
    #    clear FileNotFoundError instead of silently returning no features.
    if gdb_path.exists():
        import pyogrio
        avail = [row[0] for row in pyogrio.list_layers(str(gdb_path))]
        cat_layers = _category_layers(avail)
        if not any(cat_layers.values()):
            raise ValueError(f"No expected NHD layers found in {gdb_path}")
    else:
        cat_layers = _category_layers(
            ["NetworkNHDFlowline", "NonNetworkNHDFlowline",
             "NHDWaterbody", "NHDArea"]
        )
        print(f"[nhd_proximity] GDB absent at {gdb_path} — "
              "serving from the tile cache only", flush=True)
    print(f"[nhd_proximity] layers per category: { {k: v for k, v in cat_layers.items()} }", flush=True)

    # 2) Build tile grid covering patient bbox (fixed 0.5° lat/lon).
    # Always emit at least 1 tile per axis — for cohorts spanning <0.5°
    # (e.g. a single county) np.arange would return a single boundary and
    # xs[:-1] would be empty, leaving every patient unassigned and the
    # subsequent astype(int) crashing on NaN tile_id.
    xmin, ymin, xmax, ymax = pts_ll.total_bounds
    # Snap the grid origin to the GLOBAL 0.5° lattice so a patch of ground
    # always lands in the same tile regardless of the cohort's bbox — this is
    # what makes the feature cache safely cohort-independent (it is keyed by
    # the tile's global index, see _global_tile_index / _cache_path).
    if np.isfinite(xmin):
        x0 = _snap_to_grid(xmin)
        y0 = _snap_to_grid(ymin)
        nx = _compute_tile_count(xmax - x0)
        ny = _compute_tile_count(ymax - y0)
    else:  # empty cohort → NaN bounds
        x0, y0, nx, ny = 0.0, 0.0, 1, 1
    xs = x0 + _GRID_DEG * np.arange(nx)
    ys = y0 + _GRID_DEG * np.arange(ny)
    tiles = [(i, box(x, y, x + _GRID_DEG, y + _GRID_DEG))
             for i, (x, y) in enumerate((xx, yy) for xx in xs for yy in ys)]
    grid_gdf = gpd.GeoDataFrame(
        {"tile_id": [t[0] for t in tiles]},
        geometry=[t[1] for t in tiles],
        crs=f"EPSG:{_CRS_LL}",
    )

    # Assign each patient to a tile (intersects, nearest for misses)
    sj = gpd.sjoin(
        pts_ll[["geoid", "geometry"]], grid_gdf[["tile_id", "geometry"]],
        how="left", predicate="intersects",
    )
    sj = sj[~sj.index.duplicated(keep="first")]
    pts_ll = pts_ll.copy()
    pts_ll["tile_id"] = sj["tile_id"].values
    miss = pts_ll["tile_id"].isna()
    if miss.any():
        nn = gpd.sjoin_nearest(
            pts_ll.loc[miss, ["geometry"]], grid_gdf[["tile_id", "geometry"]], how="left"
        )
        nn = nn[~nn.index.duplicated(keep="first")]
        pts_ll.loc[miss, "tile_id"] = nn["tile_id"].values
    pts_ll["tile_id"] = pts_ll["tile_id"].astype(int)
    pts_m = pts_m.copy()
    pts_m["tile_id"] = pts_ll["tile_id"].values

    tile_ids = sorted(pts_ll["tile_id"].unique())
    print(f"[nhd_proximity] {len(patients)} patients across {len(tile_ids)} tiles", flush=True)

    # 3) Main tile loop
    N = len(pts_ll)
    dists = {cat: np.full(N, np.nan) for cat in _CATEGORIES}
    timings = {"layer_load": 0.0, "sjoin_nearest": 0.0, "other": 0.0}
    cache_hits = 0
    cache_misses = 0

    for i, tid in enumerate(tile_ids):
        tile_geom = grid_gdf.loc[grid_gdf["tile_id"] == tid, "geometry"].iloc[0]
        sel = np.where(pts_ll["tile_id"].values == tid)[0]
        bbox = _tile_bbox_ll(tile_geom)
        pts_chunk = pts_m.iloc[sel]
        # Geographic cache key from the tile's SW corner (global lattice) —
        # NOT the cohort-relative tid, which collided across cohorts.
        gx, gy = _global_tile_index(tile_geom.bounds[0], tile_geom.bounds[1])

        for cat in _CATEGORIES:
            cache_path = _cache_path(cache_dir, gx, gy, cat) if cache_dir else None
            was_hit = bool(cache_path and cache_path.exists())

            t = time.perf_counter()
            features = _load_or_compute_tile_category(
                gdb_path, bbox, cat_layers[cat], cat, cache_path,
            )
            timings["layer_load"] += time.perf_counter() - t

            if was_hit:
                cache_hits += 1
            else:
                cache_misses += 1

            t = time.perf_counter()
            d = _nearest_dist(pts_chunk, features)
            timings["sjoin_nearest"] += time.perf_counter() - t

            dists[cat][sel] = d

        if (i + 1) % max(1, len(tile_ids) // 20) == 0 or (i + 1) == len(tile_ids):
            elapsed = time.perf_counter() - t_total
            print(
                f"[nhd_proximity] tile {i+1}/{len(tile_ids)} "
                f"({100*(i+1)/len(tile_ids):5.1f}%) elapsed={elapsed/60:.2f}m",
                flush=True,
            )

    # 4) Combine: dist_blue = min across categories
    t = time.perf_counter()
    stacked = np.stack([
        np.where(np.isnan(dists[cat]), np.inf, dists[cat]) for cat in _CATEGORIES
    ])
    dist_blue = np.min(stacked, axis=0)
    dist_blue[np.isinf(dist_blue)] = np.nan

    proximity = pd.DataFrame({
        "geoid":        pts_ll["geoid"].values,
        "dist_flow_m":  dists["flow"],
        "dist_water_m": dists["water"],
        "dist_area_m":  dists["area"],
        "dist_coast_m": dists["coast"],
        "dist_blue_m":  dist_blue,
    })
    timings["other"] = time.perf_counter() - t

    out = write_table(proximity, config.output.path)

    grand_total = time.perf_counter() - t_total
    measured = sum(timings.values())
    misc = max(0.0, grand_total - measured)

    print("[nhd_proximity] === SUMMARY ===", flush=True)
    for k in sorted(timings.keys(), key=lambda x: -timings[x]):
        pct = 100 * timings[k] / grand_total if grand_total > 0 else 0
        print(f"[nhd_proximity]   {k:14s} {timings[k]/60:6.2f}m ({pct:5.1f}%)", flush=True)
    misc_pct = 100 * misc / grand_total if grand_total > 0 else 0
    print(f"[nhd_proximity]   {'misc':14s} {misc/60:6.2f}m ({misc_pct:5.1f}%)", flush=True)
    print(f"[nhd_proximity]   {'total':14s} {grand_total/60:6.2f}m", flush=True)
    if cache_dir is not None:
        total = cache_hits + cache_misses
        rate = 100 * cache_hits / total if total > 0 else 0
        print(
            f"[nhd_proximity]   feature cache: hits={cache_hits} misses={cache_misses} "
            f"hit_rate={rate:.1f}%",
            flush=True,
        )

    return out
