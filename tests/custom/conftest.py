"""Fixtures for the spacescans.custom tests.

``load_template`` reads the catalog's packaged templates (byte copies of
spacescans-web ``configs/`` at 3991c3b, checked by tests/catalog), the ones the
custom path renders in production.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from spacescans import catalog


def _write_tif(path, array, *, west=-85.0, north=31.0, res=0.01, crs="EPSG:4326",
               nodata=None, bottom_up=False, **profile):
    """A small GeoTIFF. ``array`` is (rows, cols) for one band, or (bands, rows, cols)."""
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    arr = np.asarray(array, dtype="float32")
    if arr.ndim == 2:
        arr = arr[None, ...]
    bands, rows, cols = arr.shape
    if bottom_up:
        transform = rasterio.Affine(res, 0, west, 0, res, north - rows * res)
    else:
        transform = from_origin(west, north, res, res)
    with rasterio.open(
        path, "w", driver="GTiff", width=cols, height=rows, count=bands,
        dtype="float32", crs=crs, transform=transform, nodata=nodata, **profile,
    ) as dst:
        dst.write(arr)
    return Path(path)


@pytest.fixture
def write_tif():
    return _write_tif


@pytest.fixture
def load_template():
    return catalog.template
