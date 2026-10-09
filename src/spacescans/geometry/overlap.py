"""Polygon-buffer area intersection weights via membership raster + exact_extract.

Matches v1 algorithm: for each target boundary, rasterizes the target (=1) plus
all NEARBY boundaries (=0) with fill=NaN. exact_extract "mean" then gives:
    (buffer area in target) / (buffer area in ANY nearby boundary)
This is equivalent to v1's full rasterization but memory-efficient (only
boundaries near the target are included, not all 11k+ statewide).

Weights are NOT normalized — they reflect raw overlap fractions matching v1.
"""
from __future__ import annotations

from spacescans._extras import require
require("geo", "geopandas", "rasterio", "shapely", "exactextract")

import hashlib
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import rasterio.features
import rasterio.transform
from exactextract import exact_extract

# Pad around each target boundary to capture neighbors (meters in projected CRS).
# Must be >= max patient buffer radius so all relevant boundaries are included.
_NEIGHBOR_PAD_M = 500.0


def _boundary_set_signature(boundaries: gpd.GeoDataFrame, boundary_key_col: str) -> str:
    """Cheap deterministic hash of the boundary set (key column values)."""
    keys_blob = boundaries[boundary_key_col].astype(str).str.cat()
    return hashlib.md5(keys_blob.encode()).hexdigest()[:12]


def _raster_cache_path(
    cache_dir: Path, sig: str, target_key: str, raster_res_m: float, pad_m: float
) -> Path:
    safe_key = str(target_key).replace("/", "_")
    fname = f"{safe_key}_r{raster_res_m:g}_p{int(pad_m)}.npz"
    return cache_dir / sig / fname


def compute_overlap_weights(
    buffers: gpd.GeoDataFrame,
    boundaries: gpd.GeoDataFrame,
    *,
    boundary_key_col: str,
    raster_res_m: float = 25.0,
    n_workers: int = 4,
    chunk_size: int | None = None,
    resume_dir: Path | None = None,
    test_index_limit: int | None = None,
    raster_cache_dir: Path | None = None,
) -> pd.DataFrame:
    """Compute area overlap weights between patient buffers and boundary polygons.

    Returns raw overlap fractions (NOT normalized), matching v1 output format.
    Output columns: [geoid, boundary_key_col, value].

    If raster_cache_dir is given, the per-target rasterized boundary mask is cached
    on disk keyed by (boundary set signature, target key, raster_res_m, pad). This
    skips rasterize on subsequent runs (cohort-independent).
    """
    boundaries = boundaries.reset_index(drop=True)
    buffers = buffers.reset_index(drop=True)

    # Build spatial indexes for fast neighbor lookup
    sindex = boundaries.sindex
    buffers_sindex = buffers.sindex

    n_boundaries = len(boundaries)
    if test_index_limit is not None:
        n_boundaries = min(test_index_limit, n_boundaries)

    # Progress logging: report every 1% or every 50 boundaries, whichever is smaller
    log_every = max(1, min(50, n_boundaries // 100))
    t0 = time.time()
    print(
        f"[overlap] processing {n_boundaries} boundaries × {len(buffers)} buffers",
        flush=True,
    )

    boundary_sig: str | None = None
    if raster_cache_dir is not None:
        raster_cache_dir = Path(raster_cache_dir)
        boundary_sig = _boundary_set_signature(boundaries, boundary_key_col)
        (raster_cache_dir / boundary_sig).mkdir(parents=True, exist_ok=True)
        print(
            f"[overlap] raster cache enabled: {raster_cache_dir}/{boundary_sig}/",
            flush=True,
        )

    step_keys = ("sindex", "rasterize", "memfile", "exact_extract", "postprocess")
    step_totals = {k: 0.0 for k in step_keys}
    cache_hits = 0
    cache_misses = 0

    all_results: list[pd.DataFrame] = []
    for idx in range(n_boundaries):
        result, timings, cache_hit = _extract_overlap_for_boundary(
            boundaries,
            buffers,
            idx,
            boundary_key_col=boundary_key_col,
            raster_res_m=raster_res_m,
            sindex=sindex,
            buffers_sindex=buffers_sindex,
            raster_cache_dir=raster_cache_dir,
            boundary_sig=boundary_sig,
        )
        if len(result) > 0:
            all_results.append(result)
        for k in step_keys:
            step_totals[k] += timings.get(k, 0.0)
        if cache_hit is True:
            cache_hits += 1
        elif cache_hit is False:
            cache_misses += 1

        if (idx + 1) % log_every == 0 or (idx + 1) == n_boundaries:
            elapsed = time.time() - t0
            rate = (idx + 1) / elapsed
            eta_sec = (n_boundaries - (idx + 1)) / rate if rate > 0 else 0
            if elapsed > 0:
                pct = {k: 100 * step_totals[k] / elapsed for k in step_keys}
                other = max(0.0, 100 - sum(pct.values()))
                steps_str = (
                    f"rasterize={pct['rasterize']:4.1f}%  "
                    f"exact_extract={pct['exact_extract']:4.1f}%  "
                    f"sindex={pct['sindex']:4.1f}%  "
                    f"memfile={pct['memfile']:4.1f}%  "
                    f"post={pct['postprocess']:4.1f}%  "
                    f"other={other:4.1f}%"
                )
            else:
                steps_str = ""
            print(
                f"[overlap] {idx+1:>6}/{n_boundaries} "
                f"({100*(idx+1)/n_boundaries:5.1f}%)  "
                f"elapsed={elapsed/60:6.1f}m  "
                f"rate={rate:5.2f}/s  "
                f"ETA={eta_sec/60:6.1f}m  "
                f"{steps_str}",
                flush=True,
            )

    total_loop = time.time() - t0

    if not all_results:
        combined = pd.DataFrame(columns=["geoid", boundary_key_col, "value"])
        concat_s = 0.0
    else:
        t_concat = time.perf_counter()
        combined = pd.concat(all_results, ignore_index=True)
        concat_s = time.perf_counter() - t_concat

    measured = sum(step_totals.values())
    other_s = max(0.0, total_loop - measured)
    grand_total = total_loop + concat_s
    print("[overlap] === SUMMARY (main loop + concat) ===", flush=True)
    for k in sorted(step_keys, key=lambda x: -step_totals[x]):
        pct = 100 * step_totals[k] / grand_total if grand_total > 0 else 0
        print(f"[overlap]   {k:14s} {step_totals[k]/60:6.2f}m ({pct:5.1f}%)", flush=True)
    other_pct = 100 * other_s / grand_total if grand_total > 0 else 0
    concat_pct = 100 * concat_s / grand_total if grand_total > 0 else 0
    print(f"[overlap]   {'loop_other':14s} {other_s/60:6.2f}m ({other_pct:5.1f}%)", flush=True)
    print(f"[overlap]   {'pd.concat':14s} {concat_s/60:6.2f}m ({concat_pct:5.1f}%)", flush=True)
    print(f"[overlap]   {'total':14s} {grand_total/60:6.2f}m", flush=True)
    if raster_cache_dir is not None:
        total_cache = cache_hits + cache_misses
        hit_rate = 100 * cache_hits / total_cache if total_cache > 0 else 0
        print(
            f"[overlap]   raster cache: hits={cache_hits} misses={cache_misses} "
            f"hit_rate={hit_rate:.1f}%",
            flush=True,
        )

    return combined


def _extract_overlap_for_boundary(
    boundaries: gpd.GeoDataFrame,
    buffers: gpd.GeoDataFrame,
    boundary_index: int,
    *,
    boundary_key_col: str,
    raster_res_m: float,
    sindex,
    buffers_sindex,
    raster_cache_dir: Path | None = None,
    boundary_sig: str | None = None,
) -> tuple[pd.DataFrame, dict[str, float], bool | None]:
    """Build a neighborhood membership raster and extract overlap fractions.

    1. Find all boundaries whose bounding box intersects the target's padded bbox.
    2. Rasterize: target=1, neighbors=0, outside=NaN.
    3. Filter buffers to those whose bbox intersects the target bbox (huge speedup).
    4. exact_extract "mean" = (area in target) / (area in any nearby boundary).

    Returns (result_df, timings, cache_hit) where cache_hit is True/False if
    raster cache was queried, None otherwise. Timings has per-substep seconds.
    """
    timings = {"sindex": 0.0, "rasterize": 0.0, "memfile": 0.0, "exact_extract": 0.0, "postprocess": 0.0}
    cache_hit: bool | None = None

    target_geom = boundaries.geometry.iloc[boundary_index]
    if target_geom is None or target_geom.is_empty:
        return pd.DataFrame(columns=["geoid", boundary_key_col, "value"]), timings, cache_hit

    # Padded bounding box of the target
    bx = target_geom.bounds  # (minx, miny, maxx, maxy)
    pad = _NEIGHBOR_PAD_M
    query_box = (bx[0] - pad, bx[1] - pad, bx[2] + pad, bx[3] + pad)

    # Buffer sindex (cohort-dependent) — used only to decide whether to run extract
    t = time.perf_counter()
    buffer_idxs = list(buffers_sindex.intersection(query_box))
    timings["sindex"] = time.perf_counter() - t
    has_buffers = bool(buffer_idxs)

    # When caching is OFF and no buffers overlap, skip everything (legacy behavior).
    # When caching is ON, ALWAYS rasterize + cache so later cohorts can reuse it,
    # even if the current cohort has no buffers in this boundary.
    if not has_buffers and raster_cache_dir is None:
        return pd.DataFrame(columns=["geoid", boundary_key_col, "value"]), timings, cache_hit

    # Raster extent = padded target bounds (covers all neighbors in range)
    raster_bounds = query_box
    width = max(1, round((raster_bounds[2] - raster_bounds[0]) / raster_res_m))
    height = max(1, round((raster_bounds[3] - raster_bounds[1]) / raster_res_m))
    xres = (raster_bounds[2] - raster_bounds[0]) / width
    yres = (raster_bounds[3] - raster_bounds[1]) / height
    transform = rasterio.transform.from_origin(raster_bounds[0], raster_bounds[3], xres, yres)

    # Cache lookup keyed by (boundary set signature, target key, raster_res, pad)
    cache_path: Path | None = None
    if raster_cache_dir is not None and boundary_sig is not None:
        target_key_value = boundaries[boundary_key_col].iloc[boundary_index]
        cache_path = _raster_cache_path(
            raster_cache_dir, boundary_sig, target_key_value, raster_res_m, pad
        )

    arr = None
    if cache_path is not None and cache_path.exists():
        t = time.perf_counter()
        try:
            with np.load(cache_path, allow_pickle=False) as z:
                arr_loaded = z["arr"]
                transform_arr = z["transform"]
            arr = arr_loaded
            transform = rasterio.transform.Affine(*transform_arr.tolist())
            height, width = arr.shape
            cache_hit = True
        except Exception:
            arr = None  # corrupt cache; fall through to recompute
        timings["rasterize"] = time.perf_counter() - t

    if arr is None:
        # Cache miss: rasterize from scratch
        t = time.perf_counter()
        neighbor_idxs = list(sindex.intersection(query_box))
        shapes = []
        for i in neighbor_idxs:
            g = boundaries.geometry.iloc[i]
            if g is None or g.is_empty:
                continue
            shapes.append((g, 1.0 if i == boundary_index else 0.0))

        arr = rasterio.features.rasterize(
            shapes,
            out_shape=(height, width),
            transform=transform,
            fill=np.nan,
            dtype=np.float32,
        )
        timings["rasterize"] = time.perf_counter() - t

        if cache_path is not None:
            cache_hit = False
            try:
                np.savez_compressed(
                    cache_path,
                    arr=arr,
                    transform=np.array(
                        [transform.a, transform.b, transform.c,
                         transform.d, transform.e, transform.f],
                        dtype=np.float64,
                    ),
                )
            except Exception:
                pass  # don't fail run on cache write error

    # No buffers in this boundary for the current cohort — cache is now warm,
    # but there's nothing to extract for this run.
    if not has_buffers:
        return pd.DataFrame(columns=["geoid", boundary_key_col, "value"]), timings, cache_hit

    local_buffers = buffers.iloc[buffer_idxs]

    t = time.perf_counter()
    with rasterio.MemoryFile() as memfile:
        with memfile.open(
            driver="GTiff",
            height=height,
            width=width,
            count=1,
            dtype=np.float32,
            crs=boundaries.crs,
            transform=transform,
            nodata=float("nan"),
        ) as ds:
            ds.write(arr, 1)
        timings["memfile"] = time.perf_counter() - t

        t = time.perf_counter()
        with memfile.open() as ds:
            result_df = exact_extract(
                ds, local_buffers, ["mean"], include_cols=["geoid"], output="pandas",
            )
        timings["exact_extract"] = time.perf_counter() - t

    t = time.perf_counter()
    df = pd.DataFrame(result_df)
    if df.empty or "mean" not in df.columns:
        timings["postprocess"] = time.perf_counter() - t
        return pd.DataFrame(columns=["geoid", boundary_key_col, "value"]), timings, cache_hit

    df = df[df["mean"].fillna(0) > 0].copy()
    df[boundary_key_col] = boundaries[boundary_key_col].iloc[boundary_index]
    df = df.rename(columns={"mean": "value"})
    out = df[["geoid", boundary_key_col, "value"]]
    timings["postprocess"] = time.perf_counter() - t
    return out, timings, cache_hit


# ═══════════════════════════════════════════════════════════════════════════════
# FAST MODE (per-tile bulk rasterize + exact_extract with cell_id+coverage_area)
# Mirrors the "new/fast" pattern from v1 R grid scripts; replaces the per-polygon
# loop with one rasterize+extract per spatial tile.
# ═══════════════════════════════════════════════════════════════════════════════

_TILE_SIZE_M = 30000.0  # 30 km per tile in projected CRS (~1200x1200 px @ 25m)


def compute_overlap_weights_fast(
    buffers: gpd.GeoDataFrame,
    boundaries: gpd.GeoDataFrame,
    *,
    boundary_key_col: str,
    raster_res_m: float = 25.0,
    tile_size_m: float = _TILE_SIZE_M,
    test_index_limit: int | None = None,
) -> pd.DataFrame:
    """Fast boundary_overlap: per-tile bulk rasterize + chunked exact_extract.

    Algorithm (mirrors v1 R "new/fast" grid pattern, adapted for boundary layers):
      1. Build a spatial tile grid over the patient bbox.
      2. For each tile:
         a. Find boundaries intersecting tile (padded by buffer radius).
         b. Find patient buffers whose centroid falls in tile.
         c. Rasterize all boundaries to a tile-sized int32 raster, encoding
            (polygon_idx + 1) as cell value (0 = outside any polygon).
         d. exact_extract(tile_raster, buffers, [cell_id, coverage_area]).
         e. Look up polygon_idx via raster value at each cell_id.
         f. Accumulate (geoid, polygon_idx, coverage_area).
      3. Cross-tile sum (polygons spanning multiple tiles contribute pieces).
      4. Normalize: weight = coverage_area / sum_per_buffer.

    Output schema matches compute_overlap_weights (slow): [geoid, boundary_key_col, value].
    """
    boundaries = boundaries.reset_index(drop=True)
    buffers = buffers.reset_index(drop=True)

    n_boundaries = len(boundaries)
    if test_index_limit is not None:
        boundaries = boundaries.iloc[:min(test_index_limit, n_boundaries)].reset_index(drop=True)
        n_boundaries = len(boundaries)

    # Spatial indexes
    sindex_p = boundaries.sindex
    sindex_b = buffers.sindex

    # Patient bbox defines tile extent; the grid itself is anchored to a global lattice
    # (multiples of the tile size in the projected CRS), so every raster cell sits at
    # the same place whatever the cohort. Up to v0.2.6 the grid started at the cohort's
    # bounding box, which moved cell edges between cohorts: the same residence got
    # weights differing by up to ~0.03 depending on who else was linked.
    tile_size_m = max(1, round(tile_size_m / raster_res_m)) * raster_res_m
    xmin, ymin, xmax, ymax = buffers.total_bounds
    xmin = float(np.floor(xmin / tile_size_m) * tile_size_m)
    ymin = float(np.floor(ymin / tile_size_m) * tile_size_m)
    pad = _NEIGHBOR_PAD_M  # pad polygons by buffer radius

    # Build tile grid in projected CRS units (meters)
    xs = np.arange(xmin, xmax + tile_size_m, tile_size_m)
    ys = np.arange(ymin, ymax + tile_size_m, tile_size_m)
    tiles = [(x, y, x + tile_size_m, y + tile_size_m)
             for x in xs[:-1] for y in ys[:-1]]
    n_tiles = len(tiles)

    print(
        f"[overlap_fast] processing {n_boundaries} boundaries × {len(buffers)} buffers "
        f"via {n_tiles} tiles ({tile_size_m/1000:.0f} km each)",
        flush=True,
    )

    step_keys = ("tile_index", "rasterize", "memfile", "exact_extract", "postprocess")
    step_totals = {k: 0.0 for k in step_keys}
    t0 = time.time()
    log_every = max(1, n_tiles // 20)

    all_rows: list[pd.DataFrame] = []
    tiles_with_work = 0

    for t_idx, (txmin, tymin, txmax, tymax) in enumerate(tiles):
        # Step 1: find polygons + buffers in tile
        t = time.perf_counter()
        tile_query = (txmin - pad, tymin - pad, txmax + pad, tymax + pad)
        poly_idxs = list(sindex_p.intersection(tile_query))
        buf_idxs = list(sindex_b.intersection((txmin, tymin, txmax, tymax)))
        step_totals["tile_index"] += time.perf_counter() - t

        if not poly_idxs or not buf_idxs:
            continue
        tiles_with_work += 1

        # Step 2: rasterize all polygons in tile, encoding polygon_idx+1 as cell value
        t = time.perf_counter()
        width = max(1, round((txmax - txmin) / raster_res_m))
        height = max(1, round((tymax - tymin) / raster_res_m))
        transform = rasterio.transform.from_origin(txmin, tymax, raster_res_m, raster_res_m)
        shapes = []
        # Burn in a fixed order (by the boundary key): a cell on a shared edge goes to
        # the last polygon burned, so the order must not depend on the index layout.
        poly_idxs = sorted(poly_idxs, key=lambda i: str(boundaries[boundary_key_col].iloc[i]))
        for pi in poly_idxs:
            g = boundaries.geometry.iloc[pi]
            if g is None or g.is_empty:
                continue
            shapes.append((g, int(pi) + 1))  # +1 because 0 = "no polygon"
        if not shapes:
            continue
        tile_raster = rasterio.features.rasterize(
            shapes,
            out_shape=(height, width),
            transform=transform,
            fill=0,
            dtype=np.int32,
        )
        step_totals["rasterize"] += time.perf_counter() - t

        # Step 3: write to in-memory GeoTIFF
        t = time.perf_counter()
        with rasterio.MemoryFile() as memfile:
            with memfile.open(
                driver="GTiff",
                height=height,
                width=width,
                count=1,
                dtype=np.int32,
                crs=boundaries.crs,
                transform=transform,
                nodata=0,
            ) as ds:
                ds.write(tile_raster, 1)
            step_totals["memfile"] += time.perf_counter() - t

            # Step 4: exact_extract with cell_id + coverage fraction
            t = time.perf_counter()
            local_buffers = buffers.iloc[buf_idxs]
            with memfile.open() as ds:
                stats = exact_extract(
                    ds, local_buffers,
                    ["cell_id", "coverage"],
                    include_cols=["geoid"],
                    output="pandas",
                )
            step_totals["exact_extract"] += time.perf_counter() - t

        # Step 5: explode (cell_id, coverage) → lookup polygon_idx → accumulate.
        # NOTE: coverage = fraction (per-cell). Since all cells in this tile
        # have identical area (uniform resolution), per-buffer normalization
        # gives the same weights as if we used coverage_area.
        t = time.perf_counter()
        df = pd.DataFrame(stats)
        if df.empty or "cell_id" not in df.columns:
            step_totals["postprocess"] += time.perf_counter() - t
            continue
        df = df.explode(["cell_id", "coverage"])
        df = df.dropna(subset=["cell_id", "coverage"])
        df["cell_id"] = df["cell_id"].astype(np.int64)
        df["coverage"] = df["coverage"].astype(np.float64)
        # Look up polygon_idx via raster value at cell_id (flat index)
        flat = tile_raster.ravel(order="C")
        poly_id_at_cell = flat[df["cell_id"].values]
        df["poly_id_1based"] = poly_id_at_cell
        df = df[df["poly_id_1based"] > 0]  # filter out cells outside any polygon
        df["poly_idx"] = df["poly_id_1based"].astype(np.int64) - 1
        # Map poly_idx to boundary_key
        df[boundary_key_col] = boundaries[boundary_key_col].iloc[df["poly_idx"].values].values
        all_rows.append(df[["geoid", boundary_key_col, "coverage"]])
        step_totals["postprocess"] += time.perf_counter() - t

        if (t_idx + 1) % log_every == 0 or (t_idx + 1) == n_tiles:
            elapsed = time.time() - t0
            rate = (t_idx + 1) / elapsed
            eta = (n_tiles - (t_idx + 1)) / rate / 60 if rate > 0 else 0
            print(
                f"[overlap_fast] tile {t_idx+1:>5}/{n_tiles} ({100*(t_idx+1)/n_tiles:5.1f}%) "
                f"elapsed={elapsed/60:6.2f}m  rate={rate:5.1f}/s  ETA={eta:5.2f}m  "
                f"tiles_with_work={tiles_with_work}",
                flush=True,
            )

    total_loop = time.time() - t0

    if not all_rows:
        return pd.DataFrame(columns=["geoid", boundary_key_col, "value"])

    # Cross-tile aggregate + normalize per buffer
    t_concat = time.perf_counter()
    combined = pd.concat(all_rows, ignore_index=True)
    combined = combined.groupby(["geoid", boundary_key_col], as_index=False, observed=True)["coverage"].sum()
    total_per_buffer = combined.groupby("geoid")["coverage"].transform("sum")
    combined["value"] = combined["coverage"] / total_per_buffer
    out = combined[["geoid", boundary_key_col, "value"]]
    concat_s = time.perf_counter() - t_concat

    grand_total = total_loop + concat_s
    print("[overlap_fast] === SUMMARY ===", flush=True)
    for k in sorted(step_keys, key=lambda x: -step_totals[x]):
        pct = 100 * step_totals[k] / grand_total if grand_total > 0 else 0
        print(f"[overlap_fast]   {k:14s} {step_totals[k]/60:6.2f}m ({pct:5.1f}%)", flush=True)
    other_s = max(0.0, total_loop - sum(step_totals.values()))
    other_pct = 100 * other_s / grand_total if grand_total > 0 else 0
    concat_pct = 100 * concat_s / grand_total if grand_total > 0 else 0
    print(f"[overlap_fast]   {'loop_other':14s} {other_s/60:6.2f}m ({other_pct:5.1f}%)", flush=True)
    print(f"[overlap_fast]   {'concat+norm':14s} {concat_s/60:6.2f}m ({concat_pct:5.1f}%)", flush=True)
    print(f"[overlap_fast]   {'total':14s} {grand_total/60:6.2f}m", flush=True)
    print(f"[overlap_fast]   tiles processed: {tiles_with_work} / {n_tiles}", flush=True)

    return out
