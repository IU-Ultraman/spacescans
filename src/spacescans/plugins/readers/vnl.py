"""VNL (VIIRS Nighttime Lights) reader.

Reads annual or multi-month VNL GeoTIFF files from a directory and extracts
radiance values at the grid_ids referenced by the C3 weight table. Returns a
long-format DataFrame for consumption by the ``gridded`` linkage pattern.

Filename patterns recognised (mirrors v1 R C4_Linkage_VNL_270m.R):
    VNL_v21_npp_2013_global_*.tif          → (2013-01-01, 2013-12-31)
    VNL_v22_npp-j01_2022_global_*.tif      → (2022-01-01, 2022-12-31)
    VNL_v21_npp_201204-201212_global_*.tif → (2012-04-01, 2012-12-31)

Output schema (consumed by gridded_linkage with start_col/end_col):
    grid_id (int64), start_date (str YYYY-MM-DD), end_date (str YYYY-MM-DD),
    value (float64)

Cell indexing: R's terra uses 1-based cell indexing (cell 1 = top-left).
Python rasterio yields a 2D array; we flatten in row-major (C-order) and
treat cell_id-1 as the array index. This matches R behaviour exactly.
"""

from __future__ import annotations

from spacescans._extras import require
require("geo", "geopandas", "rasterio", "shapely", "exactextract")

import glob
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd

from spacescans.pipeline.registry import register_reader


_RNG_RE = re.compile(r"_(\d{6})-(\d{6})_global_")
_YR_RE = re.compile(r"npp.*?_(\d{4})_global_")


def _parse_window_from_name(path: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return (start_date, end_date) parsed from VNL filename. Matches v1 R."""
    name = os.path.basename(path)
    m_rng = _RNG_RE.search(name)
    if m_rng:
        start_ym, end_ym = m_rng.group(1), m_rng.group(2)
        start = pd.Timestamp(f"{start_ym[:4]}-{start_ym[4:]}-01")
        end_month_start = pd.Timestamp(f"{end_ym[:4]}-{end_ym[4:]}-01")
        end = (end_month_start + pd.DateOffset(months=1)) - pd.Timedelta(days=1)
        return start, end
    m_yr = _YR_RE.search(name)
    if m_yr:
        y = int(m_yr.group(1))
        return pd.Timestamp(f"{y}-01-01"), pd.Timestamp(f"{y}-12-31")
    raise ValueError(f"Cannot parse dates from filename: {name}")


def _read_cells_at(raster_path: str, cell_ids_1based: np.ndarray) -> np.ndarray:
    """Read raster values at given 1-based cell IDs (matches R's terra indexing).

    R: r[ids] uses 1-based row-major (C-order), so cell (id - 1) sits at
    row = (id-1) // width, col = (id-1) % width.

    Reads only the bounding window of the requested cells instead of the
    whole raster: a full annual VNL global GeoTIFF is ~11.6 GB decompressed
    (86401 x 33601 float32), which OOM-kills small containers, while even a
    CONUS-wide cohort's cell window is a few hundred MB.
    """
    import rasterio
    from rasterio.windows import Window

    out = np.full(cell_ids_1based.size, np.nan, dtype=np.float64)
    with rasterio.open(raster_path) as src:
        w, h = src.width, src.height
        n = w * h
        valid = (cell_ids_1based >= 1) & (cell_ids_1based <= n)
        if not valid.any():
            return out
        flat0 = cell_ids_1based[valid] - 1  # 0-based flat indices
        rows = flat0 // w
        cols = flat0 % w
        r0, r1 = int(rows.min()), int(rows.max())
        c0, c1 = int(cols.min()), int(cols.max())
        window = Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1)
        arr = src.read(1, window=window)
        out[valid] = arr[rows - r0, cols - c0].astype(np.float64)
    return out


@register_reader("vnl")
class VNLExposureSource:
    """Load VNL annual GeoTIFFs as long-format DataFrame for gridded pattern."""

    def __init__(self, config):
        self.config = config

    def load_exposure(self, *, years: list[int] | None = None) -> pd.DataFrame:
        from spacescans.io.readers import read_table

        # Load C3 weights to get keep_ids (grid_ids we actually need)
        weights = read_table(self.config.source.file, key=self.config.source.key)
        keep_ids = np.array(
            sorted(weights["grid_id"].dropna().astype(int).unique()),
            dtype=np.int64,
        )

        # Resolve exposure directory
        exp_root = Path(self.config.exposure.file)
        if not exp_root.is_dir():
            exp_root = exp_root.parent

        # Find all .tif files
        tifs = sorted(glob.glob(str(exp_root / "*.tif")))
        if not tifs:
            raise FileNotFoundError(f"No .tif files in {exp_root}")

        # Filter by year range if given
        if years:
            year_set = set(years)
            kept = []
            for t in tifs:
                start, end = _parse_window_from_name(t)
                if start.year in year_set or end.year in year_set:
                    kept.append((start, end, t))
            kept.sort(key=lambda x: x[0])
        else:
            kept = []
            for t in tifs:
                start, end = _parse_window_from_name(t)
                kept.append((start, end, t))
            kept.sort(key=lambda x: x[0])

        print(f"[vnl] processing {len(kept)} files", flush=True)

        frames = []
        for i, (start, end, t) in enumerate(kept, start=1):
            print(f"[vnl]   {i}/{len(kept)}: {os.path.basename(t)}", flush=True)
            vals = _read_cells_at(t, keep_ids)
            df = pd.DataFrame({
                "grid_id": keep_ids,
                "value": vals,
                "start_date": start.strftime("%Y-%m-%d"),
                "end_date": end.strftime("%Y-%m-%d"),
            })
            df = df.dropna(subset=["value"])
            frames.append(df)

        if not frames:
            return pd.DataFrame(columns=["grid_id", "value", "start_date", "end_date"])
        result = pd.concat(frames, ignore_index=True)
        result["grid_id"] = result["grid_id"].astype("int64")
        return result[["grid_id", "value", "start_date", "end_date"]]
