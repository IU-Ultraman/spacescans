"""C3 grid weights of a multi-band GeoTIFF (an upload offers "one or several bands").

exact_extract reports every band of a multi-band raster under ``band_<n>_``
prefixes; ``extract_grid_weights`` looked for an unprefixed ``cell_id`` and
failed with "No coverage column found", so every multi-band custom raster
failed its C3 step. The cells and their coverage do not depend on the band:
the weights must equal those of a one-band raster on the same grid.
"""
from __future__ import annotations

import numpy as np
import pytest

pytestmark = [pytest.mark.geo]

pytest.importorskip("exactextract")
gpd = pytest.importorskip("geopandas")


def test_a_multi_band_raster_gives_the_one_band_weights(tmp_path, write_tif):
    from spacescans.geometry.buffers import build_buffers, reproject
    from spacescans.geometry.grid_weights import extract_grid_weights

    rng = np.random.default_rng(3)
    one = write_tif(tmp_path / "one.tif", rng.random((20, 20)), west=-86.5, north=40.0)
    three = write_tif(tmp_path / "three.tif", rng.random((3, 20, 20)), west=-86.5, north=40.0)
    points = gpd.GeoDataFrame(
        {"geoid": [0, 1, 2]},
        geometry=gpd.points_from_xy([-86.45, -86.41, -86.37], [39.95, 39.91, 39.87]),
        crs="EPSG:4326")
    buffers = reproject(build_buffers(points, buffer_m=270), target_crs="EPSG:4326")

    single = extract_grid_weights(buffers, one)
    multi = extract_grid_weights(buffers, three)

    assert list(multi.columns) == ["geoid", "grid_id", "weight"]
    key = ["geoid", "grid_id"]
    a = single.sort_values(key).reset_index(drop=True)
    b = multi.sort_values(key).reset_index(drop=True)
    assert a[key].astype("int64").equals(b[key].astype("int64"))
    np.testing.assert_allclose(a["weight"].to_numpy(float), b["weight"].to_numpy(float),
                               rtol=0, atol=1e-12)
    assert np.allclose(b.groupby("geoid")["weight"].sum(), 1.0)
