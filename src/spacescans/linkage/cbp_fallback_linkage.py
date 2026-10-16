"""CBP fallback linkage — county-level CBP for episodes missing ZBP, ZBP for the rest.

Matches v1 C4_Linkage_COUNTY_CBP.py:
1. Load linked ZBP output (zbp.pkl)
2. Units where zbp_r_civic is NaN → run county-level yearly_areal
3. All other units → use ZBP values (strip zbp_ prefix)
4. Concat both groups → one row per unit

The unit of the split is the location episode. Up to v0.2.6 the split was made
per patient, so in episode mode a patient with one ZBP-less episode sent every
episode to the county branch while the ZBP-valid ones were also kept: those
episodes came out twice with different values. The two branches are now exact
complements.

Participant grouping (``output_grouping: patient``) keeps the per-episode
split and then averages a participant's episodes, each weighted by the days
that contributed to its value (the participant formula every other pattern
uses). That needs the ZBP input at episode grouping with its contributing days
(``time: {output_grouping: episode, contributing_days: true}`` on the ZBP
step, as the catalog renders it and the init-config templates set it). Given a
ZBP input with one row per patient instead, the v1 rule applies (a patient
falls back to county CBP only when none of their episodes has ZBP) and a
warning says so.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pandas as pd

from spacescans.io.readers import read_table
from spacescans.io.writers import write_table
from spacescans.linkage.helpers import (
    DAYS_SUFFIX,
    apply_transforms,
    build_episode_periods,
    load_patients,
    load_weights,
    participant_means,
    resolve_output_grouping,
)
from spacescans.models.config import DatasetConfig
from spacescans.models.protocols import AggregationEngine
from spacescans.models.specs import JoinSpec, TemporalAggSpec, WeightedAggSpec
from spacescans.pipeline.registry import register_pattern

_ZBP_MISSING_COL = "zbp_r_civic"

LEGACY_PATIENT_SPLIT_WARNING = (
    "cbp_fallback: the ZBP input has one row per patient, so the v1 rule applies: a "
    "participant falls back to county CBP only when none of their episodes has ZBP. For "
    "the per-episode fallback, link ZBP with time.output_grouping: episode and "
    "time.contributing_days: true"
)


def _zbp_geoid_col(linked_zbp: pd.DataFrame) -> str | None:
    for col in ("zbp_geoid", "geoid"):
        if col in linked_zbp.columns:
            return col
    return None


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
    if _ZBP_MISSING_COL not in linked_zbp.columns and "r_civic" in linked_zbp.columns:
        linked_zbp = linked_zbp.rename(
            columns={c: f"zbp_{c}" for c in linked_zbp.columns if c != "PATID"}
        )

    grouping = resolve_output_grouping(config)
    value_cols = list(config.exposure.value_cols)
    geo = _zbp_geoid_col(linked_zbp)
    zbp_days = {c: f"zbp_{c}{DAYS_SUFFIX}" for c in value_cols}
    # participant grouping from per-episode ZBP rows and their contributing days
    exact = (grouping == "patient" and geo is not None
             and all(d in linked_zbp.columns for d in zbp_days.values()))
    per_episode = grouping == "episode" or exact
    if grouping == "episode" and geo is None:
        raise ValueError("cbp_fallback at episode grouping needs the ZBP input per episode "
                         "(link ZBP with time.output_grouping: episode)")
    if grouping == "patient" and not exact:
        warnings.warn(LEGACY_PATIENT_SPLIT_WARNING, stacklevel=2)
    # The rows of each branch: (PATID, geoid) per episode, PATID per patient (v1).
    branch_keys = ["PATID", "geoid"] if per_episode else ["PATID"]

    # Split units: those missing ZBP (zbp_r_civic is NaN) get county CBP; the
    # rest keep ZBP. The unit follows the split, so the halves never overlap.
    missing = linked_zbp[_ZBP_MISSING_COL].isna()
    patients["PATID"] = patients["PATID"].astype(str)
    if per_episode:
        missing_keys = set(zip(linked_zbp.loc[missing, "PATID"],
                               linked_zbp.loc[missing, geo].astype("int64")))
        in_missing = [
            (p, int(g)) in missing_keys
            for p, g in zip(patients["PATID"], patients[config.buffer.geoid_col])
        ]
        patients_cbp = patients[in_missing].copy()
    else:
        missing_pats = linked_zbp.loc[missing, "PATID"].unique()
        patients_cbp = patients[patients["PATID"].isin(missing_pats)].copy()

    # --- County CBP for units without ZBP ---
    year_col = config.exposure.year_col or "year"
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
        TemporalAggSpec(group_by=branch_keys, period_col="period_id", value_cols=value_cols,
                        weight_col="overlap_days",
                        days_suffix=DAYS_SUFFIX if exact else None),
    )

    # --- ZBP-valid units (the complement of the missing ones): strip zbp_ prefix ---
    zbp_valid = linked_zbp[~missing].copy()
    zbp_valid.columns = [
        c.replace("zbp_", "") if c.startswith("zbp_") else c
        for c in zbp_valid.columns
    ]
    zbp_valid["PATID"] = zbp_valid["PATID"].astype(str)
    # Keep the unit keys (PATID [+ geoid] per episode) + value cols (+ their days).
    # geoid survives the zbp_ prefix round-trip above.
    kept = [c for c in value_cols if c in zbp_valid.columns]
    if exact:
        kept += [f"{c}{DAYS_SUFFIX}" for c in value_cols]
    zbp_valid = zbp_valid[branch_keys + kept]

    # Concat
    result = pd.concat([cbp_result, zbp_valid], ignore_index=True)
    if exact:
        result = participant_means(result, value_cols)

    return write_table(result, config.output.path)
