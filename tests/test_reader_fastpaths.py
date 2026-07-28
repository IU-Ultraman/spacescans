"""Tests for the VNL windowed read and the TEMIS converted-parquet fast path."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("rasterio")
pytest.importorskip("pyhdf")


# ---------------------------------------------------------------------------
# VNL: windowed _read_cells_at must be exact vs naive full-array indexing
# ---------------------------------------------------------------------------


def _write_tif(path, arr):
    import rasterio
    from rasterio.transform import from_origin

    h, w = arr.shape
    with rasterio.open(
        str(path), "w", driver="GTiff", height=h, width=w, count=1,
        dtype=arr.dtype, transform=from_origin(-180, 75, 0.5, 0.5),
    ) as dst:
        dst.write(arr, 1)


def test_vnl_windowed_read_matches_naive_indexing(tmp_path):
    from spacescans.plugins.readers.vnl import _read_cells_at

    rng = np.random.default_rng(42)
    arr = rng.random((40, 60)).astype(np.float32)
    tif = tmp_path / "VNL_v21_npp_2013_global_x.tif"
    _write_tif(tif, arr)

    flat = arr.ravel(order="C").astype(np.float64)
    n = flat.size
    # scattered interior ids + corners + out-of-range on both sides
    ids = np.array([1, 2, 61, n, n // 2, 777, 1234, 0, -5, n + 1, n + 999],
                   dtype=np.int64)
    got = _read_cells_at(str(tif), ids)

    valid = (ids >= 1) & (ids <= n)
    assert np.isnan(got[~valid]).all()
    np.testing.assert_allclose(got[valid], flat[ids[valid] - 1])


def test_vnl_windowed_read_all_out_of_range(tmp_path):
    from spacescans.plugins.readers.vnl import _read_cells_at

    arr = np.ones((4, 4), dtype=np.float32)
    tif = tmp_path / "VNL_v21_npp_2014_global_x.tif"
    _write_tif(tif, arr)
    got = _read_cells_at(str(tif), np.array([0, 17, 100], dtype=np.int64))
    assert np.isnan(got).all()


# ---------------------------------------------------------------------------
# TEMIS: converter output + reader fast path vs the HDF4 slow path
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_temis(tmp_path, monkeypatch):
    """A fake raw/ tree of empty .hdf files plus a deterministic global grid
    patched over _read_hdf4_band0 (value = flat_index * 0.001 + day_of_month,
    with a NaN hole at cell 400000)."""
    import spacescans.plugins.readers.temis as T

    raw = tmp_path / "raw"
    days = ["20130101", "20130102"]
    for feat in ("uvief", "uvddc"):
        d = raw / feat / "2013"
        d.mkdir(parents=True)
        for ymd in days:
            (d / f"{feat}{ymd}.hdf").touch()

    def fake_read(hdf_path):
        day = int(str(hdf_path)[-6:-4])  # 01 / 02
        data = (np.arange(1440 * 720, dtype=np.float32) * 0.001) + day
        data[400000] = np.nan
        return data

    monkeypatch.setattr(T, "_read_hdf4_band0", fake_read)
    return raw


def test_temis_convert_and_fastpath_match_hdf_path(fake_temis, tmp_path):
    from spacescans.plugins.readers.temis import (
        _list_hdf_files,
        _load_converted_feature,
        _process_one_file,
    )
    from spacescans.tools.temis_convert import CONUS_BBOX, bbox_cell_ids, convert

    out = tmp_path / "converted"
    convert(fake_temis, out, CONUS_BBOX, features=("uvief", "uvddc"))
    assert (out / "manifest.json").exists()
    assert (out / "uvief_2013.parquet").exists()

    covered = bbox_cell_ids(CONUS_BBOX)
    keep_ids = np.sort(np.random.default_rng(7).choice(covered, 50, replace=False))
    start = pd.Timestamp("2013-01-01").date()
    end = pd.Timestamp("2013-01-02").date()

    fast = _load_converted_feature(out, "uvief", keep_ids, start, end)
    assert fast is not None

    slow_frames = [
        _process_one_file(fp, keep_ids)
        for fp in _list_hdf_files(str(fake_temis / "uvief"), start, end)
    ]
    slow = pd.concat(slow_frames, ignore_index=True)

    key = ["grid_id", "date"]
    fast_s = fast.sort_values(key).reset_index(drop=True)
    slow_s = slow[["grid_id", "value", "date"]].sort_values(key).reset_index(drop=True)
    assert len(fast_s) == len(slow_s)
    np.testing.assert_allclose(fast_s["value"], slow_s["value"], rtol=1e-6)
    assert (fast_s["grid_id"].values == slow_s["grid_id"].values).all()


def test_temis_fastpath_refuses_uncovered_cells(fake_temis, tmp_path):
    from spacescans.plugins.readers.temis import _load_converted_feature
    from spacescans.tools.temis_convert import CONUS_BBOX, convert

    out = tmp_path / "converted"
    convert(fake_temis, out, CONUS_BBOX, features=("uvief",))
    start = pd.Timestamp("2013-01-01").date()
    end = pd.Timestamp("2013-01-02").date()

    # cell 0 is far outside CONUS -> must fall back (return None), never
    # silently return partial coverage
    keep_ids = np.array([0, 500_000], dtype=int)
    assert _load_converted_feature(out, "uvief", keep_ids, start, end) is None


def test_temis_auto_converts_on_first_use(fake_temis, tmp_path):
    from spacescans.plugins.readers.temis import _ensure_converted

    conv = tmp_path / "converted"
    assert not conv.exists()
    _ensure_converted(fake_temis, conv)
    assert (conv / "manifest.json").exists()
    assert (conv / "uvief_2013.parquet").exists()

    # second call is a no-op (manifest present)
    mtime = (conv / "manifest.json").stat().st_mtime
    _ensure_converted(fake_temis, conv)
    assert (conv / "manifest.json").stat().st_mtime == mtime


def test_temis_auto_convert_noop_without_raw_dirs(tmp_path):
    from spacescans.plugins.readers.temis import _ensure_converted

    empty_raw = tmp_path / "raw"
    empty_raw.mkdir()
    conv = tmp_path / "converted"
    _ensure_converted(empty_raw, conv)
    assert not conv.exists()


def test_nhd_cache_hit_works_without_gdb(tmp_path):
    import geopandas as gpd
    from shapely.geometry import Point

    from spacescans.linkage.nhd_proximity_linkage import (
        _CRS_M,
        _load_or_compute_tile_category,
    )

    cache = tmp_path / "tile_gx-200_gy80_water.parquet"
    cached = gpd.GeoDataFrame(geometry=gpd.GeoSeries([Point(0, 0)], crs=_CRS_M))
    cached.to_parquet(cache)

    got = _load_or_compute_tile_category(
        tmp_path / "no_such.gdb", (-100.5, 40.0, -99.5, 41.0),
        ["NHDWaterbody"], "water", cache,
    )
    assert got is not None and len(got) == 1


def test_nhd_cache_miss_without_gdb_raises(tmp_path):
    import pytest as _pytest

    from spacescans.linkage.nhd_proximity_linkage import (
        _load_or_compute_tile_category,
    )

    with _pytest.raises(FileNotFoundError, match="nhd_features cache archive"):
        _load_or_compute_tile_category(
            tmp_path / "no_such.gdb", (-100.5, 40.0, -99.5, 41.0),
            ["NHDWaterbody"], "water", tmp_path / "tile_gx-200_gy80_water.parquet",
        )


def test_temis_fastpath_refuses_missing_year(fake_temis, tmp_path):
    from spacescans.plugins.readers.temis import _load_converted_feature
    from spacescans.tools.temis_convert import CONUS_BBOX, bbox_cell_ids, convert

    out = tmp_path / "converted"
    convert(fake_temis, out, CONUS_BBOX, features=("uvief",))
    keep_ids = bbox_cell_ids(CONUS_BBOX)[:5]
    start = pd.Timestamp("2013-01-01").date()
    end = pd.Timestamp("2014-06-30").date()  # 2014 not converted
    assert _load_converted_feature(out, "uvief", keep_ids, start, end) is None
