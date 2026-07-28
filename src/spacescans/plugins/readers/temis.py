"""TEMIS UV HDF4 reader.

Reads daily TEMIS HDF4 files (one file per day per UV variable) and extracts
values at the grid cells referenced by the C3 weight table.  Returns a
long-format DataFrame for consumption by the ``gridded`` linkage pattern.

Expected directory structure:
    <exposure.file>/<feature>/<year>/<feature>YYYYMMDD.hdf

Supported features (UV variables):
    uvddc, uvdec, uvdvc, uvief

HDF4 scaling (matching v1 script):
    fill_value = -1000  → NaN
    if max raw value > 100: physical = raw * 0.001

Output schema (consumed by gridded_linkage with date_col="date"):
    grid_id (int64), date (str YYYY-MM-DD),
    uvddc (float64), uvdec (float64), uvdvc (float64), uvief (float64)

Note: pyhdf is required for HDF4 reading.  If not installed, the reader raises
an informative ImportError.
"""

from __future__ import annotations

from spacescans._extras import require
require("hdf4", "pyhdf")

import glob
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd

from spacescans.pipeline.registry import register_reader

# HDF4 processing constants (identical to v1)
_FILL_INT = -1000
_SCALE = 0.001
_BAND_INDEX = 0

_ALL_FEATURES = ["uvddc", "uvdec", "uvdvc", "uvief"]


# ---------------------------------------------------------------------------
# HDF4 helpers
# ---------------------------------------------------------------------------

def _parse_date(filename: str):
    """Extract a date from a filename containing YYYYMMDD."""
    m = re.search(r"(\d{8})", os.path.basename(filename))
    if m is None:
        return None
    return pd.to_datetime(m.group(1), format="%Y%m%d").date()


def _list_hdf_files(feat_dir: str, start_date, end_date) -> list[str]:
    """Recursively list .hdf files within the requested date range."""
    all_hdfs = sorted(glob.glob(os.path.join(feat_dir, "**", "*.hdf"), recursive=True))
    out = []
    for fp in all_hdfs:
        d = _parse_date(fp)
        if d is not None and start_date <= d <= end_date:
            out.append(fp)
    return out


def _read_hdf4_band0(hdf_path: str) -> np.ndarray:
    """Read the first SDS from a TEMIS HDF4 file as a flattened physical array."""
    try:
        from pyhdf.SD import SD, SDC
    except ImportError as exc:
        raise ImportError(
            "pyhdf is required to read TEMIS HDF4 files. "
            "Install it with: conda install -c conda-forge pyhdf"
        ) from exc

    hdf = SD(hdf_path, SDC.READ)
    dataset_name = list(hdf.datasets().keys())[_BAND_INDEX]
    dataset = hdf.select(dataset_name)
    data = dataset[:].astype(np.float32).ravel()
    hdf.end()

    data[data == _FILL_INT] = np.nan
    max_val = np.nanmax(data) if not np.all(np.isnan(data)) else 0.0
    if np.isfinite(max_val) and max_val > 100:
        data = data * _SCALE

    return data


def _ensure_converted(uv_root: Path, conv_dir: Path) -> None:
    """Auto-convert the HDF4 archive to parquet on first use (one-time, ~2 min
    for the full 4-feature × 7-year archive).

    Runs only when no manifest exists yet and raw feature dirs are present.
    Conversion is idempotent (existing parquet files are skipped; writes go
    through tmp+rename), so a concurrent second run at worst duplicates some
    work. Failure is non-fatal — the caller falls back to the HDF4 path.
    """
    if (conv_dir / "manifest.json").exists():
        return
    if not any((uv_root / f).is_dir() for f in _ALL_FEATURES):
        return
    try:
        from spacescans.tools.temis_convert import convert
        print("[temis] no converted archive found — one-time HDF4 → parquet "
              "conversion (subsequent runs read parquet in seconds)", flush=True)
        convert(uv_root, conv_dir)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[temis] auto-conversion failed ({exc}); "
              "falling back to HDF4 reads", flush=True)


def _load_converted_feature(
    conv_dir: Path,
    feat: str,
    keep_ids: np.ndarray,
    start_date,
    end_date,
) -> pd.DataFrame | None:
    """Fast path: read pre-converted parquet (see spacescans.tools.temis_convert)
    instead of thousands of daily HDF4 files.

    Returns None — meaning "fall back to the HDF4 path" — unless the converted
    data provably covers the request: every year in range has a parquet, and
    every keep_id lies inside the converted bounding box recorded in the
    manifest. A cohort cell outside the converted bbox must NOT silently come
    back empty.
    """
    import json

    manifest_path = conv_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
        from spacescans.tools.temis_convert import bbox_cell_ids
        covered = bbox_cell_ids(tuple(manifest["bbox"]))
    except Exception:
        return None
    if not np.isin(keep_ids, covered).all():
        return None

    years = range(start_date.year, end_date.year + 1)
    paths = [conv_dir / f"{feat}_{y}.parquet" for y in years]
    if not all(p.exists() for p in paths):
        return None

    frames = [pd.read_parquet(p) for p in paths]
    df = pd.concat(frames, ignore_index=True)
    df = df[df["grid_id"].isin(keep_ids)]
    d = pd.to_datetime(df["date"]).dt.date
    df = df[(d >= start_date) & (d <= end_date)]
    print(f"[temis/{feat}] converted fast path: {len(df):,} rows "
          f"from {len(paths)} parquet(s)", flush=True)
    return df[["grid_id", "value", "date"]].reset_index(drop=True)


def _process_one_file(hdf_path: str, keep_ids: np.ndarray) -> pd.DataFrame:
    """Read one HDF file and return selected grid cells as a DataFrame."""
    data = _read_hdf4_band0(hdf_path)
    n_cells = len(data)
    valid_ids = keep_ids[(keep_ids >= 0) & (keep_ids < n_cells)]
    if len(valid_ids) == 0:
        return pd.DataFrame(columns=["grid_id", "value", "date"])

    values = data[valid_ids]
    date_str = str(_parse_date(hdf_path))
    df = pd.DataFrame({
        "grid_id": valid_ids.astype(int),
        "value": values.astype(float),
        "date": date_str,
    })
    return df.dropna(subset=["value"])


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

@register_reader("temis")
class TemisExposureSource:
    """Read TEMIS daily HDF4 UV files and return a long-format exposure table.

    Returns a wide DataFrame with columns:
        grid_id (int64), date (str YYYY-MM-DD),
        uvddc (float64), uvdec (float64), uvdvc (float64), uvief (float64)

    Only grid_ids present in the C3 weights table are extracted.
    """

    def __init__(self, config):
        self.config = config

    def load_exposure(self, *, years: list[int] | None = None) -> pd.DataFrame:
        from spacescans.io.readers import read_table
        from spacescans.linkage.helpers import load_patients

        # Load weights to identify which grid_ids we need
        weights = read_table(self.config.source.file)
        if "value" in weights.columns and "weight" not in weights.columns:
            weights = weights.rename(columns={"value": "weight"})
        keep_ids = np.array(sorted(weights["grid_id"].dropna().astype(int).unique()), dtype=int)

        # Resolve exposure root directory
        uv_root = Path(self.config.exposure.file)
        if not uv_root.is_dir():
            uv_root = uv_root.parent

        # Determine date range from patient file (apply demo_conus etc. adapter)
        patients = load_patients(self.config)
        start_date = pd.to_datetime(patients["start"]).min().date()
        end_date = pd.to_datetime(patients["end"]).max().date()

        # Determine which features (UV vars) to process
        value_cols = list(self.config.exposure.value_cols) if self.config.exposure else _ALL_FEATURES
        # Strip trailing "_270m" suffix if present (config may use "uvddc_270m")
        features = [v.replace("_270m", "") for v in value_cols if v.replace("_270m", "") in _ALL_FEATURES]
        if not features:
            features = _ALL_FEATURES

        # Converted-parquet fast path (spacescans.tools.temis_convert output,
        # by convention a `converted/` sibling of the raw/ dir). Converted
        # automatically on first use; the manual CLI remains for
        # pre-provisioning.
        conv_dir = uv_root.parent / "converted"
        _ensure_converted(uv_root, conv_dir)

        per_feature: dict[str, pd.DataFrame] = {}
        for feat in features:
            feat_df = _load_converted_feature(
                conv_dir, feat, keep_ids, start_date, end_date
            )
            if feat_df is None:
                feat_dir = str(uv_root / feat)
                if not os.path.isdir(feat_dir):
                    continue

                files = _list_hdf_files(feat_dir, start_date, end_date)
                if not files:
                    continue

                frames = []
                total = len(files)
                for idx, fp in enumerate(files, start=1):
                    if idx % 100 == 0 or idx == total:
                        print(f"[temis/{feat}] {idx}/{total} {os.path.basename(fp)}")
                    frames.append(_process_one_file(fp, keep_ids))

                if not frames:
                    continue
                feat_df = pd.concat(frames, ignore_index=True)

            if feat_df.empty:
                continue
            feat_df = feat_df.rename(columns={"value": feat})
            per_feature[feat] = feat_df[["grid_id", "date", feat]]

        if not per_feature:
            # Return empty DataFrame with expected schema
            cols = ["grid_id", "date"] + features
            return pd.DataFrame(columns=cols)

        # Merge all features on (grid_id, date)
        result = None
        for feat, df in per_feature.items():
            if result is None:
                result = df
            else:
                result = result.merge(df, on=["grid_id", "date"], how="outer")

        result["grid_id"] = result["grid_id"].astype("int64")
        return result.reset_index(drop=True)
