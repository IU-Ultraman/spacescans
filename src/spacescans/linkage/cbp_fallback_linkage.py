"""CBP fallback linkage — county-level CBP for patients missing ZBP, ZBP for the rest.

Matches v1 C4_Linkage_COUNTY_CBP.py:
1. Load linked ZBP output (zbp.pkl)
2. Patients where zbp_r_civic is NaN → run county-level yearly_areal
3. Patients where zbp_r_bowling is not NaN → use ZBP values (strip zbp_ prefix)
4. Concat both groups → 1000 patients
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from spacescans.engine.duckdb_engine import DuckDBEngine
from spacescans.io.readers import read_table
from spacescans.io.writers import write_table
from spacescans.linkage.helpers import (
    apply_transforms,
    build_episode_periods,
    load_patients,
    load_weights,
    resolve_output_grouping,
)
from spacescans.models.config import DatasetConfig
from spacescans.models.protocols import AggregationEngine
from spacescans.models.specs import JoinSpec, TemporalAggSpec, WeightedAggSpec
from spacescans.pipeline.registry import register_pattern


@register_pattern("cbp_fallback")
def run_cbp_fallback(config: DatasetConfig, engine: AggregationEngine) -> Path:
    """County CBP with ZBP fallback."""
    patients = load_patients(config)
    weights = load_weights(config.source.file, weight_col=config.engine.weight_col)
    exposure = read_table(config.exposure.file, key=config.exposure.key)
    exposure = apply_transforms(exposure, config.transforms, target="exposure")

    # Load linked ZBP
    zbp_path = config.exposure.zbp_file
    linked_zbp = read_table(zbp_path)
    linked_zbp["PATID"] = linked_zbp["PATID"].astype(str)

    # Compatibility: v1 zbp.pkl has "zbp_" prefix; v2 yearly_areal output does not.
    # If prefix missing, add it so the downstream strip logic works uniformly.
    if "zbp_r_civic" not in linked_zbp.columns and "r_civic" in linked_zbp.columns:
        linked_zbp = linked_zbp.rename(
            columns={c: f"zbp_{c}" for c in linked_zbp.columns if c != "PATID"}
        )

    # Split patients: those missing ZBP (zbp_r_civic is NaN) get county CBP
    zbp_missing_col = "zbp_r_civic"
    zbp_valid_col = "zbp_r_bowling"
    missing_pats = linked_zbp.loc[linked_zbp[zbp_missing_col].isna(), "PATID"].unique()

    patients["PATID"] = patients["PATID"].astype(str)
    patients_cbp = patients[patients["PATID"].isin(missing_pats)].copy()

    # --- County CBP for patients without ZBP ---
    value_cols = config.exposure.value_cols
    year_col = config.exposure.year_col or "year"
    # Honor output_grouping like yearly_areal/fara (Sprint 11): "patient"
    # keeps the v1 per-PATID collapse; "episode" preserves the synthetic
    # per-row `geoid`, so BOTH the county-CBP and ZBP-valid halves emit one
    # row per (patient, episode) and stay consistent with the rest of C4.
    grouping = resolve_output_grouping(config)
    group_by_keys = ["PATID", "geoid"] if grouping == "episode" else ["PATID"]
    episodes = build_episode_periods(patients_cbp, years=config.time.years)

    joined = engine.join(
        weights, exposure,
        JoinSpec(left_key=config.source.join_col, right_key=config.exposure.join_col, how="left"),
    )
    geoid_year = engine.weighted_aggregate(
        joined,
        WeightedAggSpec(group_by=[config.buffer.geoid_col, year_col], value_cols=value_cols, weight_col=config.engine.weight_col),
    )
    geoid_year = geoid_year.dropna(subset=[year_col])
    geoid_year[year_col] = geoid_year[year_col].astype(int)
    episode_exp = engine.join(
        episodes, geoid_year,
        JoinSpec(left_key=["geoid", "period_id"], right_key=[config.buffer.geoid_col, year_col], how="left"),
    )
    cbp_result = engine.temporal_aggregate(
        episode_exp,
        TemporalAggSpec(group_by=group_by_keys, period_col="period_id", value_cols=value_cols, weight_col="overlap_days"),
    )

    # --- ZBP-valid patients: strip zbp_ prefix ---
    zbp_valid = linked_zbp[linked_zbp[zbp_valid_col].notna()].copy()
    zbp_valid.columns = [
        c.replace("zbp_", "") if c.startswith("zbp_") else c
        for c in zbp_valid.columns
    ]
    zbp_valid["PATID"] = zbp_valid["PATID"].astype(str)
    # Keep the grouping keys (PATID [+ geoid] in episode mode) + value cols.
    # In episode mode geoid survives the zbp_ prefix round-trip above.
    keep_cols = group_by_keys + [c for c in value_cols if c in zbp_valid.columns]
    zbp_valid = zbp_valid[keep_cols]

    # Concat
    result = pd.concat([cbp_result, zbp_valid], ignore_index=True)

    return write_table(result, config.output.path)
