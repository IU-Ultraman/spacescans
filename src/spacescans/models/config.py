# common_v2/models/config.py
"""Pydantic config models — loaded from YAML, validated at parse time."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


# --- Enums ---

class LinkagePattern(str, Enum):
    BOUNDARY_OVERLAP = "boundary_overlap"
    GRID_WEIGHTS = "grid_weights"
    YEARLY_AREAL = "yearly_areal"
    GRIDDED = "gridded"
    PROXIMITY = "proximity"
    STATIC_AREAL = "static_areal"
    PRECOMPUTED_AREAL = "precomputed_areal"
    PRECOMPUTED_STATIC = "precomputed_static"
    FAQSD_DAILY_AREAL = "faqsd_daily_areal"
    ACAG_MULTI = "acag_multi"
    CBP_FALLBACK = "cbp_fallback"
    FARA_TRACT = "fara_tract"
    TIGER_PROXIMITY = "tiger_proximity"
    NHD_PROXIMITY = "nhd_proximity"
    BOUNDARY_OVERLAP_FAST = "boundary_overlap_fast"
    YEARLY_AREAL_BG_VINTAGE = "yearly_areal_bg_vintage"


class GeometryType(str, Enum):
    POLYGON = "polygon"
    RASTER = "raster"
    LINE = "line"
    POINT = "point"      # point features (the proximity pattern's nearest distance)


# --- AEA CRS default ---

AEA_CRS_DEFAULT = (
    "+proj=aea +lat_1=29.5 +lat_2=45.5 +lat_0=37.5 +lon_0=-96 "
    "+x_0=0 +y_0=0 +ellps=WGS84 +units=m +no_defs +type=crs"
)


# --- Sub-Models ---

class SourceConfig(BaseModel):
    file: str | list[str]
    format: str | None = None
    key: str | None = None
    crs: str | None = None
    layer: str | None = None
    join_col: str | None = None
    category_col: str | None = None
    county_file: str | None = None  # path to county boundary shapefile (tiger_proximity)
    road_cache_dir: str | None = None  # disk cache for filtered TIGER roads (tiger_proximity)
    year_col: str | None = None  # proximity, yearly: the feature attribute holding the year


class BufferConfig(BaseModel):
    patient_file: str
    buffer_m: int = 270
    buffer_resolution: int = 100
    raster_res_m: float = 25.0
    aea_crs: str = AEA_CRS_DEFAULT
    geoid_col: str = "geoid"
    long_col: str = "long"
    lat_col: str = "lat"
    test_index_limit: int | None = None
    grid_id_offset: int = 0
    # Measure grid weights against a nodata-free surrogate of the template grid, so a cell
    # that is nodata in the template file still gets a weight and per-period nodata is dropped
    # downstream (grid_weights.extract_grid_weights). The custom-grid template sets it; catalog
    # grids leave it False to keep their 0.2.x weights (golden parity).
    grid_weights_nodata_free: bool = False
    patient_adapter: str | None = None  # e.g. "demo_conus" — see helpers.load_patients
    raster_cache_dir: str | None = None  # disk cache for per-target rasterized boundary masks


class ExposureConfig(BaseModel):
    file: str | list[str]
    key: str | None = None
    join_col: str | None = None  # optional: vintage-dispatch patterns use join_col_{2010,2020} instead
    value_cols: list[str]
    year_col: str | None = None
    date_col: str | None = None
    start_col: str | None = None
    end_col: str | None = None
    fill_na: dict[str, float] | None = None  # post-aggregation fillna per column
    zbp_file: str | None = None  # CBP fallback: linked ZBP output path
    label_file: str | None = None  # FARA: column selection CSV path
    # --- BG vintage dispatch (yearly_areal_bg_vintage pattern) ---
    vintage_col: str | None = None      # column with 2010/2020 vintage flag
    join_col_2010: str | None = None    # exposure column to join to source (2010 BG weights)
    join_col_2020: str | None = None    # exposure column to join to source_2020 (2020 BG weights)


class TimeConfig(BaseModel):
    """When a C4 step links, and what one output row is.

    ``output_grouping`` is the row: ``"patient"`` (the default; one row per
    participant, PATID) or ``"episode"`` (one row per location episode, PATID
    and geoid). A participant's value is the mean over all of their episodes
    and source periods weighted by the days that contribute a value
    (``sum(d * z) / sum(d)``, missing periods and units excluded), which is
    the episode means weighted by each episode's contributing days. The
    user-facing name is ``output_level`` (``"participant"`` | ``"episode"``),
    which ``catalog.recipes.render_config`` maps onto this key.

    ``contributing_days`` (yearly_areal only): also write, per value column,
    the days that contributed to it as ``<column>__days``. The CBP fallback
    reads them from its ZBP input to form participant means exactly.
    """

    years: list[int] | None = None
    start_date: str | None = None
    end_date: str | None = None
    temporal_resolution: str = "yearly"
    temporal_mode: str = "yearly"
    output_grouping: str = "patient"  # "patient" | "episode"
    contributing_days: bool = False


class EngineConfig(BaseModel):
    backend: str = "duckdb"
    missing_policy: str = "skip"
    weight_col: str = "weight"
    # DuckDB resource settings. None (the default) leaves DuckDB's own defaults,
    # which is the 0.2.7 behaviour. They cap DuckDB only: C3 patterns and the
    # pandas-based readers (FAQSD, TEMIS) do not run inside DuckDB.
    threads: int | None = Field(default=None, ge=1)
    memory_limit: str | None = None      # DuckDB syntax, e.g. "4GB"
    temp_directory: str | None = None    # where DuckDB spills; default is ./.tmp
    # Worker processes for the Python-side work DuckDB does not run: the tiles of
    # boundary_overlap_fast and ACAG's NetCDF files. None (the default) or 1 keeps it in
    # the step process; the output is the same whatever the number.
    workers: int | None = Field(default=None, ge=1)


class OutputConfig(BaseModel):
    path: str
    format: str = "parquet"
    label_path: str | None = None
    save_intermediate: bool = False


# --- Transform Specs (discriminated union) ---

class BaseTransformSpec(BaseModel):
    target: Literal["source", "exposure", "buffer"] = "exposure"


class FilterTransform(BaseTransformSpec):
    type: Literal["filter"]
    column: str
    values: list[str | int] | None = None
    prefixes: list[str] | None = None
    exclude: bool = False
    labels: dict[str, str] | None = None


class SpatialFixTransform(BaseTransformSpec):
    type: Literal["spatial_fix"]
    fix_type: Literal["assign_crs", "set_extent", "repair_georef", "repair_nodata"]
    crs: str | None = None
    extent: list[float] | None = None
    nodata_value: float | None = None


class DeriveTransform(BaseTransformSpec):
    type: Literal["derive"]
    formula: str | None = None
    registry_key: str | None = None
    output_col: str
    params: dict | None = None


class ConditionSpec(BaseModel):
    when: dict[str, Any]
    then: Any


class RecodeRule(BaseModel):
    source_cols: list[str]
    output_col: str
    conditions: list[ConditionSpec]
    default: Any = None


class RecodeTransform(BaseTransformSpec):
    type: Literal["recode"]
    rules: list[RecodeRule]


class DateParseTransform(BaseTransformSpec):
    type: Literal["date_parse"]
    pattern: str
    date_format: str | None = None
    source_field: Literal["filename", "column"] = "filename"
    output_col: str = "date"


TransformSpec = Annotated[
    FilterTransform | SpatialFixTransform | DeriveTransform | RecodeTransform | DateParseTransform,
    Field(discriminator="type"),
]


# --- Top-Level Config ---

class DatasetConfig(BaseModel):
    name: str
    linkage_pattern: LinkagePattern
    geometry_type: GeometryType

    source: SourceConfig
    source_2020: SourceConfig | None = None  # 2nd weight source for BG vintage dispatch (yearly_areal_bg_vintage)
    buffer: BufferConfig
    exposure: ExposureConfig | None = None
    time: TimeConfig | None = None
    transforms: list[TransformSpec] = []
    engine: EngineConfig = EngineConfig()
    output: OutputConfig

    plugin: str | None = None
    prep: str | None = None
    depends_on: list[str] = []
