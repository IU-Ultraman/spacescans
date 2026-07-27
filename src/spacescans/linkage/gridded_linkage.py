# common_v2/linkage/gridded_linkage.py
from __future__ import annotations

from spacescans._extras import require
require("geo", "geopandas", "rasterio", "shapely", "exactextract")

from pathlib import Path
from spacescans.io.readers import read_table
from spacescans.io.writers import write_table
from spacescans.linkage.helpers import (
    apply_transforms,
    load_patients,
    prepare_episodes,
    resolve_output_grouping,
)
from spacescans.models.config import DatasetConfig
from spacescans.models.protocols import AggregationEngine
from spacescans.models.specs import DateRangeJoinSpec, JoinSpec, TemporalAggSpec, WeightedAggSpec
from spacescans.pipeline.registry import register_pattern, get_reader


@register_pattern("gridded")
def run_gridded(config: DatasetConfig, engine: AggregationEngine) -> Path:
    patients = load_patients(config)
    weights = read_table(config.source.file, key=config.source.key)

    # Load exposure: use plugin if specified, otherwise read as table
    if config.plugin:
        reader_cls = get_reader(config.plugin)
        reader = reader_cls(config)
        exposure = reader.load_exposure(years=config.time.years if config.time else None)
    else:
        exposure = read_table(config.exposure.file, key=config.exposure.key)
    exposure = apply_transforms(exposure, config.transforms, target="exposure")
    joined = engine.join(
        weights,
        exposure,
        JoinSpec(left_key="grid_id", right_key="grid_id", how="inner"),
    )

    # Determine temporal mode: daily (single date col) vs windowed (start_date + end_date)
    date_col = config.exposure.date_col
    start_col = config.exposure.start_col
    end_col = config.exposure.end_col
    is_daily = date_col is not None

    if is_daily:
        group_cols = ["geoid", date_col]
    else:
        group_cols = ["geoid", start_col, end_col]

    geoid_temporal = engine.weighted_aggregate(
        joined,
        WeightedAggSpec(
            group_by=group_cols,
            value_cols=config.exposure.value_cols,
            weight_col="weight",
            output_suffix="_aw",
        ),
    )

    episodes = prepare_episodes(patients)
    matched = engine.date_range_join(
        geoid_temporal,
        episodes,
        DateRangeJoinSpec(
            left_date_col=date_col if is_daily else None,
            left_start_col=start_col,
            left_end_col=end_col,
        ),
    )

    # Period identifier for temporal aggregation
    period_col = date_col if is_daily else start_col
    aw_cols = [f"{c}_aw" for c in config.exposure.value_cols]
    grouping = resolve_output_grouping(config)
    group_by_keys = ["PATID"] if grouping == "patient" else ["PATID", "geoid"]
    result = engine.temporal_aggregate(
        matched,
        TemporalAggSpec(
            group_by=group_by_keys,
            period_col=period_col,
            value_cols=aw_cols,
            weight_col="overlap_days",
        ),
    )
    # Drop the internal '_aw' suffix on the final output — the intermediate
    # area-weighted aggregation is a pipeline-internal detail; downstream
    # consumers (web _merge, R analysis) reference the user-facing value
    # column names (e.g. 'value' for VNL, 'uvddc' for TEMIS).
    rename_map = {aw: orig for aw, orig in zip(aw_cols, config.exposure.value_cols)}
    result = result.rename(columns=rename_map)
    return write_table(result, config.output.path)
