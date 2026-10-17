# common_v2/linkage/grid_weights_linkage.py
from __future__ import annotations

from spacescans._extras import require
require("geo", "geopandas", "rasterio", "shapely", "exactextract")

from pathlib import Path
import numpy as np
import geopandas as gpd
from spacescans.geometry.buffers import build_buffers, reproject
from spacescans.geometry.grid_weights import extract_grid_weights, validate_weight_sums
from spacescans.io.readers import read_table
from spacescans.io.spatial_readers import read_raster_metadata
from spacescans.io.writers import write_table
from spacescans.linkage.helpers import load_patients
from spacescans.models.config import DatasetConfig, SpatialFixTransform
from spacescans.models.protocols import AggregationEngine
from spacescans.transforms.spatial_fix import assign_crs
from spacescans.pipeline.registry import register_pattern


@register_pattern("grid_weights")
def run_grid_weights(config: DatasetConfig, engine: AggregationEngine) -> Path:
    patients = load_patients(config)
    raster_path = (
        config.source.file
        if isinstance(config.source.file, str)
        else config.source.file[0]
    )
    meta = read_raster_metadata(raster_path)
    for t in config.transforms:
        if isinstance(t, SpatialFixTransform) and t.target == "source":
            # Only pass crs/extent when the specific fix_type provides them
            crs = t.crs if t.crs else meta.crs
            extent = tuple(t.extent) if t.extent else None
            meta = assign_crs(
                np.empty(0),
                meta,
                crs=crs,
                extent=extent,
            )
    patient_points = gpd.GeoDataFrame(
        patients,
        geometry=gpd.points_from_xy(
            patients[config.buffer.long_col],
            patients[config.buffer.lat_col],
        ),
        crs="EPSG:4326",
    )
    buffers = build_buffers(patient_points, buffer_m=config.buffer.buffer_m)
    buffers_rcrs = reproject(buffers, target_crs=meta.crs)
    weights = extract_grid_weights(
        buffers_rcrs, raster_path, grid_id_offset=config.buffer.grid_id_offset,
        nodata_free=config.buffer.grid_weights_nodata_free,
    )
    validate_weight_sums(weights)
    return write_table(weights, config.output.path)
