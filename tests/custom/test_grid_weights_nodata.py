"""Grid weights must not depend on the template file's own nodata (phase D2, R1-F1).

The buffer→cell coverage is a geometric fact of where the cells are. exact_extract,
run on the data raster, drops that raster's nodata cells, so a cell that is nodata in
the one template file (the first period of the first dataset on a grid, or band 1 of a
multi-band file) was never linked — in any period, band or dataset on that grid. The fix
weights a nodata-free surrogate of the grid (``extract_grid_weights(nodata_free=True)``,
set by the ``custom_grid`` template) and lets C4's ``MissingPolicy.SKIP`` drop the cells
that are nodata in each period and band.

The references here compute coverage themselves (never from the package's C3 parquet),
with nodata that varies by period and band, so a return to the template rule fails them.
"""
from __future__ import annotations

import inspect
from datetime import date

import numpy as np
import pandas as pd
import pytest

pytestmark = [pytest.mark.geo]
pytest.importorskip("exactextract")
rasterio = pytest.importorskip("rasterio")
gpd = pytest.importorskip("geopandas")
shapely = pytest.importorskip("shapely")

from spacescans import custom  # noqa: E402
from spacescans.geometry.grid_weights import extract_grid_weights  # noqa: E402

TOL = 1e-9


# ---------------------------------------------------------------------------
# unit: extract_grid_weights over a grid whose template half is nodata
# ---------------------------------------------------------------------------

def _grid_tif(path, arr, *, nodata=-9999.0, crs="EPSG:5070"):
    from rasterio.transform import from_origin

    arr = np.asarray(arr, dtype="float32")
    with rasterio.open(path, "w", driver="GTiff", width=arr.shape[1], height=arr.shape[0],
                       count=1, dtype="float32", crs=crs,
                       transform=from_origin(0, arr.shape[0] * 1000, 1000, 1000),
                       nodata=nodata) as dst:
        dst.write(arr, 1)
    return path


def test_template_nodata_excluded_without_the_flag(tmp_path):
    """The old rule: cells that are nodata in the template file get no weight."""
    arr = np.arange(16, dtype="float32").reshape(4, 4)
    arr[:, 0:2] = -9999.0                              # left half nodata
    _grid_tif(tmp_path / "t.tif", arr)
    buffers = gpd.GeoDataFrame({"geoid": [0]},
                               geometry=[shapely.geometry.box(500, 500, 3500, 3500)],
                               crs="EPSG:5070")
    w = extract_grid_weights(buffers, tmp_path / "t.tif", nodata_free=False)
    assert set(w["grid_id"]) == {2, 3, 6, 7, 10, 11, 14, 15}
    assert abs(w["weight"].sum() - 1.0) < TOL


def test_nodata_free_weights_every_covered_cell(tmp_path):
    """The fix: every cell the buffer covers gets a weight, template nodata or not."""
    arr = np.arange(16, dtype="float32").reshape(4, 4)
    arr[:, 0:2] = -9999.0
    _grid_tif(tmp_path / "t.tif", arr)
    buffers = gpd.GeoDataFrame({"geoid": [0]},
                               geometry=[shapely.geometry.box(500, 500, 3500, 3500)],
                               crs="EPSG:5070")
    w = extract_grid_weights(buffers, tmp_path / "t.tif", nodata_free=True)
    assert set(w["grid_id"]) == set(range(16))
    assert abs(w["weight"].sum() - 1.0) < TOL
    # The weights do not depend on which cells carry data: an all-valued twin gives the same.
    arr2 = np.arange(16, dtype="float32").reshape(4, 4)        # no nodata at all
    _grid_tif(tmp_path / "t2.tif", arr2)
    w2 = extract_grid_weights(buffers, tmp_path / "t2.tif", nodata_free=True)
    merged = w.merge(w2, on=["geoid", "grid_id"], suffixes=("", "_all"))
    assert len(merged) == 16
    np.testing.assert_allclose(merged["weight"], merged["weight_all"], rtol=0, atol=TOL)


# ---------------------------------------------------------------------------
# end to end: register + link a daily raster with per-day nodata
# ---------------------------------------------------------------------------

# Coarse cells (~4-5 km) so a 270 m buffer centred on a cell lies wholly inside it:
# coverage is then {that cell: 1.0} with certainty, and the reference needs no
# coverage engine at all — it reads the grid arrays directly.
WEST, NORTH, RES, N = -86.5, 40.0, 0.05, 6
DAYS = ["2016-01-01", "2016-01-02"]


def _cell_center(row: int, col: int) -> tuple[float, float]:
    return WEST + (col + 0.5) * RES, NORTH - (row + 0.5) * RES


def _daily_tif(path, arr):
    from rasterio.transform import from_origin

    with rasterio.open(path, "w", driver="GTiff", width=N, height=N, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_origin(WEST, NORTH, RES, RES),
                       nodata=-9999.0) as dst:
        dst.write(np.asarray(arr, dtype="float32"), 1)
    return path


def test_daily_raster_links_template_nodata_locations(tmp_path):
    """A location whose cell is nodata on the template day is still linked on later days."""
    from spacescans.orchestrate import link

    if "output_level" not in inspect.signature(link).parameters:
        pytest.skip("needs output_level")

    # Three locations, each a one-episode participant spanning both days.
    # A: cell (1,1) nodata on day 1 (the template), valued 10 on day 2.
    # B: cell (2,2) valued 20 on day 1, nodata on day 2.
    # C: cell (3,3) valued 5 on day 1 and 15 on day 2.
    cells = {"A": (1, 1), "B": (2, 2), "C": (3, 3)}
    day1 = np.full((N, N), -9999.0, dtype="float32")
    day2 = np.full((N, N), -9999.0, dtype="float32")
    day1[cells["B"]] = 20.0
    day1[cells["C"]] = 5.0
    day2[cells["A"]] = 10.0
    day2[cells["C"]] = 15.0
    # A's cell is nodata on day 1 (the template file): the bug would drop it everywhere.
    src = tmp_path / "src"
    src.mkdir()
    _daily_tif(src / "t_2016-01-01.tif", day1)
    _daily_tif(src / "t_2016-01-02.tif", day2)

    root = tmp_path / "datasets"
    custom.register([src / f"t_{d}.tif" for d in DAYS], name="T daily", kind="raster",
                    temporal="daily", root=root, variables=["t_daily"])

    cohort = pd.DataFrame({
        "pid": ["A", "B", "C"],
        "startDate": [DAYS[0]] * 3,
        "endDate": [DAYS[1]] * 3,
        "longitude": [_cell_center(*cells[k])[0] for k in ("A", "B", "C")],
        "latitude": [_cell_center(*cells[k])[1] for k in ("A", "B", "C")],
    })

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(custom.DATASETS_DIR_ENV, str(root))
        res = link(cohort, [], custom=["T daily"], data_dir=tmp_path / "data",
                   run_dir=tmp_path / "run", executor="inprocess", output_level="episode")

    got = res.exposures.sort_values("episode_id")["t_daily"].to_numpy(dtype="float64")

    # Reference: each buffer is inside one cell (coverage {cell: 1}); each day is 1 day of
    # the episode. value = day-weighted mean over days whose cell value is defined.
    grids = {DAYS[0]: day1, DAYS[1]: day2}

    def ref(cell):
        pairs = [(1, grids[d][cell]) for d in DAYS if grids[d][cell] != -9999.0]
        return sum(dd * z for dd, z in pairs) / sum(dd for dd, _ in pairs) if pairs else np.nan

    expected = np.array([ref(cells[k]) for k in ("A", "B", "C")])
    # A is the discriminating case: nodata on the template day, 10 on day 2.
    assert expected[0] == 10.0
    assert np.isnan(got).tolist() == np.isnan(expected).tolist(), (got, expected)
    np.testing.assert_allclose(got[~np.isnan(got)], expected[~np.isnan(expected)],
                               rtol=0, atol=TOL)


def test_multiband_static_links_band1_nodata_locations(tmp_path):
    """Band 2's value survives where band 1 (used to pick the template cells) is nodata."""
    from spacescans.orchestrate import link

    if "output_level" not in inspect.signature(link).parameters:
        pytest.skip("needs output_level")

    from rasterio.transform import from_origin

    cellA, cellB = (1, 1), (3, 3)
    b1 = np.full((N, N), -9999.0, dtype="float32")
    b2 = np.full((N, N), -9999.0, dtype="float32")
    b1[cellB] = 3.0            # band 1 valued at B, nodata at A
    b2[cellA] = 7.0            # band 2 valued at A (band-1 nodata), nodata at B
    b2[cellB] = 9.0
    src = tmp_path / "src"
    src.mkdir()
    with rasterio.open(src / "two.tif", "w", driver="GTiff", width=N, height=N, count=2,
                       dtype="float32", crs="EPSG:4326",
                       transform=from_origin(WEST, NORTH, RES, RES), nodata=-9999.0) as dst:
        dst.write(np.stack([b1, b2]))

    root = tmp_path / "datasets"
    custom.register(src / "two.tif", name="Two band", kind="raster", temporal="static",
                    root=root, variables=[{"name": "v_b1", "band": 1},
                                          {"name": "v_b2", "band": 2}])

    cohort = pd.DataFrame({
        "pid": ["A", "B"],
        "startDate": ["2016-01-01", "2016-01-01"],
        "endDate": ["2016-12-31", "2016-12-31"],
        "longitude": [_cell_center(*cellA)[0], _cell_center(*cellB)[0]],
        "latitude": [_cell_center(*cellA)[1], _cell_center(*cellB)[1]],
    })
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(custom.DATASETS_DIR_ENV, str(root))
        res = link(cohort, [], custom=["Two band"], data_dir=tmp_path / "data",
                   run_dir=tmp_path / "run", executor="inprocess", output_level="episode")

    rows = res.exposures.sort_values("episode_id")
    v_b1 = rows["v_b1"].to_numpy(dtype="float64")
    v_b2 = rows["v_b2"].to_numpy(dtype="float64")
    # A: band 1 nodata -> v_b1 NaN; band 2 = 7 (the bug left this NaN, band-1 template).
    assert np.isnan(v_b1[0]) and v_b2[0] == 7.0
    # B: band 1 = 3, band 2 = 9.
    assert v_b1[1] == 3.0 and v_b2[1] == 9.0
