# common_v2/linkage/static_areal_linkage.py
from __future__ import annotations
from pathlib import Path
from spacescans.io.readers import read_table
from spacescans.io.writers import write_table
from spacescans.linkage.helpers import apply_transforms, load_patients, load_weights, prepare_episodes, resolve_output_grouping
from spacescans.models.config import DatasetConfig
from spacescans.models.protocols import AggregationEngine
from spacescans.models.specs import DurationWeightedSpec, JoinSpec, WeightedAggSpec
from spacescans.pipeline.registry import get_reader, register_pattern


@register_pattern("static_areal")
def run_static_areal(config: DatasetConfig, engine: AggregationEngine) -> Path:
    patients = load_patients(config)
    weights = load_weights(config.source.file, key=config.source.key, weight_col=config.engine.weight_col)

    # Load exposure: use plugin if specified, otherwise read as table
    if config.plugin:
        reader_cls = get_reader(config.plugin)
        reader = reader_cls(config)
        exposure = reader.load_exposure()
    else:
        exposure = read_table(config.exposure.file, key=config.exposure.key)
    exposure = apply_transforms(exposure, config.transforms, target="exposure")
    joined = engine.join(
        weights,
        exposure,
        JoinSpec(
            left_key=config.source.join_col,
            right_key=config.exposure.join_col,
            how="left",
        ),
    )
    geoid_values = engine.weighted_aggregate(
        joined,
        WeightedAggSpec(
            group_by=config.buffer.geoid_col,
            value_cols=config.exposure.value_cols,
            weight_col=config.engine.weight_col,
        ),
    )
    episodes = prepare_episodes(patients)

    grouping = resolve_output_grouping(config)
    group_by_episode = grouping == "episode"

    result = engine.duration_weighted(
        geoid_values,
        episodes,
        DurationWeightedSpec(
            value_cols=config.exposure.value_cols,
            group_by_episode=group_by_episode,
        ),
    )
    return write_table(result, config.output.path)
