"""Uploaded rasters and vectors must not be read as GDAL/OGR VRTs (review finding R2-1).

A file is validated by extension and then opened by content with every GDAL/OGR driver, so a
.tif/.gpkg/.geojson/.zip whose bytes are a VRT would be followed to an arbitrary local path or a
/vsicurl URL (local file read / SSRF). These tests only assert that such a file is REFUSED and
that genuine data still opens; they never reference a real target or the network.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytestmark = [pytest.mark.geo]
rasterio = pytest.importorskip("rasterio")
gpd = pytest.importorskip("geopandas")
shapely = pytest.importorskip("shapely")

from spacescans.custom import raster as _raster  # noqa: E402
from spacescans.custom.errors import RasterError  # noqa: E402
from spacescans.custom.features import _read as _features_read  # noqa: E402
from spacescans.custom.registry import DatasetError  # noqa: E402

_RASTER_VRT = ('<VRTDataset rasterXSize="4" rasterYSize="4">'
               '<VRTRasterBand dataType="Float32" band="1"><SimpleSource>'
               '<SourceFilename relativeToVRT="0">neighbour.tif</SourceFilename>'
               '</SimpleSource></VRTRasterBand></VRTDataset>')
_OGR_VRT = ('<OGRVRTDataSource><OGRVRTLayer name="x">'
            '<SrcDataSource>neighbour.csv</SrcDataSource></OGRVRTLayer></OGRVRTDataSource>')


def _real_tif(path: Path) -> Path:
    from rasterio.transform import from_origin
    with rasterio.open(path, "w", driver="GTiff", height=4, width=4, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_origin(-86.5, 40.0, 0.01, 0.01)) as dst:
        dst.write(np.arange(16, dtype="float32").reshape(4, 4), 1)
    return path


def test_real_geotiff_still_opens(tmp_path):
    p = _real_tif(tmp_path / "real.tif")
    meta = _raster.inspect_raster(p)
    assert meta["width"] == 4 and meta["band_count"] == 1
    assert _raster.extract_values(p, np.array([0, 5, 15])).tolist() == [0.0, 5.0, 15.0]
    # bytes path too (the web preview uploads bytes)
    assert _raster.inspect_raster(p.read_bytes())["width"] == 4


def test_raster_vrt_is_refused_by_path_and_bytes(tmp_path):
    (tmp_path / "evil.tif").write_text(_RASTER_VRT)
    with pytest.raises(RasterError, match="VRT"):
        _raster.inspect_raster(tmp_path / "evil.tif")
    with pytest.raises(RasterError, match="VRT"):
        _raster.inspect_raster((tmp_path / "evil.tif").read_bytes())
    with pytest.raises(RasterError, match="VRT"):
        _raster.extract_values(tmp_path / "evil.tif", np.array([0]))


def test_xml_declaration_before_the_vrt_is_still_refused(tmp_path):
    (tmp_path / "evil.tif").write_text('<?xml version="1.0"?>\n' + _RASTER_VRT)
    with pytest.raises(RasterError, match="VRT"):
        _raster.inspect_raster(tmp_path / "evil.tif")


def test_real_geojson_still_reads(tmp_path):
    p = tmp_path / "real.geojson"
    gpd.GeoDataFrame({"a": [1]}, geometry=[shapely.geometry.Point(-86.4, 39.9)],
                     crs="EPSG:4326").to_file(p, driver="GeoJSON")
    assert len(_features_read(p, None)) == 1


@pytest.mark.parametrize("name", ["evil.geojson", "evil.gpkg", "evil.json"])
def test_ogr_vrt_vector_is_refused(tmp_path, name):
    (tmp_path / name).write_text(_OGR_VRT)
    with pytest.raises(DatasetError, match="VRT"):
        _features_read(tmp_path / name, None)


def test_zip_hiding_a_vrt_member_is_refused(tmp_path):
    import zipfile
    p = tmp_path / "shape.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("layer.vrt", _OGR_VRT)
    with pytest.raises(DatasetError, match="VRT"):
        _features_read(p, None)
