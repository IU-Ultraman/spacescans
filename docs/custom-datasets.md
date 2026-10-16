# Registered datasets: your own data, prepared once, linked by name

A **registered dataset** is data you bring to a linkage: a table keyed by a 2010 Census
geography, a GeoTIFF or a series of GeoTIFFs, or a set of point, line or polygon features.
`spacescans.custom.register()` checks it and prepares it **once**, then stores the prepared
files with a metadata record under a *datasets root*. Every later link (Python, R through the
bridge, the web app) uses it by name or id and reads only the prepared files.

```python
from spacescans import custom, orchestrate

custom.register(["pm25_daily.csv"], name="PM2.5 daily", kind="polygon_table",
                temporal="daily", key={"boundary": "tract", "column": "GEOID"},
                variables=[{"name": "pm25", "unit": "ug/m3", "description": "Daily PM2.5"}],
                metadata={"source": "My lab's surface v2", "version": "2.0"})

result = orchestrate.link(cohort, ["ndi"], custom=["PM2.5 daily"])
```

Datasets listed in `custom=` join the selection after `variables`; `variables` may be empty
when you link only your own data. The definition mapping accepted before
(`custom={key: definition}` with the key also in `variables`) still works.

## Kinds and time structure

| `kind` | files | `temporal` | linked value |
|---|---|---|---|
| `polygon_table` | one CSV (or Parquet) with a key column of 2010 tract, block group, ZCTA or county codes | `static`; `yearly` (a year column); `daily` (a date column) | buffer-weighted mean of the values of the geographies under the location's buffer |
| `raster` | GeoTIFFs on one grid: one file, or one per year, two-week period or day | `static`, `yearly`, `biweekly`, `daily` | buffer-weighted mean of the pixels under the buffer |
| `features` | points, lines or polygons (one family) in a GeoPackage, GeoJSON, zipped Shapefile or GeoParquet file | `static`; `yearly` (one file per year, or a year column) | distance in meters from the location to the nearest feature |

Over a location episode the value is averaged over the days the episode spends in each period
of the data, counting only the periods with a defined value:

    episode e, variable v:  sum_t d_et * z_etv / sum_t d_et

where `z_etv` is the spatial estimate for period `t` (a year, a two-week period or a day; a
static dataset has one period spanning the episode), `d_et` the days of the episode in that
period (start and end included). Days outside the data's periods, and periods whose estimate is
missing (no geography with a value, only nodata pixels), contribute nothing; an episode with no
contributing day has no value. A participant-level result pools the same sums over all of the
participant's episodes (`output_level="participant"`, the default of `orchestrate.link`;
`output_level="episode"` gives one row per location episode).

Within a period, geographies or pixels with a missing value are left out of both the numerator
and the denominator of the buffer-weighted mean.

## Registering

```python
custom.register(paths, *, name, kind, temporal, key=None, variables=None, metadata=None,
                root=None, overwrite=False, dataset_id=None) -> DatasetRecord
```

* `paths`: the file(s). For rasters and yearly features also a `{path: period}` mapping: a year
  (yearly), a start date (`"2016-01-01"` or a `date`; daily and biweekly) or a
  `(start, end)` pair (biweekly).
* `name`: up to 80 characters, unique in the root (compared ignoring case).
* `key`:
  * `polygon_table`: `{"boundary": "tract" | "block_group" | "zcta" | "county", "column": <key
    column>}`, plus `"year_column"` (yearly) or `"date_column"` (daily), which default to a
    column called `year` / `date`. A bare boundary name (`key="tract"`) works when the key
    column has a usual name (`GEOID10`, `GEOID`, `ZCTA5CE10`, ...).
  * `features`: optional `{"year_column": ..., "layer": ...}` (the layer of a multi-layer file).
  * `raster`: not used.
* `variables`: the linked columns, a list of names or of `{"name", "unit", "description"}`;
  a table entry may add `"column"` (the file's column, when the name differs), a raster entry
  `"band"`. Names become result columns: a letter first, then letters, digits and underscores.
  Defaults: every numeric column of a table; band 1 of a raster, named after the dataset; for
  features one column `<name>_dist_m` (unit `m`).
* `metadata`: `source`, `version`, `description`, `license_note`, `visibility` (`"private"`,
  the default, or `"deployment"`) and `owner`. `visibility` and `owner` are recorded for the
  web app, which enforces them; the package does not.
* `overwrite=True` replaces a dataset of the same name; the new one gets a new id.
* `dataset_id` (8 lowercase hex characters) keeps the variable key `custom_<dataset_id>` of a
  dataset defined earlier with `define_polygon` / `define_raster` (the web app's uploads): the
  record's id starts with it. For moving such datasets into the registry.

The other functions:

```python
custom.list_datasets(root=None) -> list[DatasetRecord]          # oldest first
custom.get_dataset(name_or_id, root=None) -> DatasetRecord      # name (any case), id or variable key
custom.update_dataset(name_or_id, *, name=None, metadata=None, variables=None, root=None)
custom.remove_dataset(name_or_id, root=None) -> DatasetRecord   # the removed record
custom.dataset_definition(name_or_id, root=None) -> dict        # what link() and a web task store
custom.resolve_custom(items, root=None) -> {variable_key: definition}
custom.datasets_root(root=None) -> Path
```

`update_dataset` changes the name, the metadata (including `visibility`) and the units and
descriptions of the variables; the data, the id and the variable names stay.

### Root

The `root` argument, else `$SPACESCANS_DATASETS_DIR`, else
`platformdirs.user_data_dir("spacescans")/datasets` (on macOS
`~/Library/Application Support/spacescans/datasets`, on Linux
`$XDG_DATA_HOME/spacescans/datasets` or `~/.local/share/spacescans/datasets`); the same
directory is computed when `platformdirs` is not installed. `orchestrate.link` resolves names in
the default root; to link from another root pass the records
(`custom=[custom.get_dataset("x", root=r)]`) or set the environment variable.

Each dataset lives in `<root>/<id>/`:

| kind | prepared files |
|---|---|
| polygon_table | `values.parquet`: `geo_key` (text), `year` or `date`, one double column per variable, sorted |
| raster | `rasters/<nnnnn>_<name>.tif` (copies, in period order) and `periods.json` |
| features | `features.parquet` or `features_<year>.parquet`: the geometries only, GeoParquet in EPSG:5070 |

plus `dataset.json`, the record. A dataset is prepared in a hidden directory and renamed into
place under a lock, so a failed registration leaves nothing behind and a listed dataset is
always complete.

### The record

`DatasetRecord` (`dataset.json`) fields: `id` (uuid4 hex), `name`, `kind`, `temporal`, `key`,
`variables` (`[{name, unit, description}]`), `source`, `version`, `description`,
`license_note`, `visibility`, `owner`, `created_utc`, `files` (relative paths of the prepared
files), `checksum` (sha256 of the original files' bytes in the order of `details.originals`;
for one file, that file's sha256), `spacescans_version`, and `details`: kind-specific facts
(grid, bands and periods of a raster; geometry family, feature counts and extent of features;
row and key counts and the time span of a table; each prepared file's sha256). A record never
holds values from the data.

In a link a dataset's columns appear under its variable key `custom_<first 8 hex of id>`
(coverage table, provenance, feature dictionary rows built from the variables' descriptions
and units).

## What is checked

Messages name files, columns, counts and expected formats; they never quote a value from your
files.

**Tables.** The key column holds zero-padded codes of the boundary's width (tract 11 digits,
block group 12, county 5, ZCTA 5; a code one digit short is reported as a lost leading zero);
no blank keys; one row per geography (and year or date); years are whole numbers 1900-2100;
dates are `YYYY-MM-DD`, `YYYY/MM/DD`, `YYYYMMDD` or `MM/DD/YYYY` in 1900-2100; a value column
may not be empty or mostly (over half) non-numeric. `NA`, `N/A`, `NULL`, `NaN`, `.`, blanks and
infinite values are stored as missing. UTF-8 (with or without a byte-order mark) and Latin-1
files are read. The file is streamed through DuckDB, so a national daily table of hundreds of
millions of rows is checked and converted without being loaded into memory.

**Rasters.** Every file has a coordinate reference system, is north-up and not rotated, and
touches the continental US; all files share one grid (CRS, transform, size), because the grid
weights are computed once and serve every period; every linked band exists in every file.
Nodata is each file's own. Periods:

* yearly: one year in the file name (`ndvi_2016.tif`), a manifest or the mapping;
* daily: one date in the file name (`t_20160101.tif`, `t_2016-01-01.tif`, `t_2016_01_01.tif`,
  or `A2016001`, year and day of year), a manifest or the mapping;
* biweekly: a start date as for daily data, or a start and an end in the name
  (`pm_20160101_20160115.tif`). Without an end, a period runs to the day before the next
  period's start, at most 16 days; the last one ends on the 15th or at the month's end when it
  starts on the 1st or 16th (semi-monthly products), else after 14 days. Periods may not overlap.

A manifest is one `.csv` passed with the rasters: a `file` column (the file's name) and
`year` (yearly), `date` (daily) or `start` and optionally `end` (biweekly). At most 10,000
files per dataset.

**Features.** A coordinate reference system is required (GeoJSON is WGS84 by definition); empty
geometries are dropped and invalid polygons repaired; one geometry family (points, lines or
polygons, including their multi-part forms); at least one feature over the continental US. Only
the geometries are kept (attribute columns are not stored).

## Distances to features

The value is the planar distance in EPSG:5070 (NAD83 / Conus Albers) meters from the location
(the point itself, no buffer) to the nearest feature of the period, 0 inside a polygon. The
search is exact (a nearest-neighbor query over every feature of the period, with no search
radius), so the value does not depend on how far away the nearest feature is or on the other
locations in the cohort. For yearly features each year with a file is a period; days in years
without a file contribute nothing. Within the continental US a Conus Albers distance differs
from the geodesic one by at most about 1.5 percent (median 0.35 percent over random segments of
0.1-50 km), the same projection the catalog's road and water distances use.

## How a link uses a registered dataset

| dataset | C3 step | C4 step |
|---|---|---|
| static or yearly table | the shipped boundary step (shared with the catalog sources on that boundary) | `static_areal` / `yearly_areal` over `values.parquet` |
| daily table | the boundary step | `gridded` over the table's rows for the cohort's geographies and dates (`output/values_<key>.parquet`, written before the step) |
| static or yearly raster | `grid_weights` on the first raster (one per grid) | `static_areal` / `yearly_areal` over the pixels under the cohort |
| daily or biweekly raster | `grid_weights` on the first raster | `gridded` over the pixels per period (periods overlapping the cohort's dates only) |
| features | none | `static_areal` / `yearly_areal` over the per-location distances computed before the step |

Daily rasters at a fine resolution and a large cohort make large intermediate tables (cells
under the cohort times days), as the daily catalog source TEMIS does.

## R and the bridge

The R package reaches the registry through these bridge commands (all take an optional absolute
`root`):

| command | arguments | value |
|---|---|---|
| `datasets-register` | `files` (absolute paths), `name`, `kind`, `temporal`, `periods`? (parallel to `files`), `key`?, `variables`?, `metadata`?, `overwrite`?, `dataset_id`? | `{dataset, key}` |
| `datasets-list` | | `{root, datasets}` |
| `datasets-get` | `name` (name, id or variable key) | `{dataset, key, definition}` |
| `datasets-update` | `name`, `new_name`?, `metadata`?, `variables`? | `{dataset, key}` |
| `datasets-remove` | `name` | `{removed}` |

A `dataset` is the record plus `path` and `variable_key`. To link a registered dataset through
the bridge, pass `variables + key` and `custom = {key: definition}` to `link`.
