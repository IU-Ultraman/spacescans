"""Yearly areal linkage with BG vintage dispatch.

For exposures (e.g. NDI) where the source data is split across two Census BG
vintages (2010 and 2020), each row tagged with a `vintage_col` value of 2010
or 2020. Each vintage joins to its own C3 weight table (2010 weights from
`source`, 2020 weights from `source_2020`); the per-(geoid, year) results are
concatenated and time-aggregated per patient.

Year-vintage assignment is data-driven via the `vintage_col` column in the
exposure table — no hard-coded year thresholds in this linkage. This keeps
the pattern reusable for any dual-vintage BG exposure.
"""
from __future__ import annotations
from pathlib import Path

import pandas as pd

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


def _disambiguate(df: pd.DataFrame, col: str, reserved: set[str]) -> tuple[pd.DataFrame, str]:
    """Rename df's column if it collides case-insensitively with reserved names.

    DuckDB's pandas-frame registration silently renames duplicated columns
    (e.g. 'GEOID' alongside 'geoid' becomes 'GEOID_1'), which breaks JOIN ON
    clauses since w.GEOID then resolves to the lowercase 'geoid' column.
    """
    if col.lower() in {r.lower() for r in reserved}:
        new = f"{col}_bgkey"
        return df.rename(columns={col: new}), new
    return df, col


@register_pattern("yearly_areal_bg_vintage")
def run_yearly_areal_bg_vintage(config: DatasetConfig, engine: AggregationEngine) -> Path:
    # Required vintage-dispatch fields
    if config.source_2020 is None:
        raise ValueError("yearly_areal_bg_vintage requires `source_2020:` in YAML (2020 BG weights)")
    if config.exposure is None:
        raise ValueError("yearly_areal_bg_vintage requires `exposure:` in YAML")
    ec = config.exposure
    if ec.vintage_col is None or ec.join_col_2010 is None or ec.join_col_2020 is None:
        raise ValueError(
            "yearly_areal_bg_vintage requires exposure.vintage_col, "
            "exposure.join_col_2010, and exposure.join_col_2020"
        )

    patients = load_patients(config)
    w_2010 = load_weights(
        config.source.file, key=config.source.key, weight_col=config.engine.weight_col,
    )
    w_2020 = load_weights(
        config.source_2020.file, key=config.source_2020.key, weight_col=config.engine.weight_col,
    )

    # Disambiguate weight join_col vs buffer.geoid_col (DuckDB case-insensitive collision)
    reserved = {config.buffer.geoid_col}
    w_2010, src_key_2010 = _disambiguate(w_2010, config.source.join_col, reserved)
    w_2020, src_key_2020 = _disambiguate(w_2020, config.source_2020.join_col, reserved)

    exposure = read_table(ec.file, key=ec.key)
    exposure = apply_transforms(exposure, config.transforms, target="exposure")

    year_col = ec.year_col or "year"
    vintage_col = ec.vintage_col

    # Split exposure rows by BG vintage
    mask_2010 = exposure[vintage_col] == 2010
    mask_2020 = exposure[vintage_col] == 2020
    exp_2010 = exposure[mask_2010]
    exp_2020 = exposure[mask_2020]

    geoid_year_frames: list[pd.DataFrame] = []

    if len(exp_2010) > 0:
        joined_2010 = engine.join(
            w_2010,
            exp_2010,
            JoinSpec(
                left_key=src_key_2010,
                right_key=ec.join_col_2010,
                how="left",
            ),
        )
        gy_2010 = engine.weighted_aggregate(
            joined_2010,
            WeightedAggSpec(
                group_by=[config.buffer.geoid_col, year_col],
                value_cols=ec.value_cols,
                weight_col=config.engine.weight_col,
            ),
        )
        geoid_year_frames.append(gy_2010)

    if len(exp_2020) > 0:
        joined_2020 = engine.join(
            w_2020,
            exp_2020,
            JoinSpec(
                left_key=src_key_2020,
                right_key=ec.join_col_2020,
                how="left",
            ),
        )
        gy_2020 = engine.weighted_aggregate(
            joined_2020,
            WeightedAggSpec(
                group_by=[config.buffer.geoid_col, year_col],
                value_cols=ec.value_cols,
                weight_col=config.engine.weight_col,
            ),
        )
        geoid_year_frames.append(gy_2020)

    if not geoid_year_frames:
        raise ValueError(
            f"No rows matched vintage_col={vintage_col} == 2010 or 2020 in exposure"
        )
    geoid_year = pd.concat(geoid_year_frames, ignore_index=True)

    episodes = build_episode_periods(patients, years=config.time.years)
    episode_exp = engine.join(
        episodes,
        geoid_year,
        JoinSpec(
            left_key=["geoid", "period_id"],
            right_key=[config.buffer.geoid_col, year_col],
            how="left",
        ),
    )
    grouping = resolve_output_grouping(config)
    if grouping == "patient":
        group_by_keys = ["PATID"]
    else:  # "episode"
        group_by_keys = ["PATID", "geoid"]

    result = engine.temporal_aggregate(
        episode_exp,
        TemporalAggSpec(
            group_by=group_by_keys,
            period_col="period_id",
            value_cols=ec.value_cols,
            weight_col="overlap_days",
        ),
    )
    return write_table(result, config.output.path)
