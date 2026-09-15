"""ACAG biweekly reader — reads NetCDF rasters for all pollutants, extracts grid cell values.

Matches v1 C4_Linkage_ACAG.py: uses 1-based grid_id indexing, supports xarray for NC files,
processes all pollutant directories, and derives _nbm (non-biomass) columns.
"""

from __future__ import annotations

from spacescans._extras import require
require("nc", "xarray", "netCDF4")

import os
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from spacescans.pipeline.registry import register_reader
from spacescans.transforms.date_parse import parse_date_range_from_filename

# Pollutant key → subfolder name mapping
_BASE_MAP = {"pm25": "PM25", "bc": "BC", "dust": "DUST", "nh4": "NH4",
             "no3": "NO3", "om": "OM", "so4": "SO4", "ss": "SS"}
_BM_MAP = {k + "_bm": v + "_bm" for k, v in _BASE_MAP.items()}

DATE_PATTERN = r"\.(?P<start>\d{7})-(?P<end>\d{7})\."


def _doy_to_date(yyyydoy: str) -> str:
    from datetime import datetime, timedelta
    y, doy = int(yyyydoy[:4]), int(yyyydoy[4:])
    return (datetime(y, 1, 1) + timedelta(days=doy - 1)).strftime("%Y-%m-%d")


def _parse_biweek(filename: str) -> tuple[str, str]:
    import re
    m = re.search(r"(\d{7})-(\d{7})\.nc$", filename)
    if not m:
        raise ValueError(f"Cannot parse dates from: {filename}")
    return _doy_to_date(m.group(1)), _doy_to_date(m.group(2))


def _process_nc_file(filepath: str, keep_ids: list[int]) -> pd.DataFrame:
    """Read one NC file and extract values at 1-based grid_id positions (matching v1)."""
    with xr.open_dataset(filepath, mask_and_scale=False) as ds:
        coord_names = set(ds.coords)
        data_vars = [v for v in ds.data_vars if v not in coord_names]
        if not data_vars:
            return pd.DataFrame(columns=["grid_id", "value", "start_date", "end_date"])
        da = ds[data_vars[0]]
        # The C3 weights come from exactextract over a north-up template
        # (row 0 = north), numbered row-major from the top-left cell. ACAG
        # ships (lat, lon) with lat ascending — row 0 is the *southern* edge —
        # so without this flip every value lands mirrored north↔south, with
        # no error to show for it. Transpose first if a file ever comes
        # (lon, lat), then flip when the latitude axis ascends.
        if tuple(da.dims[-2:]) == ("lon", "lat"):
            da = da.transpose(*da.dims[:-2], "lat", "lon")
        arr = da.values
        while arr.ndim > 2:
            arr = arr[0]
        lat_name = next((c for c in ("lat", "latitude") if c in ds.variables), None)
        if lat_name is not None:
            lat = ds[lat_name].values
            if len(lat) > 1 and lat[0] < lat[-1]:
                arr = arr[::-1, :]

    n_rows, n_cols = arr.shape
    n_cells = n_rows * n_cols

    # v1 uses 1-based indexing: valid if 1 <= grid_id <= n_cells
    valid_ids = [i for i in keep_ids if 1 <= i <= n_cells]
    if not valid_ids:
        return pd.DataFrame(columns=["grid_id", "value", "start_date", "end_date"])

    rows = [(i - 1) // n_cols for i in valid_ids]
    cols = [(i - 1) % n_cols for i in valid_ids]
    vals = [float(arr[r, c]) for r, c in zip(rows, cols)]

    start_date, end_date = _parse_biweek(os.path.basename(filepath))
    df = pd.DataFrame({
        "grid_id": valid_ids,
        "value": vals,
        "start_date": start_date,
        "end_date": end_date,
    })
    df = df.dropna(subset=["value"])
    return df.reset_index(drop=True)


@register_reader("acag")
class ACAGExposureSource:
    """Read ACAG biweekly NetCDF files for one pollutant directory."""

    def __init__(self, config):
        self.config = config

    def load_exposure(self, *, years: list[int] | None = None) -> pd.DataFrame:
        """Load biweekly grid exposure as [grid_id, value, start_date, end_date]."""
        from spacescans.io.readers import read_table

        weights = read_table(self.config.source.file)
        if "value" in weights.columns:
            weights = weights.rename(columns={"value": "weight"})
        keep_ids = sorted(weights["grid_id"].dropna().astype(int).unique())

        exposure_path = Path(self.config.exposure.file)
        if exposure_path.is_dir():
            nc_files = sorted(exposure_path.glob("*.nc"))
        else:
            nc_files = sorted(exposure_path.parent.glob(exposure_path.name))

        if not nc_files:
            raise FileNotFoundError(f"No NC files found at {exposure_path}")

        start_year = min(years) if years else 2013
        end_year = max(years) if years else 2019

        all_dfs = []
        for nc_path in nc_files:
            try:
                sd, ed = _parse_biweek(nc_path.name)
            except ValueError:
                continue
            sd_year = int(sd[:4])
            ed_year = int(ed[:4])
            if not (start_year <= sd_year <= end_year or start_year <= ed_year <= end_year):
                continue
            df = _process_nc_file(str(nc_path), keep_ids)
            if not df.empty:
                all_dfs.append(df)

        if not all_dfs:
            return pd.DataFrame(columns=["grid_id", "value", "start_date", "end_date"])
        return pd.concat(all_dfs, ignore_index=True)
