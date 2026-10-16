"""User-supplied exposures: registered datasets, upload definitions, steps.

A user brings their own values, either a CSV keyed by a Census geography
(Tract, BG, ZCTA5, County) or a GeoTIFF (one, or one per year, one or several
bands), and the dataset links like a catalog variable. Registered datasets
(:func:`register`, phase D) add daily tables, biweekly and daily rasters and
point/line/polygon features (distance to the nearest feature), prepared once
under a datasets root with their metadata and linked by name:
``orchestrate.link(cohort, [...], custom=["my dataset"])``. The pipeline needs no
new code for either: C4 is ``static_areal`` / ``yearly_areal`` reading a plain
table with no plugin; for a raster the table is first materialised from the C3
weights' grid ids and the pixels under them.

This package holds the domain half of that path, shared by the web app and the
R package (through the bridge). Storage, ownership and the per-user library
stay in the web.

Registered datasets
    :func:`register`, :func:`list_datasets`, :func:`get_dataset`,
    :func:`update_dataset`, :func:`remove_dataset`, :func:`dataset_definition`,
    :func:`resolve_custom`, :func:`datasets_root`, :class:`DatasetRecord`.
Upload
    :func:`preview_csv`, :func:`validate_csv`, :func:`define_polygon`;
    :func:`preview_raster`, :func:`inspect_raster`, :func:`define_raster`.
Run
    :func:`plan`, :func:`render`, :func:`prepare_step` (materialises raster
    values), :func:`legacy_cache_key`, :func:`diagnostics`.
Data
    :func:`provisioned_boundaries`.

Everything imports with the base install. CSV validation and polygon tables
need nothing more; the raster and features functions need the ``[geo]``
extra and raise :class:`spacescans._extras.MissingExtraError` without it.
"""
from __future__ import annotations

from spacescans.custom.boundaries import (
    BOUNDARY_DATA_DIR,
    provisioned_boundaries,
    unprovisioned,
)
from spacescans.custom.definition import (
    CATALOG_FIELDS,
    MAX_DESCRIPTION,
    MAX_LABEL,
    MAX_RASTER_YEARS,
    RESERVED_RASTER_COLS,
    STATIC_COVERAGE_YEARS,
    catalog_entry,
    check_band_columns,
    check_definition,
    check_label,
    default_band_columns,
    define_polygon,
    define_raster,
    feature_rows,
    is_raster,
    preview_raster,
    raster_bands,
)
from spacescans.custom.diagnose import (
    coverage_diagnosis,
    diagnostics,
    raster_diagnosis,
)
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
from spacescans.custom.features import nearest_distances
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
from spacescans.custom.registry import (
    DATASETS_DIR_ENV,
    KINDS,
    TEMPORAL,
    VISIBILITY,
    DatasetError,
    DatasetExistsError,
    DatasetNotFoundError,
    DatasetRecord,
    dataset_definition,
    datasets_root,
    get_dataset,
    link_overlay,
    list_datasets,
    register,
    remove_dataset,
    resolve_custom,
    update_dataset,
)
from spacescans.custom.steps import (
    BOUNDARY_C3,
    C3_GRID_TEMPLATE,
    C4_TEMPLATE,
    GRID_TAG_PREFIX,
    TEMPLATES,
    Context,
    RenderContext,
    Step,
    boundary_tag_for_step,
    c3_step_for,
    c4_step_for,
    definition_for,
    definitions_from_config,
    first_definition_for_grid,
    grid_tag,
    is_dated,
    is_features,
    kind_c4_step,
    legacy_cache_key,
    needs_c3,
    output_path,
    parquet_map,
    plan,
    prepare_step,
    render,
    values_path,
    weights_path,
)
from spacescans.custom.table import (
    BOUNDARY_SPEC,
    MAX_VALUE_COLS,
    parse_csv,
    preview_csv,
    validate_csv,
)
from spacescans.custom.units import clean_unit, summarise_units, unit_as_typed
from spacescans.custom.values import MaterialisedValues, materialise_values, value_table

__all__ = [
    # registered datasets
    "DATASETS_DIR_ENV", "KINDS", "TEMPORAL", "VISIBILITY", "DatasetRecord", "DatasetError",
    "DatasetNotFoundError", "DatasetExistsError", "register", "list_datasets", "get_dataset",
    "update_dataset", "remove_dataset", "dataset_definition", "resolve_custom", "datasets_root",
    "link_overlay", "nearest_distances",
    # errors
    "CustomExposomeError", "RasterError",
    # keys
    "EXPERIMENT_KEY", "KEY_PREFIX", "TASK_CUSTOM_KEY", "variable_key",
    "dataset_id_from_key", "is_custom_key", "is_dataset_id", "derive_dataset_id",
    # CSV
    "BOUNDARY_SPEC", "MAX_VALUE_COLS", "parse_csv", "preview_csv", "validate_csv",
    # units
    "clean_unit", "unit_as_typed", "summarise_units",
    # definitions
    "CATALOG_FIELDS", "MAX_LABEL", "MAX_DESCRIPTION", "MAX_RASTER_YEARS",
    "RESERVED_RASTER_COLS", "STATIC_COVERAGE_YEARS", "catalog_entry", "check_label",
    "check_band_columns", "check_definition", "default_band_columns", "define_polygon", "define_raster",
    "feature_rows", "preview_raster", "is_raster", "raster_bands",
    # raster
    "CONUS_BBOX", "MAX_BANDS_LISTED", "FULL_READ_MAX_CELLS", "inspect_raster",
    "grid_hash", "same_grid", "extract_values", "extract_bands",
    # steps
    "BOUNDARY_C3", "C4_TEMPLATE", "C3_GRID_TEMPLATE", "GRID_TAG_PREFIX", "TEMPLATES",
    "Step", "Context", "RenderContext", "definitions_from_config", "definition_for",
    "grid_tag", "c3_step_for", "c4_step_for", "plan", "parquet_map",
    "first_definition_for_grid", "output_path", "values_path", "render",
    "prepare_step", "boundary_tag_for_step", "legacy_cache_key", "weights_path",
    "kind_c4_step", "is_dated", "is_features", "needs_c3",
    # values
    "MaterialisedValues", "materialise_values", "value_table",
    # diagnostics
    "diagnostics", "coverage_diagnosis", "raster_diagnosis",
    # boundaries
    "BOUNDARY_DATA_DIR", "provisioned_boundaries", "unprovisioned",
]
