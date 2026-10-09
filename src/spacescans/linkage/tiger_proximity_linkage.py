"""TIGER road proximity (C3-equivalent): per-(geoid × year) distance to nearest
primary (S1100) and secondary (S1200) road from local TIGER zip cache.

Output schema matches v1 annual_proximity.pkl (renamed for v2):
    geoid (int), year (int), dist_pri (float), dist_sec (float), dist_prisec (float)

Caching: per-(state, county, year) filtered roads → parquet on disk. The cache
is cohort-independent — any future cohort that touches the same county/year
gets a 100% hit on this step. Heavy lifting (sjoin_nearest with patients) is
cohort-specific and not cached.

Search: the distance returned is to the nearest road in the whole network, not
among the roads of the counties that happen to contain cohort points. Roads
are read per county, so each point's search starts from every county within
_SEARCH_RADIUS_M of it; a point whose nearest road lies beyond that radius is
searched again over every county within that distance, which cannot miss a
closer road. The result for one address therefore does not depend on which
other addresses are in the cohort. (Up to v0.2.6 the search pooled only the
counties containing cohort points, so an address in a county without a
primary road was measured to a road elsewhere in the cohort's counties.)
"""
from __future__ import annotations

from spacescans._extras import require
require("geo", "geopandas", "rasterio", "shapely", "exactextract")

import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from spacescans.io.spatial_readers import read_vector
from spacescans.io.writers import write_table
from spacescans.linkage.helpers import load_patients
from spacescans.models.config import DatasetConfig
from spacescans.models.protocols import AggregationEngine
from spacescans.pipeline.registry import register_pattern

_CRS_METRIC = 5070  # NAD83 / Conus Albers (meters)
_MTFCC_KEEP = ("S1100", "S1200")
_SEARCH_RADIUS_M = 25_000.0     # first-pass search radius around each point
_MAX_SEARCH_RADIUS_M = 3_000_000.0  # give up (NaN) beyond this: no road anywhere near
# 2010 county codes whose TIGER road files are published under a new code from a
# given year on: Shannon County, SD became Oglala Lakota County (46102) in 2015.
_RENAMED_COUNTIES = {("46", "113"): (2015, ("46", "102"))}


def _road_county(statefp: str, countyfp: str, year: int) -> tuple[str, str]:
    """The (state, county) code under which `year`'s road file is published."""
    renamed = _RENAMED_COUNTIES.get((statefp, countyfp))
    if renamed is not None and year >= renamed[0]:
        return renamed[1]
    return statefp, countyfp


def _filtered_roads_path(cache_dir: Path, statefp: str, countyfp: str, year: int) -> Path:
    return cache_dir / f"{year}" / f"{statefp}{countyfp}.parquet"


def _get_county_roads(
    statefp: str,
    countyfp: str,
    year: int,
    raw_dir: Path,
    cache_dir: Path | None,
) -> gpd.GeoDataFrame | None:
    """Return S1100/S1200 roads for one (county, year). Cached as parquet."""
    if cache_dir is not None:
        cache_path = _filtered_roads_path(cache_dir, statefp, countyfp, year)
        if cache_path.exists():
            try:
                return gpd.read_parquet(cache_path)
            except Exception:
                pass  # corrupt cache → fall through

    zip_path = raw_dir / f"tiger{year}_roads" / f"tl_{year}_{statefp}{countyfp}_roads.zip"
    if not zip_path.exists():
        return None
    try:
        roads = gpd.read_file(f"/vsizip/{zip_path}")
    except Exception:
        return None

    roads = roads[roads["MTFCC"].isin(_MTFCC_KEEP)][["MTFCC", "geometry"]].copy()

    if cache_dir is not None:
        cache_path = _filtered_roads_path(cache_dir, statefp, countyfp, year)
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            roads.to_parquet(cache_path)
        except Exception:
            pass  # don't fail run on cache write error

    return roads


def _nearest_dist(pts_m: gpd.GeoDataFrame, roads_m: gpd.GeoDataFrame) -> np.ndarray:
    """Distance from each point to nearest road feature (meters)."""
    if roads_m is None or len(roads_m) == 0:
        return np.full(len(pts_m), np.nan)
    joined = gpd.sjoin_nearest(
        pts_m[["geometry"]].reset_index(drop=True),
        roads_m[["geometry"]].reset_index(drop=True),
        how="left",
        distance_col="_dist",
    )
    # ties return several rows per point; take the distance per point by position
    per_point = joined["_dist"].astype(float).groupby(level=0).min()
    return per_point.reindex(range(len(pts_m))).to_numpy()


def _nearest_dist_exact(
    pts_m: gpd.GeoDataFrame,
    counties_m: gpd.GeoDataFrame,
    mtfcc: str,
    load_county: "callable",
) -> np.ndarray:
    """Distance from each point to the nearest `mtfcc` road anywhere, in metres.

    A point is resolved once its nearest road among the loaded counties is no
    farther than its search radius and every county intersecting a disk of
    that radius is loaded: a closer road would lie inside the disk, hence in a
    loaded county. Unresolved points get the found distance as their new
    radius (resolved on the next pass), or, if nothing was found, four times
    the radius. `load_county(state, county)` returns that county's roads in
    the metric CRS (possibly empty) and caches them across calls and classes.
    """
    n = len(pts_m)
    dist = np.full(n, np.nan)
    radius = np.full(n, _SEARCH_RADIUS_M)
    todo = np.arange(n)
    loaded: set[tuple[str, str]] = set()
    while len(todo):
        # 1 % margin: shapely's buffer polygon is inscribed in the true circle
        disks = gpd.GeoDataFrame(
            {"_pt": todo},
            geometry=pts_m.geometry.iloc[todo].buffer(radius[todo] * 1.01, resolution=16).values,
            crs=pts_m.crs,
        )
        hit = gpd.sjoin(disks, counties_m[["STATEFP10", "COUNTYFP10", "geometry"]],
                        how="inner", predicate="intersects")
        for sc in hit[["STATEFP10", "COUNTYFP10"]].drop_duplicates().itertuples(index=False):
            key = (sc[0], sc[1])
            if key not in loaded:
                load_county(*key)
                loaded.add(key)
        pool = [r for k in loaded for r in [load_county(*k)] if r is not None and len(r)]
        roads = (gpd.GeoDataFrame(pd.concat(pool, ignore_index=True), crs=pool[0].crs)
                 if pool else None)
        if roads is not None:
            roads = roads[roads["MTFCC"] == mtfcc]
        d = _nearest_dist(pts_m.iloc[todo], roads)
        found = np.isfinite(d)
        done = found & (d <= radius[todo])
        dist[todo[done]] = d[done]
        grow = ~done
        nxt = todo[grow]
        radius[nxt] = np.where(found[grow], d[grow], radius[nxt] * 4.0)
        give_up = radius[nxt] > _MAX_SEARCH_RADIUS_M
        todo = nxt[~give_up]
    return dist


@register_pattern("tiger_proximity")
def run_tiger_proximity(config: DatasetConfig, engine: AggregationEngine) -> Path:
    """Compute (geoid, year) × distance to nearest primary/secondary road."""
    t_total = time.perf_counter()

    patients = load_patients(config)
    pts = gpd.GeoDataFrame(
        patients,
        geometry=gpd.points_from_xy(
            patients[config.buffer.long_col],
            patients[config.buffer.lat_col],
        ),
        crs="EPSG:4326",
    )
    pts_m = pts.to_crs(_CRS_METRIC)

    # 1) County polygons: the search region is built from them (see module docstring)
    t = time.perf_counter()
    if not config.source.county_file:
        raise ValueError(
            "tiger_proximity requires `source.county_file` (path to a county boundary "
            "shapefile) to locate the counties around each patient. Set it in the YAML, "
            "e.g. source.county_file: ${SPACESCANS_DATA_DIR}/County/tl_2010_us_county10.shp"
        )
    counties_m = read_vector(config.source.county_file).to_crs(_CRS_METRIC)
    print(f"[tiger_proximity] {len(patients)} patients; {len(counties_m)} counties available",
          flush=True)
    t_county_resolve = time.perf_counter() - t

    # 2) For each year, search outward from every point over per-county roads (cache-aware)
    raw_dir = Path(config.source.file)
    cache_dir = Path(config.source.road_cache_dir) if config.source.road_cache_dir else None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        print(f"[tiger_proximity] road cache: {cache_dir}", flush=True)

    years = config.time.years if config.time else list(range(2013, 2020))

    timings = {"county_resolve": t_county_resolve, "load_filter": 0.0, "sjoin_nearest": 0.0, "concat": 0.0}
    cache_hits = 0
    cache_misses = 0

    records = []
    for y in years:
        t_year = time.perf_counter()
        year_roads: dict[tuple[str, str], gpd.GeoDataFrame | None] = {}

        def load_county(statefp: str, countyfp: str, _y=y, _store=year_roads):
            nonlocal cache_hits, cache_misses
            if (statefp, countyfp) in _store:
                return _store[(statefp, countyfp)]
            t0 = time.perf_counter()
            st, co = _road_county(statefp, countyfp, _y)
            cache_path = _filtered_roads_path(cache_dir, st, co, _y) if cache_dir else None
            if cache_path is not None and cache_path.exists():
                cache_hits += 1
            else:
                cache_misses += 1
            roads = _get_county_roads(st, co, _y, raw_dir, cache_dir)
            roads = roads.to_crs(_CRS_METRIC) if roads is not None and len(roads) else None
            _store[(statefp, countyfp)] = roads
            timings["load_filter"] += time.perf_counter() - t0
            return roads

        t = time.perf_counter()
        d_pri = _nearest_dist_exact(pts_m, counties_m, "S1100", load_county)
        d_sec = _nearest_dist_exact(pts_m, counties_m, "S1200", load_county)
        timings["sjoin_nearest"] += time.perf_counter() - t

        n_loaded = sum(1 for r in year_roads.values() if r is not None)
        if n_loaded == 0:
            print(f"[tiger_proximity] year {y}: no roads found, skipping", flush=True)
            continue

        records.append(pd.DataFrame({
            "geoid": patients[config.buffer.geoid_col].values,
            "year": y,
            "dist_primary_m": d_pri,
            "dist_secondary_m": d_sec,
        }))
        print(
            f"[tiger_proximity] year {y} done in {(time.perf_counter() - t_year)/60:.2f}m  "
            f"({len(year_roads)} counties searched, {n_loaded} with roads)",
            flush=True,
        )

    # 3) Combine + derive prisec column
    t = time.perf_counter()
    annual = pd.concat(records, ignore_index=True)
    annual["distance_prisec_m"] = annual[["dist_primary_m", "dist_secondary_m"]].min(axis=1)
    annual.loc[np.isinf(annual["distance_prisec_m"]), "distance_prisec_m"] = np.nan
    annual = annual.rename(columns={
        "dist_primary_m": "dist_pri",
        "dist_secondary_m": "dist_sec",
        "distance_prisec_m": "dist_prisec",
    })
    timings["concat"] = time.perf_counter() - t

    out = write_table(annual, config.output.path)

    grand_total = time.perf_counter() - t_total
    measured = sum(timings.values())
    other_s = max(0.0, grand_total - measured)

    print("[tiger_proximity] === SUMMARY ===", flush=True)
    for k in sorted(timings.keys(), key=lambda x: -timings[x]):
        pct = 100 * timings[k] / grand_total if grand_total > 0 else 0
        print(f"[tiger_proximity]   {k:16s} {timings[k]/60:6.2f}m ({pct:5.1f}%)", flush=True)
    other_pct = 100 * other_s / grand_total if grand_total > 0 else 0
    print(f"[tiger_proximity]   {'other':16s} {other_s/60:6.2f}m ({other_pct:5.1f}%)", flush=True)
    print(f"[tiger_proximity]   {'total':16s} {grand_total/60:6.2f}m", flush=True)
    if cache_dir is not None:
        total = cache_hits + cache_misses
        rate = 100 * cache_hits / total if total > 0 else 0
        print(
            f"[tiger_proximity]   road cache: hits={cache_hits} misses={cache_misses} "
            f"hit_rate={rate:.1f}%",
            flush=True,
        )

    return out
