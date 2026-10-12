"""User-uploaded exposures ("custom exposomes"): validation and definitions.

A user brings their own values, a CSV keyed by a Census geography (Tract, BG,
ZCTA5, County) or a GeoTIFF, and the dataset links like a shipped variable. This package
holds the domain half of that path, shared by the web app and the R package
(through the bridge). Storage, ownership and the per-user library stay in the
web.

Everything imports with the base install; the raster functions need the
``[geo]`` extra and raise :class:`spacescans._extras.MissingExtraError` without it.
"""
from __future__ import annotations

from spacescans.custom.errors import CustomExposomeError, RasterError
from spacescans.custom.keys import (
    EXPERIMENT_KEY,
    KEY_PREFIX,
    TASK_CUSTOM_KEY,
    dataset_id_from_key,
    derive_dataset_id,
    is_custom_key,
    is_dataset_id,
    variable_key,
)
from spacescans.custom.raster import (
    CONUS_BBOX,
    FULL_READ_MAX_CELLS,
    MAX_BANDS_LISTED,
    extract_bands,
    extract_values,
    grid_hash,
    inspect_raster,
    same_grid,
)
from spacescans.custom.table import (
    BOUNDARY_SPEC,
    MAX_VALUE_COLS,
    parse_csv,
    preview_csv,
    validate_csv,
)
from spacescans.custom.units import clean_unit, summarise_units, unit_as_typed

__all__ = [
    "CustomExposomeError", "RasterError",
    "EXPERIMENT_KEY", "KEY_PREFIX", "TASK_CUSTOM_KEY", "variable_key",
    "dataset_id_from_key", "is_custom_key", "is_dataset_id", "derive_dataset_id",
    "BOUNDARY_SPEC", "MAX_VALUE_COLS", "parse_csv", "preview_csv", "validate_csv",
    "clean_unit", "unit_as_typed", "summarise_units",
    "CONUS_BBOX", "MAX_BANDS_LISTED", "FULL_READ_MAX_CELLS", "inspect_raster",
    "grid_hash", "same_grid", "extract_values", "extract_bands",
]
