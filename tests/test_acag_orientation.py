"""ACAG reader: template cells are numbered from the north, the NetCDF is
stored from the south — the reader must reconcile the two.

exactextract numbers a north-up raster's cells row-major from the top-left,
and that (plus grid_id_offset=1) is what the C3 weight table carries. ACAG's
NetCDF arrays are (lat, lon) with latitude ascending, i.e. row 0 is the
southern edge. Indexing that array directly by the template's grid_id mirrors
every value across the equator of the grid — measured on the real product:
Tallahassee's cell came back with a value from 51.6°N. These tests pin the
flip with a 3×4 synthetic file where every cell is distinguishable.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("xarray")
pytest.importorskip("netCDF4")
import xarray as xr  # noqa: E402

from spacescans.plugins.readers.acag import _process_nc_file  # noqa: E402

N_LAT, N_LON = 3, 4
NAME = "V5NA05.HybridPM25.xNorthAmerica.2017001-2017014.nc"


def _write(path: Path, *, lat_ascending: bool, dims=("lat", "lon")) -> np.ndarray:
    """Cell value = north-up row-major index, so a correct read returns the
    grid_id - 1 it was asked for. Returned array is always north-up."""
    north_up = np.arange(N_LAT * N_LON, dtype="float32").reshape(N_LAT, N_LON)
    lat_nu = np.array([12.0, 11.0, 10.0])          # row 0 = north
    lon = np.array([0.0, 1.0, 2.0, 3.0])
    if lat_ascending:
        data, lat = north_up[::-1, :], lat_nu[::-1]  # file stores south first
    else:
        data, lat = north_up, lat_nu
    if dims == ("lon", "lat"):
        data = data.T
    ds = xr.Dataset({"GWRPM25": (dims, data)}, coords={"lat": lat, "lon": lon})
    ds.to_netcdf(path)
    return north_up


@pytest.mark.parametrize("lat_ascending", [True, False], ids=["lat-ascending", "lat-descending"])
def test_grid_id_maps_to_north_up_cell(tmp_path: Path, lat_ascending: bool) -> None:
    north_up = _write(tmp_path / NAME, lat_ascending=lat_ascending)
    keep = [1, N_LON, N_LAT * N_LON]                 # NW corner, NE corner, SE corner
    out = _process_nc_file(str(tmp_path / NAME), keep).set_index("grid_id")["value"]
    for gid in keep:
        r, c = divmod(gid - 1, N_LON)
        assert out[gid] == north_up[r, c] == gid - 1, (
            f"grid_id {gid} should read the north-up cell ({r},{c}); "
            f"got {out[gid]} — latitude orientation not reconciled"
        )


def test_lon_lat_dim_order_is_transposed(tmp_path: Path) -> None:
    north_up = _write(tmp_path / NAME, lat_ascending=True, dims=("lon", "lat"))
    out = _process_nc_file(str(tmp_path / NAME), [1, N_LAT * N_LON]).set_index("grid_id")["value"]
    assert out[1] == north_up[0, 0]
    assert out[N_LAT * N_LON] == north_up[-1, -1]


def test_biweek_dates_parsed_from_filename(tmp_path: Path) -> None:
    _write(tmp_path / NAME, lat_ascending=True)
    out = _process_nc_file(str(tmp_path / NAME), [1])
    assert out.loc[0, "start_date"] == "2017-01-01"
    assert out.loc[0, "end_date"] == "2017-01-14"
