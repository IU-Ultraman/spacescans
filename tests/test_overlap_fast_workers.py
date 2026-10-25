"""compute_overlap_weights_fast gives the same weights, bit for bit, with any number of workers.

``workers`` hands the tiles with work to spawned processes; their rows come back in tile
order, so the cross-tile sum adds the same numbers in the same order. Geometry is
synthetic, in metres; a 1 km tile makes a few dozen tiles out of a small layout.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("geopandas")
pytest.importorskip("shapely")
pytest.importorskip("exactextract")

import geopandas as gpd  # noqa: E402
from shapely.geometry import Point, box  # noqa: E402

pytestmark = [pytest.mark.geo, pytest.mark.extras]

CRS = "EPSG:5070"
UNIT = 1_013          # unit edges off the 25 m lattice, so buffers straddle cells and units


def _boundaries() -> gpd.GeoDataFrame:
    # 6 x 4 units whose keys are not in index order (the burn order sorts by key)
    cells = [box(x * UNIT, y * UNIT, (x + 1) * UNIT, (y + 1) * UNIT) for x in range(6) for y in range(4)]
    keys = [f"U{k:02d}" for k in np.random.default_rng(7).permutation(len(cells))]
    return gpd.GeoDataFrame({"GEOID": keys}, geometry=cells, crs=CRS)


def _buffers(n: int = 60) -> gpd.GeoDataFrame:
    rng = np.random.default_rng(11)
    xs, ys = rng.uniform(100, 6 * UNIT - 100, n), rng.uniform(100, 4 * UNIT - 100, n)
    return gpd.GeoDataFrame({"geoid": np.arange(n)},
                            geometry=[Point(x, y).buffer(270, quad_segs=100) for x, y in zip(xs, ys)],
                            crs=CRS)


def _weights(workers) -> pd.DataFrame:
    from spacescans.geometry.overlap import compute_overlap_weights_fast

    return compute_overlap_weights_fast(_buffers(), _boundaries(), boundary_key_col="GEOID",
                                        raster_res_m=25.0, tile_size_m=1_000, workers=workers)


@pytest.mark.parametrize("workers", [2, 3])
def test_weights_do_not_depend_on_the_number_of_workers(workers):
    one = _weights(1)
    pd.testing.assert_frame_equal(one, _weights(workers), check_exact=True)
    pd.testing.assert_frame_equal(one, _weights(None), check_exact=True)
    assert len(one) > 60                          # buffers on unit edges get several weights
    assert np.allclose(one.groupby("geoid")["value"].sum(), 1.0)


def test_parallel_progress_lines_count_to_the_end(capsys):
    from spacescans.orchestrate.events import parse_progress

    _weights(2)
    lines = capsys.readouterr().out.splitlines()
    fracs = [f for f in map(parse_progress, lines) if f is not None]
    assert fracs and fracs[-1] == 1.0 and fracs == sorted(fracs)
    assert any("on 2 worker processes" in line for line in lines)


@pytest.mark.parametrize("bad", [0, 1.5, True])
def test_workers_must_be_a_positive_integer(bad):
    with pytest.raises(ValueError, match="workers"):
        _weights(bad)
