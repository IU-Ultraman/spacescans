"""Fast boundary_overlap variant (per-tile bulk rasterize + chunked exact_extract).

Drop-in replacement for `boundary_overlap` using the new R-style "fast" pattern.
Algorithm: see overlap.compute_overlap_weights_fast docstring.
"""
from __future__ import annotations

from spacescans._extras import require
require("geo", "geopandas", "rasterio", "shapely", "exactextract")

from pathlib import Path
import geopandas as gpd
import pandas as pd
from spacescans.geometry.buffers import build_buffers
from spacescans.geometry.overlap import compute_overlap_weights_fast
from spacescans.io.readers import read_table
from spacescans.io.spatial_readers import read_vector
from spacescans.io.writers import write_table
from spacescans.linkage.helpers import load_patients
from spacescans.models.config import DatasetConfig
from spacescans.models.protocols import AggregationEngine
from spacescans.pipeline.registry import register_pattern


@register_pattern("boundary_overlap_fast")
def run_boundary_overlap_fast(config: DatasetConfig, engine: AggregationEngine) -> Path:
    patients = load_patients(config)
    if isinstance(config.source.file, list):
        boundaries = gpd.GeoDataFrame(
            pd.concat(
                [read_vector(f, layer=config.source.layer) for f in config.source.file],
                ignore_index=True,
            )
        )
    else:
        boundaries = read_vector(config.source.file, layer=config.source.layer)
    patient_points = gpd.GeoDataFrame(
        patients,
        geometry=gpd.points_from_xy(
            patients[config.buffer.long_col],
            patients[config.buffer.lat_col],
        ),
        crs="EPSG:4326",
    )
    buffers = build_buffers(
        patient_points,
        buffer_m=config.buffer.buffer_m,
        buffer_resolution=config.buffer.buffer_resolution,
        target_crs=config.buffer.aea_crs,
    )
    boundaries = boundaries.to_crs(buffers.crs)
    weights = compute_overlap_weights_fast(
        buffers,
        boundaries,
        boundary_key_col=config.source.join_col,
        raster_res_m=config.buffer.raster_res_m,
        test_index_limit=config.buffer.test_index_limit,
        workers=config.engine.workers,
    )
    return write_table(weights, config.output.path)
