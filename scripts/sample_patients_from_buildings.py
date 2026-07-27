#!/usr/bin/env python3
"""Stage 2: Sample N synthetic patients from a per-county building parquet (Stage 1 output).

Pipeline:
  1. Load buildings_<FIPS>.parquet (output of Stage 1).
  2. Uniformly sample N buildings (replace=False) with --seed for reproducibility.
  3. Assign sequential PIDs in the format 'PID0000001'..'PID<N:07d>'.
  4. Generate episodes: startDate uniform in [start_year-01-01, end_year-12-31],
     duration uniform in [min_days, max_days].
  5. Spatial-join each patient to BG shapefile for sidecar FIPS columns.
  6. Validate (state/county FIPS, unique PIDs, parseable dates, len==N, containment).
  7. Write parquet matching the schema of demo_patients_conus_fast_100000.parquet:
     pid, startDate, endDate, longitude, latitude, state_fips, county_fips, tract_geoid, bg_geoid.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point


CANONICAL_COLUMNS = [
    "pid", "startDate", "endDate", "longitude", "latitude",
    "state_fips", "county_fips", "tract_geoid", "bg_geoid",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--buildings", type=Path, required=True, help="Stage 1 derived parquet")
    p.add_argument("--n", type=int, required=True, help="cohort size")
    p.add_argument("--seed", type=int, default=42, help="RNG seed for reproducibility")
    p.add_argument("--bg-shapefile", type=Path, required=True, help="state-level BG shapefile (2010 vintage)")
    p.add_argument("--output", type=Path, required=True, help="output patient parquet")
    p.add_argument("--start-year", type=int, default=2014, help="earliest startDate year (default 2014)")
    p.add_argument("--end-year", type=int, default=2018, help="latest startDate year (default 2018)")
    p.add_argument("--min-days", type=int, default=80, help="min episode duration (default 80)")
    p.add_argument("--max-days", type=int, default=100, help="max episode duration (default 100)")
    p.add_argument("--pid-prefix", default="PID", help="PATID prefix (default 'PID')")
    p.add_argument("--expected-state-fips", required=True,
                   help="2-char state FIPS that ALL sampled patients must fall in (e.g. '12' for FL)")
    p.add_argument("--expected-county-fips", required=True,
                   help="5-char county FIPS that ALL sampled patients must fall in (e.g. '12073' for Leon)")
    return p.parse_args()


def sample_buildings(buildings_path: Path, n: int, seed: int) -> pd.DataFrame:
    df = pd.read_parquet(buildings_path)
    if len(df) < n:
        raise SystemExit(f"ERROR: only {len(df):,} buildings available, need {n:,}")
    sample = df.sample(n=n, random_state=seed, replace=False).reset_index(drop=True)
    print(f"  sampled {len(sample):,} buildings from {len(df):,} available", file=sys.stderr)
    return sample


def assign_pids(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    df = df.copy()
    df["pid"] = [f"{prefix}{i+1:07d}" for i in range(len(df))]
    return df


def generate_episodes(n: int, *, seed: int, start_year: int, end_year: int,
                     min_days: int, max_days: int) -> tuple[list[str], list[str]]:
    """Return (startDate, endDate) lists as ISO 8601 strings."""
    rng = np.random.default_rng(seed + 1)  # different stream from building sample
    start_d0 = date(start_year, 1, 1)
    start_d1 = date(end_year, 12, 31)
    n_days_in_window = (start_d1 - start_d0).days + 1

    start_offsets = rng.integers(0, n_days_in_window, size=n)
    durations = rng.integers(min_days, max_days + 1, size=n)

    start_dates: list[str] = []
    end_dates: list[str] = []
    for off, dur in zip(start_offsets, durations):
        sd = start_d0 + timedelta(days=int(off))
        ed = sd + timedelta(days=int(dur))
        start_dates.append(sd.isoformat())
        end_dates.append(ed.isoformat())
    return start_dates, end_dates


def attach_bg_sidecar_fips(df: pd.DataFrame, bg_shapefile: Path) -> pd.DataFrame:
    """Spatial-join each patient point to a BG polygon; derive state/county/tract/bg FIPS."""
    print(f"  loading BG shapefile {bg_shapefile}", file=sys.stderr)
    bg = gpd.read_file(bg_shapefile)
    if bg.crs is None or bg.crs.to_epsg() != 4326:
        bg = bg.to_crs(epsg=4326)

    pts = gpd.GeoDataFrame(
        df.assign(geometry=[Point(lon, lat) for lon, lat in zip(df["lon"], df["lat"])]),
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(pts, bg[["GEOID10", "geometry"]], how="left", predicate="within")
    # Collapse duplicates if a point lies exactly on a polygon boundary.
    joined = joined[~joined.index.duplicated(keep="first")]

    if joined["GEOID10"].isna().any():
        n_nan = joined["GEOID10"].isna().sum()
        raise SystemExit(f"ERROR: {n_nan:,} patients did not match any BG — boundary mismatch?")

    joined["state_fips"] = joined["GEOID10"].str[:2]
    joined["county_fips"] = joined["GEOID10"].str[:5]
    joined["tract_geoid"] = joined["GEOID10"].str[:11]
    joined["bg_geoid"] = joined["GEOID10"]
    return joined.drop(columns=["geometry", "GEOID10", "index_right"])


def canonicalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename lon->longitude, lat->latitude, reorder to canonical column order."""
    df = df.rename(columns={"lon": "longitude", "lat": "latitude"})
    return df[CANONICAL_COLUMNS].copy()


def validate_and_write(
    df: pd.DataFrame, *,
    expected_state_fips: str,
    expected_county_fips: str,
    expected_n: int,
    buildings_df: pd.DataFrame,
    output_path: Path,
) -> None:
    """Run all acceptance-criteria assertions on the canonicalised DataFrame, then write."""
    # Schema
    assert list(df.columns) == CANONICAL_COLUMNS, f"unexpected columns: {df.columns.tolist()}"
    # Size
    assert len(df) == expected_n, f"expected len {expected_n}, got {len(df)}"
    # FIPS
    bad_state = (df["state_fips"] != expected_state_fips).sum()
    bad_county = (df["county_fips"] != expected_county_fips).sum()
    assert bad_state == 0, f"{bad_state} patients not in state {expected_state_fips}"
    assert bad_county == 0, f"{bad_county} patients not in county {expected_county_fips}"
    # Identity uniqueness
    assert df["pid"].is_unique, "duplicate pid"
    # Date parseability
    for col in ("startDate", "endDate"):
        pd.to_datetime(df[col])  # raises if any value can't be parsed
    # Coord non-null on the canonical names (post-rename)
    assert df["longitude"].notna().all() and df["latitude"].notna().all(), "NaN coord"
    # Spec acceptance criterion #3: every patient lon/lat must equal some building centroid.
    building_centroids = set(zip(buildings_df["lon"].round(10), buildings_df["lat"].round(10)))
    patient_centroids = set(zip(df["longitude"].round(10), df["latitude"].round(10)))
    missing = patient_centroids - building_centroids
    assert not missing, f"{len(missing)} patient centroids not in buildings parquet: sample={list(missing)[:3]}"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, compression="snappy", index=False)
    print(f"  wrote {output_path} ({output_path.stat().st_size:,} bytes, {len(df):,} rows)", file=sys.stderr)


def main() -> int:
    args = parse_args()
    print(f"== Stage 2: sample {args.n:,} patients from {args.buildings} (seed={args.seed}) ==", file=sys.stderr)

    # Load once and keep a reference to the full buildings parquet for the containment assertion.
    all_buildings = pd.read_parquet(args.buildings)

    df = sample_buildings(args.buildings, n=args.n, seed=args.seed)
    df = assign_pids(df, prefix=args.pid_prefix)
    sd, ed = generate_episodes(
        args.n, seed=args.seed, start_year=args.start_year, end_year=args.end_year,
        min_days=args.min_days, max_days=args.max_days,
    )
    df["startDate"] = sd
    df["endDate"] = ed

    df = attach_bg_sidecar_fips(df, args.bg_shapefile)
    df = canonicalise_columns(df)
    validate_and_write(
        df,
        expected_state_fips=args.expected_state_fips,
        expected_county_fips=args.expected_county_fips,
        expected_n=args.n,
        buildings_df=all_buildings,
        output_path=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
