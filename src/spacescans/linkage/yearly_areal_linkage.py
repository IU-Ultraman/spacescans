# common_v2/linkage/yearly_areal_linkage.py
from __future__ import annotations
from pathlib import Path
from spacescans.io.readers import read_table
from spacescans.io.writers import write_table
from spacescans.linkage.helpers import apply_transforms, build_episode_periods, load_patients, load_weights, resolve_output_grouping
from spacescans.models.config import DatasetConfig
from spacescans.models.protocols import AggregationEngine
from spacescans.models.specs import JoinSpec, TemporalAggSpec, WeightedAggSpec
from spacescans.pipeline.registry import register_pattern


@register_pattern("yearly_areal")
def run_yearly_areal(config: DatasetConfig, engine: AggregationEngine) -> Path:
    patients = load_patients(config)
    weights = load_weights(config.source.file, key=config.source.key, weight_col=config.engine.weight_col)
    exposure = read_table(config.exposure.file, key=config.exposure.key)
    exposure = apply_transforms(exposure, config.transforms, target="exposure")
    episodes = build_episode_periods(patients, years=config.time.years)
    year_col = config.exposure.year_col or "year"
    joined = engine.join(
        weights,
        exposure,
        JoinSpec(
            left_key=config.source.join_col,
            right_key=config.exposure.join_col,
            how="left",
        ),
    )
    geoid_year = engine.weighted_aggregate(
        joined,
        WeightedAggSpec(
            group_by=[config.buffer.geoid_col, year_col],
            value_cols=config.exposure.value_cols,
            weight_col=config.engine.weight_col,
        ),
    )
    episode_exp = engine.join(
        episodes,
        geoid_year,
        JoinSpec(
            left_key=["geoid", "period_id"],
            right_key=[config.buffer.geoid_col, year_col],
            how="left",
        ),
    )
    # Dispatch on TimeConfig.output_grouping: "patient" keeps the v1
    # per-PATID collapse; "episode" preserves the synthetic per-row
    # `geoid` (== episode_id when upstream supplies one).
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
            value_cols=config.exposure.value_cols,
            weight_col="overlap_days",
        ),
    )
    return write_table(result, config.output.path)
