"""ACAG multi-pollutant linkage — processes 16 pollutants independently then merges.

Matches v1 C4_Linkage_ACAG.py: for each pollutant directory, extracts biweekly
grid values, runs SQL-based spatial+temporal weighted average, merges all
pollutants on PATID, and derives _nbm (non-biomass) columns.
"""
from __future__ import annotations

from spacescans._extras import require
require("geo", "geopandas", "rasterio")
require("nc", "xarray", "netCDF4")

import os
import warnings
from pathlib import Path

import pandas as pd

from spacescans.io.readers import read_table
from spacescans.io.writers import write_table
from spacescans.linkage.helpers import load_patients, load_weights, resolve_output_grouping
from spacescans.models.config import DatasetConfig
from spacescans.models.protocols import AggregationEngine
from spacescans.models.specs import DateRangeJoinSpec, JoinSpec, TemporalAggSpec, WeightedAggSpec
from spacescans.pipeline.registry import register_pattern
from spacescans.plugins.readers.acag import ACAGExposureSource, _BASE_MAP, _BM_MAP

# All 16 pollutants in v1 order
_POLLUTANTS = list(_BASE_MAP.keys()) + list(_BM_MAP.keys())


@register_pattern("acag_multi")
def run_acag_multi(config: DatasetConfig, engine: AggregationEngine) -> Path:
    """Process all ACAG pollutants, merge, derive _nbm columns."""
    patients = load_patients(config)
    weights = load_weights(config.source.file, weight_col="weight")

    acag_root = Path(config.exposure.file)  # e.g. data/ACAG/C4/xNorthAmerica
    years = config.time.years if config.time else list(range(2013, 2020))
    # Same dispatch as gridded_linkage: "episode" keeps one row per
    # (PATID, geoid) so spacescans-web can join results back onto its
    # per-row episode_id; "patient" (the v1/CLI default) collapses to PATID.
    grouping = resolve_output_grouping(config) if config.time is not None else "patient"
    group_by_keys = ["PATID"] if grouping == "patient" else ["PATID", "geoid"]

    results = {}
    for poll in _POLLUTANTS:
        # Resolve pollutant directory
        subdir = _BM_MAP.get(poll) if poll.endswith("_bm") else _BASE_MAP.get(poll)
        poll_dir = acag_root / subdir / "BiWeekly"
        if not poll_dir.is_dir():
            warnings.warn(f"Skipping {poll}: {poll_dir} not found")
            continue

        print(f"  Processing: {poll} ({poll_dir})", flush=True)

        # Create a temporary config-like object for the reader
        class _TempConfig:
            pass
        tc = _TempConfig()
        tc.source = config.source
        tc.exposure = type(config.exposure).model_construct(
            file=str(poll_dir),
            join_col="grid_id",
            value_cols=["value"],
            start_col="start_date",
            end_col="end_date",
        )

        reader = ACAGExposureSource(tc)
        exposure = reader.load_exposure(years=years)
        if exposure.empty:
            warnings.warn(f"  No data for {poll}")
            continue

        # Spatial weighted average: grid → geoid × biweek
        joined = engine.join(
            weights, exposure,
            JoinSpec(left_key="grid_id", right_key="grid_id", how="inner"),
        )
        geoid_biweek = engine.weighted_aggregate(
            joined,
            WeightedAggSpec(
                group_by=["geoid", "start_date", "end_date"],
                value_cols=["value"],
                weight_col="weight",
                output_suffix="_aw",
            ),
        )

        # Temporal weighted average: geoid×biweek → patient
        from spacescans.linkage.helpers import prepare_episodes
        episodes = prepare_episodes(patients)
        matched = engine.date_range_join(
            geoid_biweek, episodes,
            DateRangeJoinSpec(left_start_col="start_date", left_end_col="end_date"),
        )
        patient_twa = engine.temporal_aggregate(
            matched,
            TemporalAggSpec(
                group_by=group_by_keys,
                period_col="start_date",
                value_cols=["value_aw"],
                weight_col="overlap_days",
            ),
        )
        patient_twa = patient_twa.rename(columns={"value_aw": poll})
        patient_twa["PATID"] = patient_twa["PATID"].astype(str)
        results[poll] = patient_twa[group_by_keys + [poll]]
        print(f"  Done: {poll} ({len(patient_twa)} patients)", flush=True)

    if not results:
        return write_table(pd.DataFrame(columns=group_by_keys), config.output.path)

    # Merge all pollutants on PATID (outer join)
    acag = None
    for poll, df in results.items():
        acag = df if acag is None else acag.merge(df, on=group_by_keys, how="outer")

    # Derive _nbm columns: base - biomass
    for base in _BASE_MAP:
        bm_col = base + "_bm"
        nbm_col = base + "_nbm"
        if base in acag.columns and bm_col in acag.columns:
            acag[nbm_col] = acag[base] - acag[bm_col]

    return write_table(acag, config.output.path)
