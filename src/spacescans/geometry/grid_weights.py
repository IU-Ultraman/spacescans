"""Raster-buffer grid weight extraction using exact_extract."""
from __future__ import annotations

from spacescans._extras import require
require("geo", "geopandas", "rasterio", "shapely", "exactextract")

import sys
import time
from pathlib import Path
import geopandas as gpd
import numpy as np
import pandas as pd
from spacescans.models.raster_meta import RasterMeta


def _grid_surrogate(raster_path: str | Path):
    """An in-memory, single-band, nodata-free twin of ``raster_path``'s GRID.

    The buffer→cell coverage is a geometric fact of where the cells are, not of
    their values: it must not change with the template file's own nodata. So the
    weights are measured against a surrogate on the same CRS, transform and
    shape, one band of ones and no nodata — then *every* cell a buffer touches
    gets a weight, and C4's per-period ``MissingPolicy.SKIP`` drops the cells
    that are nodata in a given period or band and renormalises there (the
    docs' "left out of both the numerator and the denominator within each
    period"). Without this, exact_extract omits the template file's nodata
    cells and a location over them is never linked, in any period.
    """
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile

    with rasterio.open(str(raster_path)) as src:
        profile = {
            "driver": "GTiff", "width": src.width, "height": src.height,
            "count": 1, "dtype": "uint8", "crs": src.crs, "transform": src.transform,
            "nodata": None,
        }
    memfile = MemoryFile()
    with memfile.open(**profile) as dst:
        dst.write(np.ones((profile["height"], profile["width"]), dtype="uint8"), 1)
    return memfile


def extract_grid_weights(
    buffers: gpd.GeoDataFrame,
    raster_path: str | Path,
    *,
    chunk_size: int = 5000,
    id_col: str = "geoid",
    coverage_stat: str = "coverage",
    grid_id_offset: int = 0,
    nodata_free: bool = False,
) -> pd.DataFrame:
    import rasterio
    from exactextract import exact_extract

    n_buffers = len(buffers)
    n_chunks = max(1, (n_buffers + chunk_size - 1) // chunk_size)
    log_every = max(1, n_chunks // 20)
    print(
        f"[grid_weights] processing {n_buffers} buffers × raster={Path(raster_path).name} "
        f"in {n_chunks} chunks of {chunk_size}"
        f"{' (nodata-free grid)' if nodata_free else ''}",
        flush=True,
    )

    timings = {"raster_open": 0.0, "exact_extract": 0.0, "postprocess": 0.0}
    t_loop = time.perf_counter()

    # When nodata_free, weight against a ones surrogate of the grid so a cell
    # that is nodata in the template file still gets a weight (see _grid_surrogate).
    surrogate = _grid_surrogate(raster_path) if nodata_free else None

    all_chunks = []
    for chunk_idx, start in enumerate(range(0, n_buffers, chunk_size)):
        chunk = buffers.iloc[start : start + chunk_size]

        t = time.perf_counter()
        src_ctx = surrogate.open() if surrogate is not None else rasterio.open(str(raster_path))
        timings["raster_open"] += time.perf_counter() - t

        with src_ctx as src:
            t = time.perf_counter()
            stats = exact_extract(
                src, chunk, ["cell_id", coverage_stat],
                include_cols=[id_col], output="pandas",
            )
            timings["exact_extract"] += time.perf_counter() - t

        t = time.perf_counter()
        df = pd.DataFrame(stats)
        if df.empty:
            timings["postprocess"] += time.perf_counter() - t
            continue
        if "cell_id" not in df.columns and "band_1_cell_id" in df.columns:
            # A multi-band raster: exact_extract reports each band, prefixed. The cells and
            # their coverage (so the weights) are the same in every band: use band 1's.
            df = df.rename(columns={"band_1_cell_id": "cell_id",
                                    f"band_1_{coverage_stat}": coverage_stat})
        cov_col = None
        for c in df.columns:
            if c.startswith("coverage") and c != "cell_id":
                cov_col = c
                break
        if cov_col is None:
            raise RuntimeError(f"No coverage column found. Columns: {list(df.columns)}")
        df = df.explode(["cell_id", cov_col])
        df = df.dropna(subset=["cell_id", cov_col])
        df["cell_id"] = df["cell_id"].astype(int) + grid_id_offset
        df[cov_col] = df[cov_col].astype(float)
        df = df[df[cov_col] > 0]
        totals = df.groupby(id_col)[cov_col].transform("sum")
        df["weight"] = df[cov_col] / totals
        all_chunks.append(df[[id_col, "cell_id", "weight"]].rename(columns={"cell_id": "grid_id"}))
        timings["postprocess"] += time.perf_counter() - t

        if (chunk_idx + 1) % log_every == 0 or (chunk_idx + 1) == n_chunks:
            elapsed = time.perf_counter() - t_loop
            done = min((chunk_idx + 1) * chunk_size, n_buffers)
            rate = done / elapsed if elapsed > 0 else 0
            eta = (n_buffers - done) / rate / 60 if rate > 0 else 0
            print(
                f"[grid_weights] {done:>6}/{n_buffers} ({100*done/n_buffers:5.1f}%)  "
                f"elapsed={elapsed/60:5.2f}m  rate={rate:.1f}/s  ETA={eta:5.2f}m",
                flush=True,
            )

    total_loop = time.perf_counter() - t_loop

    if surrogate is not None:
        surrogate.close()

    if not all_chunks:
        return pd.DataFrame(columns=[id_col, "grid_id", "weight"])

    t = time.perf_counter()
    out = pd.concat(all_chunks, ignore_index=True)
    concat_s = time.perf_counter() - t
    grand_total = total_loop + concat_s

    measured = sum(timings.values())
    other_s = max(0.0, total_loop - measured)

    print("[grid_weights] === SUMMARY (main loop + concat) ===", flush=True)
    for k in sorted(timings.keys(), key=lambda x: -timings[x]):
        pct = 100 * timings[k] / grand_total if grand_total > 0 else 0
        print(f"[grid_weights]   {k:14s} {timings[k]/60:6.2f}m ({pct:5.1f}%)", flush=True)
    other_pct = 100 * other_s / grand_total if grand_total > 0 else 0
    concat_pct = 100 * concat_s / grand_total if grand_total > 0 else 0
    print(f"[grid_weights]   {'loop_other':14s} {other_s/60:6.2f}m ({other_pct:5.1f}%)", flush=True)
    print(f"[grid_weights]   {'pd.concat':14s} {concat_s/60:6.2f}m ({concat_pct:5.1f}%)", flush=True)
    print(f"[grid_weights]   {'total':14s} {grand_total/60:6.2f}m", flush=True)

    return out


def validate_weight_sums(
    weights: pd.DataFrame,
    *,
    id_col: str = "geoid",
    tol: float = 1e-6,
) -> float:
    sums = weights.groupby(id_col)["weight"].sum()
    devs = (sums - 1.0).abs()
    max_dev = devs.max()
    if pd.isna(max_dev):
        max_dev = 0.0
    assert max_dev < tol, f"Weight sum deviation: {max_dev:.8f}"
    return float(max_dev)


def assert_same_grid(*metas: RasterMeta) -> RasterMeta:
    if not metas:
        raise ValueError("No raster metas provided")
    ref = metas[0]
    for i, m in enumerate(metas[1:], 1):
        if (m.crs, m.height, m.width, m.transform, m.bounds) != (
            ref.crs,
            ref.height,
            ref.width,
            ref.transform,
            ref.bounds,
        ):
            raise RuntimeError(f"Raster grid mismatch at index {i}: {m} vs {ref}")
    return ref
