"""tiger_proximity nearest-road search must not depend on the rest of the cohort.

Up to v0.2.6 the search pooled the roads of the counties that contain cohort
points, so a point in a county without a primary road was measured to a road
in some other cohort county, however far. `_nearest_dist_exact` searches the
counties around each point and widens the search until no closer road can
exist. Geometry here is synthetic, in metres (EPSG:5070).
"""
from __future__ import annotations

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString, Point, box

pytestmark = pytest.mark.geo

CRS = "EPSG:5070"


def _world():
    # A has no primary road; its neighbour B has one 7 km from P.
    # Z is 1,000 km away and has a primary road; Q lives there.
    # F has no road for 60 km; G, not touching F's 25 km disk, has one at 60 km from R.
    counties = gpd.GeoDataFrame(
        {"STATEFP10": ["01", "01", "02", "03", "03"],
         "COUNTYFP10": ["001", "002", "001", "001", "002"]},
        geometry=[box(0, 0, 10_000, 10_000),             # A
                  box(10_000, 0, 20_000, 10_000),        # B
                  box(1_000_000, 0, 1_010_000, 10_000),  # Z
                  box(2_000_000, 0, 2_010_000, 10_000),  # F
                  box(2_050_000, 0, 2_080_000, 10_000)], # G
        crs=CRS,
    )
    roads = {
        ("01", "001"): None,
        ("01", "002"): gpd.GeoDataFrame({"MTFCC": ["S1100"]},
                                        geometry=[LineString([(12_000, 0), (12_000, 10_000)])], crs=CRS),
        ("02", "001"): gpd.GeoDataFrame({"MTFCC": ["S1100"]},
                                        geometry=[LineString([(1_005_000, 0), (1_005_000, 10_000)])], crs=CRS),
        ("03", "001"): None,
        ("03", "002"): gpd.GeoDataFrame({"MTFCC": ["S1100"]},
                                        geometry=[LineString([(2_065_000, 0), (2_065_000, 10_000)])], crs=CRS),
    }
    calls = []

    def load_county(state, county):
        calls.append((state, county))
        return roads[(state, county)]

    return counties, load_county, calls


def _pts(*xy):
    return gpd.GeoDataFrame(geometry=[Point(x, y) for x, y in xy], crs=CRS)


def test_nearest_road_is_found_in_a_neighbouring_county():
    from spacescans.linkage.tiger_proximity_linkage import _nearest_dist_exact

    counties, load, _ = _world()
    d = _nearest_dist_exact(_pts((5_000, 5_000), (1_002_000, 5_000)), counties, "S1100", load)
    assert d[0] == pytest.approx(7_000.0)          # B's road, not Z's 1,000 km away
    assert d[1] == pytest.approx(3_000.0)


def test_distance_does_not_depend_on_other_cohort_members():
    from spacescans.linkage.tiger_proximity_linkage import _nearest_dist_exact

    counties, load, _ = _world()
    alone = _nearest_dist_exact(_pts((5_000, 5_000)), counties, "S1100", load)
    counties, load, _ = _world()
    with_q = _nearest_dist_exact(_pts((5_000, 5_000), (1_002_000, 5_000)), counties, "S1100", load)
    assert alone[0] == pytest.approx(with_q[0])


def test_search_widens_until_a_road_is_found():
    from spacescans.linkage.tiger_proximity_linkage import _nearest_dist_exact

    counties, load, calls = _world()
    d = _nearest_dist_exact(_pts((2_005_000, 5_000)), counties, "S1100", load)
    assert d[0] == pytest.approx(60_000.0)
    assert ("03", "002") in calls                   # G was reached by widening


def test_no_road_anywhere_is_missing_not_infinite():
    from spacescans.linkage.tiger_proximity_linkage import _nearest_dist_exact

    counties, load, _ = _world()
    d = _nearest_dist_exact(_pts((5_000, 5_000)), counties, "S1200", load)   # no S1200 at all
    assert np.isnan(d[0])
