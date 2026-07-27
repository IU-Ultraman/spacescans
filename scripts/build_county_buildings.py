#!/usr/bin/env python3
"""Stage 1: Build a per-county derived buildings parquet from Microsoft Global ML
Building Footprints.

Pipeline:
  1. Load county boundary from TIGER shapefile (filter by state_fips + county_fips).
  2. Compute zoom-9 quadkey tiles intersecting the boundary bbox.
  3. Resolve tile URLs from the local dataset-links CSV index (with padding sanity check).
  4. Idempotently download missing tiles to <tiles-dir>.
  5. Parse each tile's GeoJSONL, dedup by WKB across tiles, build a GeoDataFrame.
  6. Spatial clip to the county polygon (within).
  7. Reproject to a metric CRS, compute area + centroid.
  8. Apply [area_min, area_max] filter.
  9. Sort deterministically and write parquet with sequential building_id.

CLI:
    python scripts/build_county_buildings.py \\
        --county-fips 12073 --state-fips 12 \\
        --index data_full/GlobalMLBuildingFootprints/dataset-links-2026-02-03.xls \\
        --boundary data_full/County_FL/C3/tl_2010_us_county10/tl_2010_us_county10.shp \\
        --tiles-dir data_full/GlobalMLBuildingFootprints/tiles/ \\
        --output data_full/GlobalMLBuildingFootprints/derived/buildings_12073.parquet
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import shape as shapely_shape

from _building_utils import (
    filter_polygons_by_area,
    iter_geojsonl_gz,
    quadkeys_for_bbox,
    representative_lonlat,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--county-fips", required=True, help="5-char FIPS, e.g. 12073")
    p.add_argument("--state-fips", required=True, help="2-char state FIPS, e.g. 12")
    p.add_argument("--index", type=Path, required=True, help="path to dataset-links CSV (.xls extension is CSV)")
    p.add_argument("--boundary", type=Path, required=True, help="TIGER 50-state county shapefile")
    p.add_argument("--tiles-dir", type=Path, required=True, help="cache dir for downloaded .csv.gz tiles")
    p.add_argument("--output", type=Path, required=True, help="output parquet path")
    p.add_argument("--area-min", type=float, default=50.0, help="min building area in m^2 (default 50)")
    p.add_argument("--area-max", type=float, default=2000.0, help="max building area in m^2 (default 2000)")
    p.add_argument("--area-crs", default="EPSG:6346", help="equal-area CRS for metric area (default UTM 17N for FL)")
    p.add_argument("--force", action="store_true", help="rebuild even if output exists and is newer than inputs")
    return p.parse_args()


def load_boundary(boundary_path: Path, state_fips: str, county_fips: str) -> tuple[gpd.GeoSeries, tuple]:
    """Load the county polygon. Returns (GeoSeries with 1 polygon in EPSG:4326, bbox tuple)."""
    county3 = county_fips[-3:]  # COUNTYFP10 is last 3 digits of the 5-digit FIPS
    print(f"  loading boundary from {boundary_path}", file=sys.stderr)
    gdf = gpd.read_file(boundary_path)
    sel = gdf[(gdf["STATEFP10"] == state_fips) & (gdf["COUNTYFP10"] == county3)]
    if len(sel) == 0:
        raise SystemExit(f"ERROR: no county matches STATEFP10={state_fips} COUNTYFP10={county3}")
    if len(sel) > 1:
        raise SystemExit(f"ERROR: {len(sel)} counties match — expected 1")
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        sel = sel.to_crs(epsg=4326)
    bbox = sel.iloc[0].geometry.bounds
    return sel.geometry, bbox


def resolve_tile_urls(index_path: Path, quadkeys: set[str]) -> pd.DataFrame:
    """Look up URLs from the MS index CSV. Returns DataFrame[QuadKey, Url].

    Includes a padding-mismatch detector (spec Open Risk #1): if mercantile's
    quadkey strings differ in width from the index's strings, raise loudly.
    """
    idx = pd.read_csv(index_path, dtype={"QuadKey": str})
    us_idx = idx[idx["Location"] == "UnitedStates"]

    if us_idx.empty:
        raise SystemExit(
            f"ERROR: dataset-links index {index_path} has no rows with Location='UnitedStates'. "
            f"Possible causes: wrong file, MS released a new index with different region naming."
        )

    us_qk_max = int(us_idx["QuadKey"].str.len().max())
    ours_max = max((len(q) for q in quadkeys), default=0)
    if ours_max < us_qk_max:
        raise SystemExit(
            f"ERROR: quadkey padding mismatch — ours have max len {ours_max}, "
            f"US index uses len {us_qk_max}. Likely missing zfill({us_qk_max}). "
            f"Sample ours: {sorted(quadkeys)[:3]}; sample index: {sorted(us_idx['QuadKey'].head(3))}"
        )

    mask = us_idx["QuadKey"].isin(quadkeys)
    matched = us_idx.loc[mask, ["QuadKey", "Url"]].reset_index(drop=True)
    missing = quadkeys - set(matched["QuadKey"])
    if missing:
        widths = {len(q) for q in missing}
        if widths != {us_qk_max}:
            raise SystemExit(
                f"ERROR: {len(missing)} unmatched quadkeys have inconsistent widths {widths} vs index {us_qk_max}: "
                f"{sorted(missing)[:5]}"
            )
        print(f"  WARNING: {len(missing)} quadkey(s) not in US index (ocean/edge?): {sorted(missing)}", file=sys.stderr)
    print(f"  resolved {len(matched)}/{len(quadkeys)} tile URLs from index", file=sys.stderr)
    return matched


def _open_with_retry(url: str, *, timeout: int = 60, max_attempts: int = 3) -> object:
    """Open a URL with timeout and bounded retry on transient errors."""
    import time
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            return urllib.request.urlopen(url, timeout=timeout)
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < max_attempts:
                wait = 2 ** attempt
                print(f"    attempt {attempt}/{max_attempts} failed ({e}); retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
    raise SystemExit(f"ERROR: failed to download {url} after {max_attempts} attempts: {last_err}")


def download_tiles(urls_df: pd.DataFrame, tiles_dir: Path) -> list[Path]:
    """Idempotently download each tile to tiles_dir/<QK>.csv.gz. Returns list of local paths."""
    tiles_dir.mkdir(parents=True, exist_ok=True)
    out_paths: list[Path] = []
    for _, row in urls_df.iterrows():
        qk, url = row["QuadKey"], row["Url"]
        dest = tiles_dir / f"{qk}.csv.gz"
        if dest.exists() and dest.stat().st_size > 0:
            print(f"  ✓ {qk}: cached ({dest.stat().st_size:,} bytes)", file=sys.stderr)
        else:
            print(f"  → {qk}: downloading from {url}", file=sys.stderr)
            tmp = dest.with_suffix(".csv.gz.tmp")
            with _open_with_retry(url) as resp, open(tmp, "wb") as f:
                while chunk := resp.read(1 << 20):
                    f.write(chunk)
            tmp.rename(dest)
            print(f"    saved {dest.stat().st_size:,} bytes", file=sys.stderr)
        out_paths.append(dest)
    return out_paths


def load_buildings_from_tiles(tile_paths: list[Path], county_geom) -> gpd.GeoDataFrame:
    """Parse all .csv.gz tiles, dedup by WKB across tiles, concat into one GeoDataFrame,
    clip to county polygon.

    Returns GeoDataFrame with columns: geometry (Polygon, EPSG:4326), height (float), confidence (float), quadkey (str).
    """
    rows: list[dict] = []
    for tile_path in tile_paths:
        qk = tile_path.stem.replace(".csv", "")
        print(f"  parsing {tile_path.name}", file=sys.stderr)
        n_before = len(rows)
        for feature in iter_geojsonl_gz(tile_path):
            geom = shapely_shape(feature["geometry"])
            props = feature.get("properties", {})
            rows.append({
                "quadkey": qk,
                "geometry": geom,
                "height_m": float(props.get("height", -1.0)),
                "confidence": float(props.get("confidence", -1.0)),
            })
        print(f"    +{len(rows) - n_before:,} polygons", file=sys.stderr)

    gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326")
    print(f"  raw: {len(gdf):,} polygons across {len(tile_paths)} tiles", file=sys.stderr)

    # Geometry-hash dedup (protect against tile-boundary doubling: a polygon straddling two
    # adjacent quadkey tiles appears in both files).
    n_before = len(gdf)
    gdf["_wkb"] = gdf.geometry.apply(lambda g: g.wkb)
    gdf = gdf.drop_duplicates(subset="_wkb").drop(columns="_wkb").reset_index(drop=True)
    if len(gdf) < n_before:
        print(f"  dedup by geometry WKB: {n_before:,} -> {len(gdf):,} (-{n_before - len(gdf):,} duplicates)", file=sys.stderr)

    clipped = gdf[gdf.geometry.within(county_geom)].reset_index(drop=True)
    print(f"  after county clip (within): {len(clipped):,} polygons", file=sys.stderr)
    return clipped


def compute_area_and_centroid(gdf: gpd.GeoDataFrame, area_crs: str) -> gpd.GeoDataFrame:
    """Add 'area_m2', 'lon', 'lat' columns. Reprojects to area_crs for accurate area;
    centroid (with representative_point fallback for L-shapes) in WGS84.
    """
    gdf_proj = gdf.to_crs(area_crs)
    gdf = gdf.assign(area_m2=gdf_proj.geometry.area)

    lon_lat = gdf.geometry.apply(representative_lonlat)
    gdf = gdf.assign(
        lon=[ll[0] for ll in lon_lat],
        lat=[ll[1] for ll in lon_lat],
    )
    return gdf


def write_derived_parquet(gdf: gpd.GeoDataFrame, output_path: Path,
                          area_min: float, area_max: float) -> None:
    """Apply area filter, sort deterministically, assign building_id, write parquet."""
    print(f"  applying area filter [{area_min}, {area_max}] m^2", file=sys.stderr)
    mask = filter_polygons_by_area(gdf["area_m2"].tolist(), area_min=area_min, area_max=area_max)
    gdf = gdf[mask].reset_index(drop=True)
    print(f"  after area filter: {len(gdf):,} polygons", file=sys.stderr)

    # Deterministic sort for reproducible building_id assignment.
    gdf = gdf.sort_values(["quadkey", "lon", "lat"]).reset_index(drop=True)
    gdf["building_id"] = range(len(gdf))

    out_df = gdf[["building_id", "lon", "lat", "area_m2", "height_m", "confidence", "quadkey"]].copy()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_parquet(output_path, compression="snappy", index=False)
    print(f"  wrote {output_path} ({output_path.stat().st_size:,} bytes, {len(out_df):,} rows)", file=sys.stderr)


def _is_up_to_date(output: Path, boundary: Path, tile_paths: list[Path]) -> bool:
    """Spec § 'Idempotency': chained inequality
        output mtime > max(tile mtimes) >= boundary mtime  AND  all tiles exist on disk.
    """
    if not output.exists():
        return False
    if not tile_paths or not all(t.exists() for t in tile_paths):
        return False
    out_m = output.stat().st_mtime
    tile_m = max(t.stat().st_mtime for t in tile_paths)
    bnd_m = boundary.stat().st_mtime
    return (out_m > tile_m) and (tile_m >= bnd_m)


def main() -> int:
    args = parse_args()

    print(f"== Stage 1: county buildings for {args.state_fips}/{args.county_fips} ==", file=sys.stderr)

    if args.state_fips != "12" and args.area_crs == "EPSG:6346":
        print(
            f"  WARNING: --area-crs defaults to EPSG:6346 (FL UTM 17N) which is inaccurate "
            f"outside FL. For state_fips={args.state_fips!r}, pass an appropriate equal-area "
            f"CRS (e.g. EPSG:5070 for CONUS Albers, or the local UTM zone).",
            file=sys.stderr,
        )

    geom_series, bbox = load_boundary(args.boundary, args.state_fips, args.county_fips)
    county_geom = geom_series.iloc[0]
    print(f"  bbox: {bbox}", file=sys.stderr)

    qks = quadkeys_for_bbox(bbox, zoom=9)
    print(f"  zoom-9 quadkeys: {sorted(qks)}", file=sys.stderr)

    urls_df = resolve_tile_urls(args.index, qks)
    expected_tile_paths = [args.tiles_dir / f"{qk}.csv.gz" for qk in urls_df["QuadKey"]]

    if not args.force and _is_up_to_date(args.output, args.boundary, expected_tile_paths):
        print(f"  ✓ {args.output} is up to date (use --force to rebuild)", file=sys.stderr)
        return 0

    tile_paths = download_tiles(urls_df, args.tiles_dir)
    gdf = load_buildings_from_tiles(tile_paths, county_geom)
    gdf = compute_area_and_centroid(gdf, args.area_crs)
    write_derived_parquet(gdf, args.output, args.area_min, args.area_max)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
