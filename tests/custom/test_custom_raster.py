"""GeoTIFF inspection and pixel read-back by the pipeline's grid_id convention.

Ported from spacescans-web ``backend/tests/test_raster_values.py``. Every
rejection is a run that would otherwise fail late or link nothing: no CRS ->
grid_weights cannot reproject the buffers; bottom-up -> exactextract refuses it
("Incompatible extents", the ACAG incident); off-CONUS -> 0 % linked.
"""
from __future__ import annotations

import numpy as np
import pytest

pytestmark = pytest.mark.geo
rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin  # noqa: E402

from spacescans.custom import raster as rv  # noqa: E402
from spacescans.custom import RasterError  # noqa: E402


def _grid(rows=4, cols=5):
    """value = row * 10 + col, so a pixel's value says where it is."""
    return np.array([[r * 10 + c for c in range(cols)] for r in range(rows)])


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------

def test_inspect_describes_a_good_raster(tmp_path, write_tif):
    p = write_tif(tmp_path / "ok.tif", _grid(), nodata=-9999)
    meta = rv.inspect_raster(p.read_bytes())
    assert (meta["width"], meta["height"], meta["cells"]) == (5, 4, 20)
    assert meta["crs"] == "EPSG:4326"
    assert meta["band_count"] == 1
    assert meta["nodata"] == -9999.0
    assert meta["resolution_label"] == "0.01°"
    w, s, e, n = meta["bounds_wgs84"]
    assert (round(w, 2), round(n, 2)) == (-85.0, 31.0)
    assert len(meta["grid_hash"]) == 8


def test_inspect_accepts_a_path_as_well_as_bytes(tmp_path, write_tif):
    p = write_tif(tmp_path / "ok.tif", _grid())
    assert rv.inspect_raster(p) == rv.inspect_raster(p.read_bytes()) == rv.inspect_raster(str(p))


def test_inspect_rejects_a_raster_without_crs(tmp_path, write_tif):
    p = write_tif(tmp_path / "nocrs.tif", _grid(), crs=None)
    with pytest.raises(RasterError, match="no coordinate reference system"):
        rv.inspect_raster(p.read_bytes())


def test_inspect_rejects_a_bottom_up_raster(tmp_path, write_tif):
    p = write_tif(tmp_path / "south.tif", _grid(), bottom_up=True)
    with pytest.raises(RasterError, match="bottom-up"):
        rv.inspect_raster(p.read_bytes())


def test_inspect_rejects_a_raster_off_the_continental_us(tmp_path, write_tif):
    p = write_tif(tmp_path / "europe.tif", _grid(), west=10.0, north=50.0)
    with pytest.raises(RasterError, match="does not touch the continental US"):
        rv.inspect_raster(p.read_bytes())


def test_inspect_rejects_something_that_is_not_a_raster():
    with pytest.raises(RasterError, match="not a raster"):
        rv.inspect_raster(b"tract,value\n12073001100,0.4\n")


def test_inspect_lists_bands(tmp_path, write_tif):
    p = write_tif(tmp_path / "multi.tif", np.stack([_grid(), _grid() + 100, _grid() + 200]))
    meta = rv.inspect_raster(p.read_bytes())
    assert meta["band_count"] == 3
    assert [b["index"] for b in meta["bands"]] == [1, 2, 3]


def test_inspect_lists_each_bands_nodata(tmp_path, write_tif):
    p = write_tif(tmp_path / "m.tif", np.stack([_grid(), _grid()]), nodata=-9999)
    assert [b["nodata"] for b in rv.inspect_raster(p)["bands"]] == [-9999.0, -9999.0]


def test_projected_resolution_label(tmp_path, write_tif):
    p = write_tif(tmp_path / "km.tif", _grid(), west=-2_000_000, north=1_000_000,
                  res=1000, crs="EPSG:5070")
    assert rv.inspect_raster(p.read_bytes())["resolution_label"] == "1 km"
    p = write_tif(tmp_path / "m.tif", _grid(), west=-2_000_000, north=1_000_000,
                  res=250, crs="EPSG:5070")
    assert rv.inspect_raster(p.read_bytes())["resolution_label"] == "250 m"


# --------------------------------------------------------------------------
# grid identity
# --------------------------------------------------------------------------

def test_grid_hash_ignores_pixel_values(tmp_path, write_tif):
    """Two years of one dataset differ in every pixel and share one C3."""
    a = rv.inspect_raster(write_tif(tmp_path / "a.tif", _grid()).read_bytes())
    b = rv.inspect_raster(write_tif(tmp_path / "b.tif", _grid() * 7 + 3).read_bytes())
    assert rv.same_grid(a, b)
    assert a["grid_hash"] == b["grid_hash"]


@pytest.mark.parametrize("change", [
    dict(res=0.02), dict(west=-85.01), dict(north=31.5), dict(crs="EPSG:4269"),
])
def test_grid_hash_changes_with_the_grid(tmp_path, write_tif, change):
    base = rv.inspect_raster(write_tif(tmp_path / "a.tif", _grid()).read_bytes())
    other = rv.inspect_raster(write_tif(tmp_path / "b.tif", _grid(), **change).read_bytes())
    assert not rv.same_grid(base, other)


def test_grid_hash_changes_with_shape(tmp_path, write_tif):
    base = rv.inspect_raster(write_tif(tmp_path / "a.tif", _grid(4, 5)).read_bytes())
    other = rv.inspect_raster(write_tif(tmp_path / "b.tif", _grid(4, 6)).read_bytes())
    assert not rv.same_grid(base, other)


def test_grid_hash_is_the_webs_formula():
    meta = {"crs": "EPSG:4326", "transform": [0.05, 0.0, -125.0, 0.0, -0.05, 50.0],
            "width": 1180, "height": 520}
    # The example rasters' grid (custom_exposome_examples, R1 config.json).
    assert rv.grid_hash(meta) == "745b4463"


# --------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------

def test_extract_follows_the_row_major_grid_id_convention(tmp_path, write_tif):
    """grid_id = row * width + col, 0-based: what grid_weights emits with
    grid_id_offset 0 and what the noise reader inverts."""
    p = write_tif(tmp_path / "g.tif", _grid(4, 5))
    vals = rv.extract_values(p, np.array([0, 4, 5, 13, 19]))   # (0,0) (0,4) (1,0) (2,3) (3,4)
    assert vals.tolist() == [0.0, 4.0, 10.0, 23.0, 34.0]


def test_extract_turns_nodata_into_nan(tmp_path, write_tif):
    g = _grid(4, 5).astype(float)
    g[1, 2] = -9999
    p = write_tif(tmp_path / "nd.tif", g, nodata=-9999)
    vals = rv.extract_values(p, np.array([7, 8]))    # (1,2) is nodata, (1,3) is 13
    assert np.isnan(vals[0]) and vals[1] == 13.0


def test_extract_gives_nan_outside_the_raster(tmp_path, write_tif):
    p = write_tif(tmp_path / "g.tif", _grid(4, 5))
    vals = rv.extract_values(p, np.array([-1, 0, 20, 10_000]))
    assert np.isnan(vals[0]) and vals[1] == 0.0 and np.isnan(vals[2]) and np.isnan(vals[3])


def test_extract_reads_the_requested_band(tmp_path, write_tif):
    p = write_tif(tmp_path / "multi.tif", np.stack([_grid(), _grid() + 100]))
    assert rv.extract_values(p, np.array([13]), band=1).tolist() == [23.0]
    assert rv.extract_values(p, np.array([13]), band=2).tolist() == [123.0]
    with pytest.raises(RasterError, match="band 3 does not exist"):
        rv.extract_values(p, np.array([0]), band=3)


def test_extract_with_no_ids_returns_empty(tmp_path, write_tif):
    p = write_tif(tmp_path / "g.tif", _grid())
    assert rv.extract_values(p, np.array([], dtype="int64")).shape == (0,)


def _tiled(path, stack, **kw):
    stack = np.asarray(stack, dtype="float32")
    if stack.ndim == 2:
        stack = stack[None]
    with rasterio.open(
        path, "w", driver="GTiff", width=stack.shape[2], height=stack.shape[1],
        count=stack.shape[0], dtype="float32", crs="EPSG:4326",
        transform=from_origin(-85.0, 31.0, 0.01, 0.01), nodata=-1, tiled=True,
        blockxsize=16, blockysize=16, **kw,
    ) as dst:
        dst.write(stack)
    return path


def test_blockwise_read_matches_full_read(tmp_path, monkeypatch):
    """Force the block path on a small raster and compare against the simple
    full read: the two must agree cell for cell, including nodata."""
    g = _grid(40, 50).astype(float)
    g[7, 9] = -1
    p = _tiled(tmp_path / "tiled.tif", g)
    ids = np.array([0, 49, 7 * 50 + 9, 20 * 50 + 33, 39 * 50 + 49, 5000])
    full = rv.extract_values(p, ids)
    monkeypatch.setattr(rv, "FULL_READ_MAX_CELLS", 10)     # 2,000 cells > 10 -> blocks
    blocks = rv.extract_values(p, ids)
    assert np.array_equal(np.isnan(full), np.isnan(blocks))
    assert np.allclose(full[~np.isnan(full)], blocks[~np.isnan(blocks)])
    assert np.isnan(blocks[2]) and np.isnan(blocks[5])       # nodata and out of range
    assert blocks[3] == 20 * 10 + 33


def test_the_block_threshold_can_be_passed_in(tmp_path):
    """The web's re-export shim passes its own FULL_READ_MAX_CELLS this way."""
    p = _tiled(tmp_path / "tiled.tif", _grid(40, 50))
    ids = np.array([0, 49, 20 * 50 + 33, 5000])
    full = rv.extract_bands(p, ids, bands=[1])
    blocks = rv.extract_bands(p, ids, bands=[1], full_read_max_cells=10)
    assert np.array_equal(full, blocks, equal_nan=True)


def test_extract_bands_returns_one_column_per_band_in_the_requested_order(tmp_path, write_tif):
    p = write_tif(tmp_path / "m.tif", np.stack([_grid(), _grid() + 100, _grid() + 200]))
    vals = rv.extract_bands(p, np.array([0, 13]), bands=[3, 1])
    assert vals.shape == (2, 2)
    assert vals.tolist() == [[200.0, 0.0], [223.0, 23.0]]


def test_extract_values_is_extract_bands_with_one_band(tmp_path, write_tif):
    p = write_tif(tmp_path / "m.tif", np.stack([_grid(), _grid() + 100]))
    ids = np.array([0, 7, 19, 25])
    assert np.array_equal(rv.extract_values(p, ids, band=2),
                          rv.extract_bands(p, ids, bands=[2])[:, 0], equal_nan=True)


def test_extract_bands_applies_nodata_per_band(tmp_path, write_tif):
    """A list of nodata values applies band by band; None falls back to the
    file's own value."""
    a = _grid().astype(float)
    b = _grid().astype(float) + 100
    a[0, 0] = -9999                       # the file's nodata, in band 1
    b[0, 1] = -1                          # a sentinel the caller names for band 2
    p = write_tif(tmp_path / "m.tif", np.stack([a, b]), nodata=-9999)
    vals = rv.extract_bands(p, np.array([0, 1]), bands=[1, 2], nodata=[None, -1])
    assert np.isnan(vals[0, 0]) and vals[1, 0] == 1.0
    assert vals[0, 1] == 100.0 and np.isnan(vals[1, 1])
    with pytest.raises(RasterError, match="2 nodata values for 1 bands"):
        rv.extract_bands(p, np.array([0]), bands=[1], nodata=[None, None])


def test_extract_bands_rejects_a_missing_band(tmp_path, write_tif):
    p = write_tif(tmp_path / "m.tif", np.stack([_grid(), _grid()]))
    with pytest.raises(RasterError, match="band 3 does not exist"):
        rv.extract_bands(p, np.array([0]), bands=[1, 3])
    with pytest.raises(RasterError, match="no band requested"):
        rv.extract_bands(p, np.array([0]), bands=[])


@pytest.mark.parametrize("interleave", ["pixel", "band"])
def test_blockwise_multi_band_read_matches_full_read(tmp_path, monkeypatch, interleave):
    stack = np.stack([_grid(40, 50), _grid(40, 50) + 1000, _grid(40, 50) + 2000]).astype("float32")
    stack[1, 7, 9] = -1
    p = _tiled(tmp_path / f"tiled_{interleave}.tif", stack, interleave=interleave)
    ids = np.array([0, 49, 7 * 50 + 9, 20 * 50 + 33, 39 * 50 + 49, 5000])
    full = rv.extract_bands(p, ids, bands=[2, 3, 1])
    monkeypatch.setattr(rv, "FULL_READ_MAX_CELLS", 10)
    blocks = rv.extract_bands(p, ids, bands=[2, 3, 1])
    assert np.array_equal(full, blocks, equal_nan=True)
    assert np.isnan(blocks[2, 0]) and blocks[2, 1] == 2000 + 79   # nodata only in band 2
    assert blocks[3].tolist() == [1233.0, 2233.0, 233.0]
    assert np.isnan(blocks[5]).all()                                # out of range
