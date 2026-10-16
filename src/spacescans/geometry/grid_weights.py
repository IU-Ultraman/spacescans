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


def extract_grid_weights(
    buffers: gpd.GeoDataFrame,
    raster_path: str | Path,
    *,
    chunk_size: int = 5000,
    id_col: str = "geoid",
    coverage_stat: str = "coverage",
    grid_id_offset: int = 0,
) -> pd.DataFrame:
    import rasterio
    from exactextract import exact_extract

    n_buffers = len(buffers)
    n_chunks = max(1, (n_buffers + chunk_size - 1) // chunk_size)
    log_every = max(1, n_chunks // 20)
    print(
        f"[grid_weights] processing {n_buffers} buffers × raster={Path(raster_path).name} "
        f"in {n_chunks} chunks of {chunk_size}",
        flush=True,
    )

    timings = {"raster_open": 0.0, "exact_extract": 0.0, "postprocess": 0.0}
    t_loop = time.perf_counter()

    all_chunks = []
    for chunk_idx, start in enumerate(range(0, n_buffers, chunk_size)):
        chunk = buffers.iloc[start : start + chunk_size]

        t = time.perf_counter()
        src_ctx = rasterio.open(str(raster_path))
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
