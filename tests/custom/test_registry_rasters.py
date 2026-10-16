"""Registered rasters: static, yearly, biweekly and daily GeoTIFF series on one grid."""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from spacescans import custom

pytestmark = [pytest.mark.geo]
pytest.importorskip("rasterio")


@pytest.fixture
def root(tmp_path):
    return tmp_path / "datasets"


def _tifs(write_tif, folder, names, *, bands=1, value=None, **kw):
    folder.mkdir(exist_ok=True)
    out = []
    for i, name in enumerate(names):
        arr = np.full((bands, 6, 6), float(i + 1) if value is None else value, dtype="float32")
        out.append(write_tif(folder / name, arr, west=-86.5, north=40.0, **kw))
    return out


def test_a_static_raster(tmp_path, root, write_tif):
    (tif,) = _tifs(write_tif, tmp_path / "in", ["ndvi.tif"], bands=3, nodata=-1)
    rec = custom.register(tif, name="NDVI static", kind="raster", temporal="static", root=root,
                          variables=[{"name": "ndvi", "band": 2, "unit": "index"},
                                     {"name": "evi", "band": 3}])
    assert rec.files == ["rasters/00001_ndvi.tif", "periods.json"]
    assert rec.key is None
    assert rec.variables == [{"name": "ndvi", "unit": "index", "description": None},
                             {"name": "evi", "unit": None, "description": None}]
    copy = rec.path("rasters/00001_ndvi.tif")
    assert copy.read_bytes() == tif.read_bytes()
    assert rec.checksum == hashlib.sha256(tif.read_bytes()).hexdigest()
    d = rec.details
    assert d["bands"] == [2, 3] and d["band_count"] == 3 and d["file_count"] == 1
    assert d["grid"]["width"] == 6 and d["grid"]["crs"] == "EPSG:4326"
    index = json.loads(rec.path("periods.json").read_text())
    assert index == {"temporal": "static",
                     "files": [{"file": "rasters/00001_ndvi.tif", "source_name": "ndvi.tif",
                                "sha256": rec.checksum, "bytes": tif.stat().st_size}]}

    definition = rec.definition()
    assert (definition["geometry"], definition["spatial_method"], definition["boundary"]) == (
        "raster", "grid", "Point")
    assert definition["bands"] == [2, 3] and definition["band_nodata"] == [None, None]
    assert definition["value_cols"] == ["ndvi", "evi"]
    assert definition["grid_hash"] == d["grid_hash"]
    assert definition["rasters"] == [{"path": str(copy), "year": None,
                                      "uploaded_filename": "ndvi.tif", "sha256": rec.checksum,
                                      "bytes": tif.stat().st_size}]
    assert custom.check_definition(definition, key=rec.variable_key, deep=True)["year_col"] is None
    assert custom.raster_bands(definition) == (["ndvi", "evi"], [2, 3], [None, None])


def test_a_static_raster_is_one_file_without_a_period(tmp_path, root, write_tif):
    a, b = _tifs(write_tif, tmp_path / "in", ["a.tif", "b.tif"])
    with pytest.raises(custom.DatasetError, match="exactly one GeoTIFF"):
        custom.register([a, b], name="x", kind="raster", temporal="static", root=root)
    with pytest.raises(custom.DatasetError, match="has no period"):
        custom.register({a: 2016}, name="x", kind="raster", temporal="static", root=root)
    with pytest.raises(custom.DatasetError, match="takes no key"):
        custom.register(a, name="x", kind="raster", temporal="static", key="tract", root=root)


def test_the_default_variable_is_band_one_named_after_the_dataset(tmp_path, root, write_tif):
    (tif,) = _tifs(write_tif, tmp_path / "in", ["x.tif"])
    rec = custom.register(tif, name="Night lights 2016", kind="raster", temporal="static",
                          root=root)
    assert rec.variables == [{"name": "night_lights_2016", "unit": None, "description": None}]
    assert rec.details["bands"] == [1]


def test_yearly_rasters_from_names_are_stored_in_year_order(tmp_path, root, write_tif):
    files = _tifs(write_tif, tmp_path / "in", ["ndvi_2017.tif", "ndvi_2015.tif"])
    rec = custom.register(files, name="ndvi", kind="raster", temporal="yearly", root=root)
    index = json.loads(rec.path("periods.json").read_text())["files"]
    assert [(e["file"], e["year"], e["source_name"]) for e in index] == [
        ("rasters/00001_ndvi_2015.tif", 2015, "ndvi_2015.tif"),
        ("rasters/00002_ndvi_2017.tif", 2017, "ndvi_2017.tif")]
    assert rec.details["coverage_years"] == [2015, 2017]
    # the checksum covers the originals in period order
    joined = files[1].read_bytes() + files[0].read_bytes()
    assert rec.checksum == hashlib.sha256(joined).hexdigest()
    d = rec.definition()
    assert d["year_col"] == "year" and d["coverage_years"] == [2015, 2017]
    assert [r["year"] for r in d["rasters"]] == [2015, 2017]
    custom.check_definition(d, key=rec.variable_key, deep=True)


def test_biweekly_rasters_get_periods_and_a_definition_with_dates(tmp_path, root, write_tif):
    names = ["pm_20160116.tif", "pm_20160101.tif", "pm_20160201.tif"]
    files = _tifs(write_tif, tmp_path / "in", names)
    rec = custom.register(files, name="PM biweekly", kind="raster", temporal="biweekly",
                          root=root)
    index = json.loads(rec.path("periods.json").read_text())["files"]
    assert [(e["start"], e["end"]) for e in index] == [
        ("2016-01-01", "2016-01-15"), ("2016-01-16", "2016-01-31"), ("2016-02-01", "2016-02-15")]
    assert rec.details["coverage_dates"] == ["2016-01-01", "2016-02-15"]
    d = rec.definition()
    assert d["temporal"] == "biweekly" and d["year_col"] is None
    assert d["coverage_dates"] == ["2016-01-01", "2016-02-15"]
    assert [(r["start"], r["end"], r["year"]) for r in d["rasters"]][0] == (
        "2016-01-01", "2016-01-15", None)
    custom.check_definition(d, key=rec.variable_key, deep=True)
    broken = {**d, "rasters": [{**d["rasters"][0], "start": "soon"}]}
    with pytest.raises(custom.CustomExposomeError, match="ISO 'start' and 'end'"):
        custom.check_definition(broken, files=False)


def test_daily_rasters_from_a_manifest(tmp_path, root, write_tif):
    files = _tifs(write_tif, tmp_path / "in", ["a.tif", "b.tif"])
    manifest = tmp_path / "in" / "dates.csv"
    manifest.write_text("file,date\na.tif,2016-03-02\nb.tif,2016-03-01\n")
    rec = custom.register([*files, manifest], name="daily", kind="raster", temporal="daily",
                          root=root)
    index = json.loads(rec.path("periods.json").read_text())["files"]
    assert [(e["source_name"], e["start"], e["end"]) for e in index] == [
        ("b.tif", "2016-03-01", "2016-03-01"), ("a.tif", "2016-03-02", "2016-03-02")]
    assert [o["name"] for o in rec.details["originals"]] == ["b.tif", "a.tif", "dates.csv"]


def test_a_period_mapping_overrides_file_names(tmp_path, root, write_tif):
    files = _tifs(write_tif, tmp_path / "in", ["x_20200101.tif", "y.tif"])
    rec = custom.register({files[0]: "2016-01-01", files[1]: "2016-01-02"}, name="d",
                          kind="raster", temporal="daily", root=root)
    assert rec.details["coverage_dates"] == ["2016-01-01", "2016-01-02"]


def test_raster_series_are_checked(tmp_path, root, write_tif):
    a, b = _tifs(write_tif, tmp_path / "in", ["v_2015.tif", "v_2016.tif"])
    other = write_tif(tmp_path / "in" / "v_2017.tif", np.ones((7, 6)), west=-86.5, north=40.0)
    with pytest.raises(custom.DatasetError, match="v_2017.tif is on a different grid"):
        custom.register([a, b, other], name="x", kind="raster", temporal="yearly", root=root)
    with pytest.raises(custom.DatasetError, match="band 2 does not exist"):
        custom.register([a, b], name="x", kind="raster", temporal="yearly", root=root,
                        variables=[{"name": "v", "band": 2}])
    nocrs = write_tif(tmp_path / "in" / "n_2016.tif", np.ones((6, 6)), crs=None)
    with pytest.raises(custom.DatasetError, match="n_2016.tif: the raster has no coordinate"):
        custom.register(nocrs, name="x", kind="raster", temporal="yearly", root=root)
    with pytest.raises(custom.DatasetError, match="has no date in its name"):
        custom.register([a], name="x", kind="raster", temporal="daily", root=root)
    sub = tmp_path / "in" / "sub"
    sub.mkdir()
    dup = write_tif(sub / "v_2015.tif", np.ones((6, 6)), west=-86.5, north=40.0)
    with pytest.raises(custom.DatasetError, match="two files are named v_2015.tif"):
        custom.register([a, dup], name="x", kind="raster", temporal="yearly", root=root)
    with pytest.raises(custom.DatasetError, match="a band is linked twice"):
        custom.register([a, b], name="x", kind="raster", temporal="yearly", root=root,
                        variables=[{"name": "p", "band": 1}, {"name": "q", "band": 1}])
    assert custom.list_datasets(root) == []
    assert [p.name for p in root.iterdir()] == [".registry.lock"]


def test_a_raster_copy_edited_after_registration_is_caught(tmp_path, root, write_tif):
    (tif,) = _tifs(write_tif, tmp_path / "in", ["x_2016.tif"])
    rec = custom.register(tif, name="x", kind="raster", temporal="yearly", root=root)
    d = rec.definition()
    write_tif(rec.path(rec.files[0]), np.zeros((6, 6)), west=-86.5, north=40.0)
    with pytest.raises(custom.CustomExposomeError, match="changed since"):
        custom.check_definition(d, key=rec.variable_key, deep=True)
