"""Fixtures for the spacescans.custom tests.

``templates/`` holds byte-identical copies of the six web templates the custom
path renders (spacescans-web ``configs/`` at 3991c3b). Once the catalog ships
its own copies, point ``load_template`` at ``spacescans.catalog.template``.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import yaml

TEMPLATES = Path(__file__).parent / "templates"


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
    def _load(relpath: str) -> dict:
        return yaml.safe_load((TEMPLATES / relpath).read_text())
    return _load
