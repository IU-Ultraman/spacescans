"""compute_overlap_weights_fast must give a residence the same weights in any cohort.

Up to v0.2.6 the 25 m raster grid started at the cohort's bounding box, so adding
one far-away patient moved every cell edge and changed the area weights of
everyone else (by up to ~0.03 on real block groups). The grid is now anchored to
a global lattice. Geometry here is synthetic, in metres.
"""
from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, box

pytestmark = pytest.mark.geo

CRS = "EPSG:5070"


def _boundaries():
    # two units whose shared edge runs through the first buffer at a non-lattice offset
    return gpd.GeoDataFrame(
        {"GEOID": ["A", "B"]},
        geometry=[box(0, 0, 1_013, 2_000), box(1_013, 0, 3_000, 2_000)],
        crs=CRS,
    )


def _buffers(*centres):
    return gpd.GeoDataFrame(
        {"geoid": list(range(len(centres)))},
        geometry=[Point(x, y).buffer(270, resolution=100) for x, y in centres],
        crs=CRS,
    )


def _weights_of_first(buffers):
    from spacescans.geometry.overlap import compute_overlap_weights_fast

    w = compute_overlap_weights_fast(buffers, _boundaries(), boundary_key_col="GEOID", raster_res_m=25.0)
    return w[w["geoid"] == 0].set_index("GEOID")["value"].sort_index()


def test_weights_do_not_depend_on_the_rest_of_the_cohort():
    alone = _weights_of_first(_buffers((1_000, 1_000)))
    # a second patient 7.3 km away shifts the cohort's bounding box by a non-multiple of 25 m
    with_other = _weights_of_first(_buffers((1_000, 1_000), (-7_311, -4_127)))
    pd.testing.assert_series_equal(alone, with_other)
    assert set(alone.index) == {"A", "B"}
    assert alone.sum() == pytest.approx(1.0)
