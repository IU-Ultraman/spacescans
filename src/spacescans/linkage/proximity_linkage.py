# common_v2/linkage/proximity_linkage.py
"""Nearest-feature distance: from each location to the nearest point, line or polygon.

The generic proximity pattern, for hand-written YAML configs (``spacescans run``).
The catalog's road and water distances have their own tiled C3 patterns
(``tiger_proximity``, ``nhd_proximity``), and registered ``features`` datasets
(:mod:`spacescans.custom`) do not use this pattern either: they compute the same
exact distance in :mod:`spacescans.custom.features` at the start of a link and
aggregate it with ``static_areal`` / ``yearly_areal``. Config:

* ``source.file``: the features — one vector file (``.parquet`` is read as
  GeoParquet, anything else with ``geopandas.read_file``), or for a yearly
  source a list of files, one per year of ``time.years`` in that order.
  ``source.year_col`` instead selects each year's features from one file by
  that attribute. ``source.layer`` picks a layer. The features need a CRS.
* ``exposure.value_cols[0]``: the output column (default ``distance_m``).
  ``source.category_col``: one column per category value,
  ``<column>_<category>``, each the distance to the nearest feature of that
  category.
* Distance in metres in ``buffer.aea_crs``, to the nearest feature anywhere
  (no search radius; a point inside a polygon is 0). Episodes with missing
  coordinates get no distance.
* Time: without a time block or with ``temporal_mode: static`` the distance is
  time-invariant and an episode's weight is its length (end - start + 1 days);
  with ``temporal_mode: yearly`` there is one distance per episode and year of
  ``time.years``, weighted by the episode's days in that year.
  ``time.output_grouping``: ``episode`` writes ``PATID, geoid, <column>``,
  ``patient`` (the default) ``PATID, <column>``: the participant's day-weighted
  mean over their episodes (and years) with a distance.
"""
from __future__ import annotations

from spacescans._extras import require
require("geo", "geopandas", "rasterio", "shapely", "exactextract")

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from spacescans.io.spatial_readers import read_vector
from spacescans.io.writers import write_table
from spacescans.linkage.helpers import (
    apply_transforms,
    build_episode_periods,
    load_patients,
    prepare_episodes,
    resolve_output_grouping,
)
from spacescans.models.config import DatasetConfig
from spacescans.models.protocols import AggregationEngine
from spacescans.models.specs import DurationWeightedSpec, JoinSpec, TemporalAggSpec
from spacescans.pipeline.registry import register_pattern

DEFAULT_VALUE_COL = "distance_m"


def _read_features(path: str, layer: str | None) -> gpd.GeoDataFrame:
    if str(path).lower().endswith((".parquet", ".geoparquet")):
        features = gpd.read_parquet(path)
    else:
        features = read_vector(path, layer=layer)
    if features.crs is None:
        raise ValueError(f"proximity: the features in {Path(path).name} have no CRS")
    return features


def _nearest(points_m: gpd.GeoDataFrame, features_m: gpd.GeoDataFrame | None) -> np.ndarray:
    """Distance from each point to the nearest feature (NaN when there is none)."""
    out = np.full(len(points_m), np.nan)
    if features_m is None or len(features_m) == 0 or len(points_m) == 0:
        return out
    feats = features_m[features_m.geometry.notna() & ~features_m.geometry.is_empty]
    pts = points_m[points_m.geometry.notna() & ~points_m.geometry.is_empty]
    if len(feats) == 0 or len(pts) == 0:
        return out
    joined = gpd.sjoin_nearest(
        pts[["geometry"]], feats[["geometry"]].reset_index(drop=True),
        how="left", distance_col="_d",
    )
    # ties return several rows per point; the distance is the same
    per_point = joined["_d"].astype(float).groupby(level=0).min()
    out[points_m.index.get_indexer(per_point.index)] = per_point.to_numpy()
    return out


def _distance_columns(points_m: gpd.GeoDataFrame, features_m: gpd.GeoDataFrame,
                      value_col: str, category_col: str | None) -> dict[str, np.ndarray]:
    if category_col is None:
        return {value_col: _nearest(points_m, features_m)}
    cols: dict[str, np.ndarray] = {}
    for cat, group in features_m.groupby(category_col, sort=True):
        cols[f"{value_col}_{cat}"] = _nearest(points_m, group)
    return cols


def _yearly_features(config: DatasetConfig, years: list[int], metric_crs: str):
    """``{year: features in metric_crs}`` for a yearly source."""
    src = config.source
    files = src.file if isinstance(src.file, list) else [src.file]
    if len(files) > 1:
        if len(files) != len(years):
            raise ValueError(f"proximity: {len(files)} feature files for {len(years)} years; "
                             "give one file per year of time.years, in that order")
        return {y: _read_features(f, src.layer).to_crs(metric_crs) for y, f in zip(years, files)}
    features = _read_features(files[0], src.layer).to_crs(metric_crs)
    if src.year_col is None:
        return {y: features for y in years}
    if src.year_col not in features.columns:
        raise ValueError(f"proximity: no column {src.year_col!r} in the features")
    year_of = pd.to_numeric(features[src.year_col], errors="coerce")
    return {y: features[year_of == y] for y in years}


@register_pattern("proximity")
def run_proximity(config: DatasetConfig, engine: AggregationEngine) -> Path:
    patients = load_patients(config)
    if config.buffer.geoid_col != "geoid":     # the episode key every C4 pattern writes
        patients = patients.rename(columns={config.buffer.geoid_col: "geoid"})
    grouping = resolve_output_grouping(config) if config.time is not None else "patient"
    temporal_mode = config.time.temporal_mode if config.time else "static"
    value_col = (config.exposure.value_cols[0]
                 if config.exposure is not None and config.exposure.value_cols else DEFAULT_VALUE_COL)
    category_col = config.source.category_col
    metric_crs = config.buffer.aea_crs

    lon = pd.to_numeric(patients[config.buffer.long_col], errors="coerce")
    lat = pd.to_numeric(patients[config.buffer.lat_col], errors="coerce")
    located = lon.notna() & lat.notna()
    points = gpd.GeoDataFrame(
        {"geoid": patients.loc[located, "geoid"].to_numpy()},
        geometry=gpd.points_from_xy(lon[located], lat[located]),
        crs="EPSG:4326",
    ).to_crs(metric_crs)

    def transformed(features: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        return apply_transforms(features, config.transforms, target="source")

    if temporal_mode == "yearly":
        years = list(config.time.years or [])
        if not years:
            raise ValueError("proximity: a yearly source needs time.years")
        frames = []
        for year, features in _yearly_features(config, years, metric_crs).items():
            cols = _distance_columns(points, transformed(features), value_col, category_col)
            frames.append(pd.DataFrame({"geoid": points["geoid"].to_numpy(), "year": year, **cols}))
        distances = pd.concat(frames, ignore_index=True)
        value_cols = [c for c in distances.columns if c not in ("geoid", "year")]
        periods = build_episode_periods(patients, years=years)
        joined = engine.join(periods, distances,
                             JoinSpec(left_key=["geoid", "period_id"], right_key=["geoid", "year"],
                                      how="left"))
        keys = ["PATID"] if grouping == "patient" else ["PATID", "geoid"]
        result = engine.temporal_aggregate(
            joined, TemporalAggSpec(group_by=keys, period_col="period_id",
                                    value_cols=value_cols, weight_col="overlap_days"))
    else:
        files = config.source.file if isinstance(config.source.file, list) else [config.source.file]
        if len(files) != 1:
            raise ValueError("proximity: a static source takes one feature file; a list of "
                             "files is one per year (time.temporal_mode: yearly)")
        features = transformed(_read_features(files[0], config.source.layer).to_crs(metric_crs))
        cols = _distance_columns(points, features, value_col, category_col)
        distances = pd.DataFrame({"geoid": points["geoid"].to_numpy(), **cols})
        value_cols = list(cols)
        result = engine.duration_weighted(
            distances,
            prepare_episodes(patients),
            DurationWeightedSpec(value_cols=value_cols, group_by_episode=grouping == "episode"),
        )
    return write_table(result, config.output.path)
