#!/usr/bin/env python3
"""Generate the synthetic nationwide cohorts every evaluation experiment shares.

Writes, under benchmarks/cohorts/:
  synth_<size>.csv        the cohort as SPACESCANS accepts it — one row per location
                          episode: pid, startDate, endDate, longitude, latitude (ISO dates)
  synth_<size>_moves.csv  the generating truth per participant — number of episodes, number
                          of moves, and the straight-line distance of each move in km
  synth_summary.csv       long format (model, metric, value): the parameters and realised
                          shares (share of participants with >1 address,
                          median move distance, share of episodes with a gap, ...)

Design (all parameters are named here and nowhere else):
  * Locations are drawn by choosing a 2010 census tract uniformly at random and then a
    uniform point inside it. Tracts are drawn to hold roughly 4,000 people, so uniform-over-
    tracts is a coarse population weighting without any population file. Only the 48
    conterminous states and DC are used (the shipped boundaries), and only tracts with
    land (ALAND10 > 0): the 293 water-only tracts (the Great Lakes, large bays) are
    excluded from the first draw and a move is accepted only if it lands in a land tract.
    The first version kept them, and 1.5 % of episodes fell in water where no source
    reports values. Points can still fall on the water part of a shoreline tract.
  * Residential history: the study window is 2013-01-01 to 2018-12-31. Each patient is
    observed for a span of 1 to 6 years starting at a random date in the first two years of
    the window. The share of patients with more than one address is P_MULTI, taken from
    the only EHR address-history validation we found (Hughes & Pruitt 2017: 64 of 100
    patients had more than one EMR-derived address). Patients with more than one address
    have 1 to 3 moves, at random times inside their span.
  * Move distance is a three-part log-normal mixture standing in for within-county,
    within-state and interstate moves (shares from the Current Population Survey
    geographic-mobility tables for 2013-2018: roughly 6 in 10 moves stay in the county,
    2 in 10 change county within the state, 2 in 10 change state). The new address must
    fall inside a tract; otherwise the draw is repeated.
  * GAP_SHARE of multi-episode patients have one unobserved gap of 30 to 180 days between
    two consecutive episodes, so that the evaluation exercises partial temporal coverage.
  * Sizes are in episodes. Patients are generated until the episode target is met.

Everything is seeded; rerunning reproduces the files byte for byte.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
# 2010 census tract boundaries per state (<state>/*.shp), as in a spacescans data directory
DEFAULT_TRACTS = Path(os.environ.get("SPACESCANS_DATA_DIR", ".")) / "TRACT" / "C3"
OUT_DIR = HERE / "cohorts"

WINDOW_START = date(2013, 1, 1)
WINDOW_END = date(2018, 12, 31)
P_MULTI = 0.64          # Hughes & Pruitt 2017: 64/100 patients had >1 EMR address
MOVES_GIVEN_MULTI = {1: 0.60, 2: 0.30, 3: 0.10}
GAP_SHARE = 0.10        # multi-episode patients with one unobserved gap
GAP_DAYS = (30, 180)
SPAN_YEARS = (1.0, 6.0)
# (share, median km, sigma of log) — within county / within state / interstate
DIST_MIX = [(0.60, 3.0, 0.9), (0.20, 40.0, 0.7), (0.20, 600.0, 0.8)]
SIZES = {"1k": 1_000, "10k": 10_000, "100k": 100_000}
SEED = 20260928

# States outside the conterminous US, excluded from sampling.
_EXCLUDE_FIPS = {"02", "15", "72"}


def load_tracts(tract_root: Path):
    import geopandas as gpd
    import pandas as pd

    frames = []
    for d in sorted(tract_root.glob("tl_2010_*_tract10")):
        fips = d.name.split("_")[2]
        if fips in _EXCLUDE_FIPS:
            continue
        shp = next(d.glob("*.shp"), None)
        if shp is None:
            continue
        g = gpd.read_file(shp, columns=["GEOID10", "ALAND10", "geometry"])
        frames.append(g[g["ALAND10"] > 0])
    if not frames:
        sys.exit(f"no tract shapefiles under {tract_root}")
    tracts = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=frames[0].crs)
    if tracts.crs is None or tracts.crs.to_epsg() != 4269:
        tracts = tracts.to_crs(4326)
    else:
        tracts = tracts.to_crs(4326)
    return tracts


def point_in_tract(rng, geom):
    """Uniform point inside a polygon by rejection sampling on its bounds."""
    from shapely import contains_xy

    minx, miny, maxx, maxy = geom.bounds
    for _ in range(200):
        x = rng.uniform(minx, maxx)
        y = rng.uniform(miny, maxy)
        if contains_xy(geom, x, y):
            return x, y
    c = geom.representative_point()
    return c.x, c.y


def offset(lon, lat, dist_km, bearing_rad):
    """Move a point by a straight-line distance on a locally flat earth."""
    dlat = (dist_km * np.cos(bearing_rad)) / 111.32
    dlon = (dist_km * np.sin(bearing_rad)) / (111.32 * np.cos(np.radians(lat)))
    return lon + dlon, lat + dlat


def draw_distance(rng):
    r = rng.random()
    acc = 0.0
    for share, median, sigma in DIST_MIX:
        acc += share
        if r <= acc:
            return float(rng.lognormal(np.log(median), sigma))
    share, median, sigma = DIST_MIX[-1]
    return float(rng.lognormal(np.log(median), sigma))


def generate(n_episodes, tracts, rng, tree, geoms):
    from shapely import Point

    rows, truth = [], []
    n_tracts = len(tracts)
    pid_no = 0
    total = 0
    while total < n_episodes:
        pid_no += 1
        pid = f"P{pid_no:07d}"
        # observation span
        start = WINDOW_START + timedelta(days=int(rng.integers(0, 2 * 365)))
        span_days = int(rng.uniform(*SPAN_YEARS) * 365.25)
        end = min(start + timedelta(days=span_days), WINDOW_END)
        if (end - start).days < 30:
            end = min(start + timedelta(days=30), WINDOW_END)
        n_moves = 0
        if rng.random() < P_MULTI:
            n_moves = int(rng.choice(list(MOVES_GIVEN_MULTI), p=list(MOVES_GIVEN_MULTI.values())))
        # move dates strictly inside the span, at least 30 days apart
        span = (end - start).days
        if n_moves and span < 30 * (n_moves + 1):
            n_moves = max(0, span // 30 - 1)
        cut_days = sorted(rng.choice(np.arange(30, span - 29), size=n_moves, replace=False).tolist()) if n_moves else []
        # first address
        t0 = int(rng.integers(0, n_tracts))
        lon, lat = point_in_tract(rng, geoms[t0])
        addresses = [(lon, lat)]
        dists = []
        for _ in range(n_moves):
            for _try in range(25):
                d = draw_distance(rng)
                nlon, nlat = offset(lon, lat, d, rng.uniform(0, 2 * np.pi))
                hit = tree.query(Point(nlon, nlat), predicate="within")
                if len(hit):
                    break
            else:
                t1 = int(rng.integers(0, n_tracts))
                nlon, nlat = point_in_tract(rng, geoms[t1])
                d = float(np.hypot((nlon - lon) * 111.32 * np.cos(np.radians(lat)), (nlat - lat) * 111.32))
            dists.append(d)
            lon, lat = nlon, nlat
            addresses.append((lon, lat))
        # episodes
        bounds = [start] + [start + timedelta(days=c) for c in cut_days] + [end]
        gap_at = None
        if n_moves and rng.random() < GAP_SHARE:
            gap_at = int(rng.integers(1, n_moves + 1))  # gap before episode index gap_at
        for i, (lon, lat) in enumerate(addresses):
            e_start = bounds[i]
            e_end = bounds[i + 1] - timedelta(days=1) if i < n_moves else bounds[i + 1]
            if gap_at is not None and i == gap_at:
                g = int(rng.integers(*GAP_DAYS))
                e_start = e_start + timedelta(days=g)
                if e_start >= e_end:
                    e_start = e_end - timedelta(days=1)
            rows.append((pid, e_start.isoformat(), e_end.isoformat(), round(lon, 6), round(lat, 6)))
        truth.append({
            "pid": pid, "n_episodes": n_moves + 1, "n_moves": n_moves,
            "has_gap": int(gap_at is not None),
            "move_km": ";".join(f"{d:.2f}" for d in dists),
            "max_move_km": f"{max(dists):.2f}" if dists else "",
            "span_days": (end - start).days,
        })
        total += n_moves + 1
    return rows[:n_episodes] if False else rows, truth


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tracts", type=Path, default=DEFAULT_TRACTS)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--sizes", default=",".join(SIZES), help="comma list of 1k,10k,100k")
    args = ap.parse_args(argv)

    from shapely import STRtree

    tracts = load_tracts(args.tracts)
    geoms = tracts.geometry.values
    tree = STRtree(geoms)
    args.out.mkdir(parents=True, exist_ok=True)

    summary = []
    for label in args.sizes.split(","):
        n = SIZES[label]
        rng = np.random.default_rng(SEED + n)
        rows, truth = generate(n, tracts, rng, tree, geoms)
        with (args.out / f"synth_{label}.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["pid", "startDate", "endDate", "longitude", "latitude"])
            w.writerows(rows)
        with (args.out / f"synth_{label}_moves.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(truth[0].keys()))
            w.writeheader()
            w.writerows(truth)
        n_pat = len(truth)
        multi = sum(1 for t in truth if t["n_moves"] > 0)
        gaps = sum(t["has_gap"] for t in truth)
        dists = [float(x) for t in truth for x in t["move_km"].split(";") if x]
        summary += [
            (label, "n_episodes", len(rows)),
            (label, "n_patients", n_pat),
            (label, "share_patients_multi_address", round(multi / n_pat, 4)),
            (label, "share_patients_with_gap", round(gaps / n_pat, 4)),
            (label, "median_move_km", round(float(np.median(dists)), 2) if dists else ""),
            (label, "share_moves_under_1km", round(float(np.mean([d < 1 for d in dists])), 4) if dists else ""),
            (label, "share_moves_over_10km", round(float(np.mean([d > 10 for d in dists])), 4) if dists else ""),
        ]
        print(f"{label}: {len(rows)} episodes, {n_pat} patients, {multi} multi-address, {gaps} with gap")
    summary += [
        ("params", "p_multi", P_MULTI), ("params", "gap_share", GAP_SHARE),
        ("params", "window_start", WINDOW_START.isoformat()), ("params", "window_end", WINDOW_END.isoformat()),
        ("params", "seed", SEED),
    ]
    with (args.out / "synth_summary.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["model", "metric", "value"])
        w.writerows(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
