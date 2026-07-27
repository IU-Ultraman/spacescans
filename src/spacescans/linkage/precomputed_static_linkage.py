# common_v2/linkage/precomputed_static_linkage.py
"""Precomputed static linkage pattern.

Used when the exposure table is already at geoid resolution (no temporal
variation, no spatial aggregation needed).  The reader plugin loads the
precomputed pkl and returns a DataFrame with [geoid, <value_cols>].

Aggregation logic (mirrors v1 NHD script, lines 316-346):
  1. Load patients + precomputed geoid-level exposure.
  2. For each patient, compute episode duration (days).
  3. Duration-weighted average across all episodes per patient.
     (Since data is static per geoid, this simplifies to a day-weighted
      average of geoid values across address periods.)

This pattern replaces the ``proximity`` pattern for NHD blue space, where the
intermediate ``proximity_blue_by_category_m.pkl`` already contains
geoid-level distances.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from spacescans.io.writers import write_table
from spacescans.linkage.helpers import load_patients, resolve_output_grouping
from spacescans.pipeline.registry import get_reader, register_pattern


def _wtd_mean(vals, wts):
    """Duration-weighted mean with NaN handling (matches v1 wtd_mean)."""
    import numpy as np
    values = np.asarray(vals, dtype=float)
    weights = np.asarray(wts, dtype=float)
    ok = ~pd.isna(values)
    if not ok.any():
        return float("nan")
    return float(values[ok] @ weights[ok]) / float(weights[ok].sum())


@register_pattern("precomputed_static")
def run_precomputed_static(config, engine) -> Path:
    """Aggregate geoid-level static exposure to patient level.

    The plugin (``config.plugin``) must implement ``load_exposure()``
    returning a DataFrame with at least [geoid, <value_cols>].
    """
    patients = load_patients(config)

    # Load exposure via plugin
    reader_cls = get_reader(config.plugin)
    reader = reader_cls(config)
    exposure = reader.load_exposure()

    # Resolve value columns
    value_cols = list(config.exposure.value_cols) if config.exposure else [
        c for c in exposure.columns if c != "geoid"
    ]

    # Prepare episode duration
    vsehr = patients[["PATID", "geoid", "start", "end"]].copy()
    vsehr["PATID"] = vsehr["PATID"].astype(str)
    vsehr["geoid"] = pd.to_numeric(vsehr["geoid"], errors="coerce").astype("int64")
    vsehr["start"] = pd.to_datetime(vsehr["start"])
    vsehr["end"] = pd.to_datetime(vsehr["end"])
    vsehr["overlap_days"] = (vsehr["end"] - vsehr["start"]).dt.days + 1
    vsehr = vsehr[vsehr["geoid"].notna() & (vsehr["overlap_days"] > 0)]

    # Join exposure to patient episodes on geoid
    exposure = exposure.copy()
    exposure["geoid"] = exposure["geoid"].astype("int64")
    joined = vsehr.merge(exposure, on="geoid", how="left")

    # Duration-weighted average per patient (or per patient-episode).
    # Dispatch on TimeConfig.output_grouping — mirrors the SQL-clause edit in
    # precomputed_areal_linkage.py but expressed as a pandas groupby because
    # this pattern has no temporal dimension.
    grouping = resolve_output_grouping(config)
    group_keys = ["PATID"] if grouping == "patient" else ["PATID", "geoid"]

    records = []
    for key, grp in joined.groupby(group_keys):
        key_tuple = key if isinstance(key, tuple) else (key,)
        weights = grp["overlap_days"].values.astype(float)
        row: dict = dict(zip(group_keys, key_tuple))
        for col in value_cols:
            if col in grp.columns:
                row[col] = _wtd_mean(grp[col].values, weights)
            else:
                row[col] = float("nan")
        records.append(row)

    result = pd.DataFrame(records) if records else pd.DataFrame(columns=[*group_keys, *value_cols])

    # Apply post-aggregation fill_na (e.g. dist_coast_m → 99999 for NHD)
    if config.exposure and config.exposure.fill_na:
        for col, fill_value in config.exposure.fill_na.items():
            if col in result.columns:
                result[col] = result[col].fillna(fill_value)

    return write_table(result, config.output.path)
