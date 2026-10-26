"""engine.workers on the other catalog paths: the same output, bit for bit, with any number.

Grid weights (Noise, VIIRS VNL, TEMIS and ACAG Step 1), NHD and TIGER distances (Step 1)
and the TEMIS HDF4 reader (Step 2), each on a small synthetic world, once in this process
and once with spawned workers. Spawned workers cannot see a monkeypatch,
so every input here is a real file.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("geopandas")
pytest.importorskip("rasterio")
pytest.importorskip("exactextract")

import geopandas as gpd  # noqa: E402
from shapely.geometry import LineString, Point, Polygon, box  # noqa: E402

pytestmark = [pytest.mark.geo, pytest.mark.extras]

ALBERS = "EPSG:5070"


def _tif(path: Path, arr: np.ndarray, crs: str, origin: tuple[float, float], cell: float,
         nodata=None) -> Path:
    import rasterio
    from rasterio.transform import from_origin

    with rasterio.open(path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1,
                       dtype=arr.dtype, crs=crs, transform=from_origin(*origin, cell, cell),
                       nodata=nodata) as dst:
        dst.write(arr, 1)
    return path


# --- grid weights --------------------------------------------------------------------------

def _grid_inputs(tmp_path: Path):
    arr = np.arange(50 * 60, dtype="float32").reshape(50, 60)
    arr[10:14, 20:30] = -9999.0                                   # a nodata patch
    raster = _tif(tmp_path / "grid.tif", arr, ALBERS, (0.0, 5_000.0), 100.0, nodata=-9999.0)
    rng = np.random.default_rng(1)
    pts = rng.uniform([300, 300], [5_700, 4_700], size=(40, 2))
    buffers = gpd.GeoDataFrame({"geoid": np.arange(40), "other": "x"},
                               geometry=[Point(x, y).buffer(270, quad_segs=32) for x, y in pts], crs=ALBERS)
    return buffers, raster


@pytest.mark.parametrize("nodata_free", [False, True])
def test_grid_weights_do_not_depend_on_the_number_of_workers(tmp_path, nodata_free):
    from spacescans.geometry.grid_weights import extract_grid_weights

    buffers, raster = _grid_inputs(tmp_path)
    one = extract_grid_weights(buffers, raster, chunk_size=7, nodata_free=nodata_free)
    for workers in (2, 3):
        many = extract_grid_weights(buffers, raster, chunk_size=7, nodata_free=nodata_free, workers=workers)
        pd.testing.assert_frame_equal(one, many, check_exact=True)
    assert one["geoid"].nunique() == 40
    assert np.allclose(one.groupby("geoid")["weight"].sum(), 1.0)


def test_grid_weights_parallel_progress_reaches_every_buffer(tmp_path, capsys):
    from spacescans.geometry.grid_weights import extract_grid_weights

    buffers, raster = _grid_inputs(tmp_path)
    extract_grid_weights(buffers, raster, chunk_size=7, workers=2)
    out = capsys.readouterr().out
    assert "6 chunks on 2 worker processes" in out and "    40/40 (100.0%)" in out


# --- NHD distances ---------------------------------------------------------------------------

def _nhd_world(tmp_path: Path) -> SimpleNamespace:
    """Points over two 0.5-degree tiles, with a cached feature file per tile and category."""
    from spacescans.linkage.nhd_proximity_linkage import _CRS_M, _GRID_DEG, _global_tile_index

    rng = np.random.default_rng(2)
    lon = rng.uniform(-86.45, -85.55, 24)
    lat = rng.uniform(39.55, 39.95, 24)
    patients = tmp_path / "patients.parquet"
    pd.DataFrame({"PATID": [f"P{i}" for i in range(24)], "geoid": np.arange(24),
                  "long": lon, "lat": lat}).to_parquet(patients, index=False)
    cache = tmp_path / "nhd_cache"
    cache.mkdir()
    for sw_lon in (-86.5, -86.0):
        gx, gy = _global_tile_index(sw_lon, 39.5)
        x0 = sw_lon
        feats = {
            "flow": [LineString([(x0 + 0.1, 39.5), (x0 + 0.3, 40.0)])],
            "water": [Polygon([(x0 + 0.2, 39.7), (x0 + 0.25, 39.7), (x0 + 0.25, 39.75), (x0 + 0.2, 39.75)])],
            "area": [box(x0 + 0.4, 39.6, x0 + 0.45, 39.65)] if sw_lon == -86.5 else [],
            "coast": [] if sw_lon == -86.5 else [LineString([(x0, 39.9), (x0 + _GRID_DEG, 39.95)])],
        }
        for cat, geoms in feats.items():
            gdf = gpd.GeoDataFrame(geometry=gpd.GeoSeries(geoms, crs="EPSG:4326")).to_crs(_CRS_M)
            gdf.to_parquet(cache / f"tile_gx{gx}_gy{gy}_{cat}.parquet")
    return SimpleNamespace(patients=patients, cache=cache, gdb=tmp_path / "absent.gdb")


def _nhd_config(world, out: Path, workers):
    from spacescans.models.config import BufferConfig, DatasetConfig, EngineConfig, OutputConfig, SourceConfig

    return DatasetConfig(name="nhd_test", linkage_pattern="nhd_proximity", geometry_type="line",
                         source=SourceConfig(file=str(world.gdb), road_cache_dir=str(world.cache)),
                         buffer=BufferConfig(patient_file=str(world.patients)),
                         engine=EngineConfig(workers=workers), output=OutputConfig(path=str(out)))


def test_nhd_distances_do_not_depend_on_the_number_of_workers(tmp_path, capsys):
    from spacescans.linkage.nhd_proximity_linkage import run_nhd_proximity
    from spacescans.orchestrate.events import parse_progress

    world = _nhd_world(tmp_path)
    one = pd.read_parquet(run_nhd_proximity(_nhd_config(world, tmp_path / "one.parquet", None), None))
    capsys.readouterr()
    two = pd.read_parquet(run_nhd_proximity(_nhd_config(world, tmp_path / "two.parquet", 2), None))
    lines = capsys.readouterr().out.splitlines()
    pd.testing.assert_frame_equal(one, two, check_exact=True)
    assert len(one) == 24 and one["dist_flow_m"].notna().all() and one["dist_area_m"].isna().any()
    assert any("2 tiles on 2 worker processes" in line for line in lines)
    fracs = [f for f in map(parse_progress, lines) if f is not None]
    assert fracs and fracs[-1] == 1.0


# --- TIGER distances -------------------------------------------------------------------------

def _tiger_world(tmp_path: Path) -> SimpleNamespace:
    counties = gpd.GeoDataFrame({"STATEFP10": ["18", "18", "18"], "COUNTYFP10": ["001", "003", "005"]},
                                geometry=[box(-86.6, 39.5, -86.2, 39.9), box(-86.2, 39.5, -85.8, 39.9),
                                          box(-85.8, 39.5, -85.4, 39.9)], crs="EPSG:4269")
    county_file = tmp_path / "counties.gpkg"
    counties.to_file(county_file, driver="GPKG")
    cache = tmp_path / "roads_cache"
    for year in (2013, 2014, 2015):
        shift = 0.01 * (year - 2013)
        for fips, x in (("18001", -86.5), ("18005", -85.6)):             # 18003 has no road
            d = cache / str(year)
            d.mkdir(parents=True, exist_ok=True)
            gpd.GeoDataFrame({"MTFCC": ["S1100", "S1200"]},
                             geometry=[LineString([(x + shift, 39.5), (x + shift, 39.9)]),
                                       LineString([(x, 39.6 + shift), (x + 0.3, 39.6 + shift)])],
                             crs="EPSG:4269").to_parquet(d / f"{fips}.parquet")
    rng = np.random.default_rng(3)
    patients = tmp_path / "patients.parquet"
    pd.DataFrame({"PATID": [f"P{i}" for i in range(18)], "geoid": np.arange(18),
                  "long": rng.uniform(-86.55, -85.45, 18), "lat": rng.uniform(39.55, 39.85, 18)}
                 ).to_parquet(patients, index=False)
    return SimpleNamespace(patients=patients, cache=cache, county_file=county_file,
                           raw=tmp_path / "no_raw_zips")


def _tiger_config(world, out: Path, workers):
    from spacescans.models.config import (BufferConfig, DatasetConfig, EngineConfig, OutputConfig,
                                          SourceConfig, TimeConfig)

    return DatasetConfig(name="tiger_test", linkage_pattern="tiger_proximity", geometry_type="line",
                         source=SourceConfig(file=str(world.raw), county_file=str(world.county_file),
                                             road_cache_dir=str(world.cache)),
                         buffer=BufferConfig(patient_file=str(world.patients)),
                         time=TimeConfig(years=[2013, 2014, 2015], temporal_mode="yearly"),
                         engine=EngineConfig(workers=workers), output=OutputConfig(path=str(out)))


def test_tiger_distances_do_not_depend_on_the_number_of_workers(tmp_path, capsys):
    from spacescans.linkage.tiger_proximity_linkage import run_tiger_proximity

    world = _tiger_world(tmp_path)
    one = pd.read_parquet(run_tiger_proximity(_tiger_config(world, tmp_path / "one.parquet", None), None))
    capsys.readouterr()
    two = pd.read_parquet(run_tiger_proximity(_tiger_config(world, tmp_path / "two.parquet", 2), None))
    out = capsys.readouterr().out
    pd.testing.assert_frame_equal(one, two, check_exact=True)
    assert sorted(one["year"].unique()) == [2013, 2014, 2015] and len(one) == 3 * 18
    assert one["dist_pri"].notna().all()
    assert "3 years on 2 worker processes" in out


# --- TEMIS HDF4 reader -----------------------------------------------------------------------

def test_temis_hdf4_reader_does_not_depend_on_the_number_of_workers(tmp_path, monkeypatch):
    pytest.importorskip("pyhdf")
    from pyhdf.SD import SD, SDC

    import spacescans.plugins.readers.temis as T

    raw = tmp_path / "raw"
    days = pd.date_range("2013-01-01", periods=60, freq="D")
    for f, feat in enumerate(("uvief", "uvddc")):
        d = raw / feat / "2013"
        d.mkdir(parents=True)
        for k, day in enumerate(days):
            arr = (np.arange(20 * 40, dtype=np.int32).reshape(20, 40) * 3 + 1000 * f + k)
            arr[2, 3] = -1000                                      # the fill value
            sd = SD(str(d / f"{feat}{day:%Y%m%d}.hdf"), SDC.WRITE | SDC.CREATE)
            sds = sd.create(feat.upper(), SDC.INT32, arr.shape)
            sds[:] = arr
            sds.endaccess()
            sd.end()
    weights = tmp_path / "weights.parquet"
    pd.DataFrame({"geoid": [1, 2, 3], "grid_id": [0, 83, 799], "weight": [1.0, 1.0, 1.0]}).to_parquet(
        weights, index=False)
    patients = tmp_path / "patients.parquet"
    pd.DataFrame({"PATID": ["P1"], "start": ["2013-01-01"], "end": ["2013-03-01"]}).to_parquet(
        patients, index=False)
    monkeypatch.setattr(T, "_ensure_converted", lambda *a, **k: None)   # parent only: the HDF4 path

    def read(workers):
        cfg = SimpleNamespace(source=SimpleNamespace(file=str(weights)),
                              exposure=SimpleNamespace(file=str(raw), value_cols=["uvief", "uvddc"]),
                              buffer=SimpleNamespace(patient_file=str(patients), patient_adapter=None),
                              engine=SimpleNamespace(workers=workers))
        return T.TemisExposureSource(cfg).load_exposure()

    one = read(None)
    pd.testing.assert_frame_equal(one, read(2), check_exact=True)
    assert len(one) == 60 * 2                                      # grid_id 83 is the fill value
    assert set(one.columns) == {"grid_id", "date", "uvief", "uvddc"}
