"""One-time TEMIS HDF4 → parquet pre-conversion.

The temis reader's cost is dominated by opening thousands of daily HDF4
files (4 UV vars × 365 days × N years) on every C4 run. This tool reads the
archive once and writes one compact parquet per (feature, year) holding only
the grid cells inside a bounding box (default CONUS) — after which the
reader's converted-data fast path skips HDF4 entirely.

Usage (paths relative to --data-dir semantics don't apply here; pass real paths):

    python -m spacescans.tools.temis_convert \
        --raw /project/TEMIS/C4/raw --out /project/TEMIS/C4/converted

Grid: TEMIS products share a fixed global 0.25° grid, 1440×720, row 0 at
90N / col 0 at 180W, C-order flat indexing — identical to the C3 template
raster, so `grid_id` here means exactly what it means in the C3 weights.
Scaling/fill semantics are inherited by calling the reader's own
_read_hdf4_band0 on the full global array before subsetting (the >100-max
scale heuristic must see the global max, not the subset's).
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

GRID_W, GRID_H = 1440, 720
RES = 0.25
LON0, LAT0 = -180.0, 90.0
CONUS_BBOX = (-125.0, 24.0, -66.0, 50.0)  # lon_min, lat_min, lon_max, lat_max
_ALL_FEATURES = ("uvddc", "uvdec", "uvdvc", "uvief")
MANIFEST = "manifest.json"


def bbox_cell_ids(bbox: tuple[float, float, float, float]) -> np.ndarray:
    """0-based C-order flat indices of all grid cells intersecting bbox."""
    lon_min, lat_min, lon_max, lat_max = bbox
    c0 = int(np.floor((lon_min - LON0) / RES))
    c1 = int(np.ceil((lon_max - LON0) / RES)) - 1
    r0 = int(np.floor((LAT0 - lat_max) / RES))
    r1 = int(np.ceil((LAT0 - lat_min) / RES)) - 1
    c0, c1 = max(c0, 0), min(c1, GRID_W - 1)
    r0, r1 = max(r0, 0), min(r1, GRID_H - 1)
    rows = np.arange(r0, r1 + 1)
    cols = np.arange(c0, c1 + 1)
    return (rows[:, None] * GRID_W + cols[None, :]).ravel()


def convert(raw_dir: Path, out_dir: Path,
            bbox: tuple[float, float, float, float] = CONUS_BBOX,
            features: tuple[str, ...] = _ALL_FEATURES) -> None:
    from spacescans.plugins.readers.temis import (
        _list_hdf_files,
        _parse_date,
        _read_hdf4_band0,
    )

    idx = bbox_cell_ids(bbox)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    written: dict[str, int] = {}

    for feat in features:
        feat_dir = raw_dir / feat
        if not feat_dir.is_dir():
            continue
        files = _list_hdf_files(str(feat_dir), pd.Timestamp.min.date(),
                                pd.Timestamp.max.date())
        by_year: dict[int, list[str]] = {}
        for fp in files:
            d = _parse_date(fp)
            by_year.setdefault(d.year, []).append(fp)

        for year, fps in sorted(by_year.items()):
            out_path = out_dir / f"{feat}_{year}.parquet"
            if out_path.exists():
                print(f"[convert] skip existing {out_path.name}", flush=True)
                continue
            frames = []
            for fp in fps:
                data = _read_hdf4_band0(fp)  # full global read + fill/scale
                vals = data[idx]
                keep = ~np.isnan(vals)
                if not keep.any():
                    continue
                frames.append(pd.DataFrame({
                    "grid_id": idx[keep].astype(np.int32),
                    "date": str(_parse_date(fp)),
                    "value": vals[keep].astype(np.float32),
                }))
            df = (pd.concat(frames, ignore_index=True) if frames
                  else pd.DataFrame(columns=["grid_id", "date", "value"]))
            tmp = out_path.with_suffix(".parquet.tmp")
            df.to_parquet(tmp, index=False)
            tmp.rename(out_path)
            written[out_path.name] = len(df)
            print(f"[convert] {out_path.name}: {len(fps)} days -> "
                  f"{len(df):,} rows ({(time.time()-t0)/60:.1f}m elapsed)",
                  flush=True)

    manifest = {
        "grid": {"width": GRID_W, "height": GRID_H, "res_deg": RES,
                 "origin": [LON0, LAT0], "indexing": "0-based C-order"},
        "bbox": list(bbox),
        "cell_count": int(idx.size),
        "files": written,
        "created_at": pd.Timestamp.now("UTC").isoformat(),
        # TEMIS terms: use requires credits and informing the team; there is
        # no explicit redistribution grant — regenerate locally, don't ship.
        "credit": "Data © KNMI/ESA — TEMIS UV (https://www.temis.nl/); "
                  "doi.org/10.21944/temis-uv-oper-v2",
        "redistribution": "not granted by TEMIS terms; generate per deployment",
    }
    with open(out_dir / MANIFEST, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"[convert] DONE in {(time.time()-t0)/60:.1f}m -> {out_dir}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--raw", required=True, type=Path,
                   help="TEMIS raw dir holding <feature>/<year>/*.hdf")
    p.add_argument("--out", required=True, type=Path,
                   help="output dir for <feature>_<year>.parquet + manifest")
    p.add_argument("--bbox", nargs=4, type=float, default=list(CONUS_BBOX),
                   metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"))
    a = p.parse_args()
    convert(a.raw, a.out, tuple(a.bbox))


if __name__ == "__main__":
    main()
