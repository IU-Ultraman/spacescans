---
name: spacescans-custom-datasets
description: Prepare a user's own exposure dataset for SPACESCANS and register it with spacescans.custom.register() in Python, ss_register_dataset() in R, or the web application's Add dataset form. Covers the three shapes SPACESCANS links, a table keyed by 2010 Census tracts, block groups, ZCTAs or counties, GeoTIFF rasters on one grid, and point, line or polygon features, with their metadata, the validation messages and the checks to run before and after a first linkage. Use when someone wants to link their own, proprietary or restricted exposure data with SPACESCANS, asks what format SPACESCANS expects, or gets a registration error or a low linkage rate for a custom dataset. Not for cohort files (the five cohort columns are in the README) and not for changing the built-in catalog.
license: MIT
metadata:
  project: spacescans
  reference: https://github.com/IU-Ultraman/spacescans/blob/main/docs/custom-datasets.md
---

# Preparing a custom exposure dataset for SPACESCANS

SPACESCANS links a cohort of location episodes (participant, start date, end date,
longitude, latitude) to exposure data. A dataset you bring is linked by the same engine as
the built-in catalog once it is **registered**: `register()` validates the files, converts them
once into prepared files (Parquet for tables, checked copies for rasters, GeoParquet in
EPSG:5070 for features), stores them with a metadata record, and every later `link()` uses the
dataset by name. The original files are not changed.

Your job in this skill is to get the user's data into one of the three accepted shapes, write
honest metadata, register it, read the validation messages, and check the first linkage. Work on
copies; never rename, move or delete the user's originals.

## 1. Decide the shape

One dataset has one spatial representation and one time structure. Pick the row that matches
the data; split the data into several datasets if it mixes shapes.

| `kind` | files | `temporal` | what gets linked |
|---|---|---|---|
| `polygon_table` | one CSV (or Parquet) keyed by 2010 Census tract, block group, ZCTA or county codes | `static`; `yearly` (a year column); `daily` (a date column) | buffer-weighted mean of the values of the geographies under the location's buffer |
| `raster` | GeoTIFFs on one grid: one file, or one per year, two-week period or day | `static`, `yearly`, `biweekly`, `daily` | buffer-weighted mean of the pixels under the buffer |
| `features` | points, lines or polygons (one family) in a GeoPackage, GeoJSON, zipped Shapefile or GeoParquet file | `static`; `yearly` (one file per year, or a year column) | distance in meters from the location to the nearest feature (0 inside a polygon) |

Tables need only the base install (`pip install spacescans`); rasters and features need the
`geo` extra (`pip install "spacescans[geo]"`). The R package reaches the same code through
`spacescans::ss_install()`.

## 2. Inspect before converting

Read the data documentation first. Establish, and write down in a short processing note:

- the geography of each row (tract, block group, ZCTA, county) and its **vintage**: SPACESCANS
  joins tables to **2010** boundaries only. A 2020-vintage tract or block-group file must be
  crosswalked to 2010 codes by the user's method before registration; do not silently use it.
- the key column and whether leading zeros survived (a 5-digit county code read as a number loses
  its zero: `1001` is Autauga County, AL, `01001`);
- the time columns (one row per year or per date? or one column per year?), and for rasters which
  period each file is;
- the variables to link, their **units** and what a value means (a rate, a count, a percent or a
  fraction, a daily mean); the missing-value codes;
- for rasters: CRS, resolution, extent, nodata, and whether every file shares one grid;
- for features: CRS, geometry type, and which features belong in the dataset (attribute columns
  are not stored, so a subset such as "clinics of one type" must be filtered before registering).

Ask the user for anything the files and their documentation do not settle, in particular units,
vintage and the license. Never invent units or a vintage.

## 3. Convert to what `register()` accepts

### Tables

- **Long format**: one row per geography, or per geography and year (`yearly`) or date
  (`daily`). A wide file with one column per year is reshaped with `pandas.melt`.
- **Key column as text, zero-padded** to the code width: tract 11 digits, block group 12,
  county 5, ZCTA 5. No blank keys.
- **Year** as a whole number in 1900–2100 (column `year`, or name it in `key`); **dates** as
  `YYYY-MM-DD`, `YYYY/MM/DD`, `YYYYMMDD` or `MM/DD/YYYY` (column `date`, or name it in `key`).
- **Values numeric.** `NA`, `N/A`, `NULL`, `NaN`, `.`, blanks and infinities are read as
  missing; anything else non-numeric in more than half of a column fails the column. At most 40
  value columns. Variable names start with a letter and use letters, digits and underscores.
- Every other column is ignored; drop columns that are neither key, time nor values to keep the
  file small. UTF-8 (with or without BOM) or Latin-1.

```python
import pandas as pd
wide = pd.read_csv("heat_wide.csv", dtype={"fips": str})         # read codes as text
long = wide.melt(id_vars="fips", var_name="year", value_name="heat_days")
long["year"] = long["year"].str.extract(r"(\d{4})").astype(int)  # "hd_2013" -> 2013
long["fips"] = long["fips"].str.zfill(5)                          # 1001 -> 01001
long.to_csv("heat_long.csv", index=False)
```

### Rasters

- GeoTIFF, with a coordinate reference system, north-up and not rotated, touching the
  continental US.
- **All files on one grid** (same CRS, transform and size): the spatial weights are computed
  once and serve every period. Warp the others onto the first file's grid (`gdalwarp -t_srs
  <crs> -te <bounds> -tr <xres> <yres>` or `rasterio.warp.reproject`) and check with rasterio
  that `crs`, `transform` and `shape` are equal before registering.
- Nodata is each file's own tag; set it if the file marks missing pixels with a sentinel value.
- The **period** of each file comes from its name (`ndvi_2016.tif`; `t_20160101.tif`,
  `t_2016-01-01.tif`, `A2016001`; biweekly `pm_20160101_20160115.tif`), from a manifest CSV
  passed with the rasters (columns `file` and `year`, `date`, or `start` and optionally
  `end`), or from a `{path: period}` mapping in Python. Biweekly periods may not overlap; a
  period without an end runs to the day before the next start, at most 16 days.
- The linked band must exist in every file (band 1 by default; name others in `variables`).
  At most 10,000 files per dataset. A daily raster at fine resolution over a large cohort
  makes large intermediate tables; say so to the user.

### Features

- GeoPackage, GeoJSON (WGS84 by definition), zipped Shapefile or GeoParquet, with a CRS.
- One geometry family per dataset (points, lines or polygons, multi-part forms included).
  Empty geometries are dropped and invalid polygons repaired; at least one feature must lie
  over the continental US.
- Only geometries are kept. Filter to the features that define the exposure (for example one
  facility type) before registering; register several datasets for several types.
- `yearly`: one file per year (year in the file name or a `{path: year}` mapping) or one file
  with a year column named in `key={"year_column": ...}`. Days in years without a file get no
  value.

## 4. Register

```python
from spacescans import custom

custom.register("heat_long.csv", name="county_heat", kind="polygon_table", temporal="yearly",
                key={"boundary": "county", "column": "fips", "year_column": "year"},
                variables=[{"name": "heat_days", "unit": "days",
                            "description": "Days above the local 95th-percentile maximum temperature"}],
                metadata={"source": "<provider or lab>", "version": "<release or date>",
                          "description": "<one or two sentences>",
                          "license_note": "<terms the provider attaches>"})

custom.register(["ndvi_2015.tif", "ndvi_2016.tif"], name="ndvi", kind="raster", temporal="yearly",
                variables=[{"name": "ndvi", "unit": "index", "description": "Annual mean NDVI"}])

custom.register("clinics.geojson", name="clinics", kind="features", temporal="static")
```

- `key` for a table: `boundary` is `tract`, `block_group`, `zcta` or `county`; `column` is the
  key column; `year_column` / `date_column` default to `year` / `date`. A bare boundary name
  (`key="tract"`) works when the key column has a usual name such as `GEOID10` or `GEOID`.
- `variables`: a list of names, or of `{"name", "unit", "description"}`; a table entry may add
  `"column"` when the file's column has another name, a raster entry `"band"`. Defaults: every
  numeric column of a table; band 1 of a raster named after the dataset; `<name>_dist_m` for
  features. Names become result columns.
- `metadata`: `source`, `version`, `description`, `license_note`, `visibility` (`private`, the
  default; `deployment` is enforced only by the web application) and `owner`. `name` is at most
  80 characters and unique in the registry; `overwrite=True` replaces a dataset of the same name.
- The registry lives in `root=`, else `$SPACESCANS_DATASETS_DIR`, else the per-user data
  directory. The record (`dataset.json`) keeps the checksum of the originals, counts, spans and
  extents, never a data value.

In R the same call is `ss_register_dataset(paths, name, kind, temporal, key = list(boundary =
"county", column = "fips", year_column = "year"), variables = data.frame(name = "heat_days",
unit = "days", description = "..."), source = "...", version = "...", license_note = "...")`;
`ss_datasets()`, `ss_dataset()`, `ss_update_dataset()` and `ss_remove_dataset()` manage the
registry, and `ss_link(cohort, custom = "county_heat")` links by name. In the web application
the Datasets page's **Add dataset** form takes the same files and metadata, and registered
datasets appear under **Your datasets** in the Select Exposures step.

## 5. Read the messages, then fix the file

Registration either stores a complete dataset or stores nothing and raises `DatasetError`
with a message that names files, columns and counts (never values). Fix the input it points at;
do not work around the check.

| message (abridged) | cause | fix |
|---|---|---|
| `1 of 5 values in 'fips' are not 5-digit County codes. 1 of them is one digit short, which usually means a spreadsheet dropped a leading zero` | codes read as numbers | read the column as text and zero-pad |
| `15 of 15 values in 'fips' are not 11-digit Tract codes` | wrong `boundary`, or the wrong column | match the boundary to the code width |
| `1 duplicate rows: each geography and year must appear once` | a reshape or merge doubled rows, or two vintages are mixed | deduplicate or aggregate before registering |
| `value column(s) mostly non-numeric` | wrong delimiter, decimal comma, units inside cells | clean the column; keep units in `variables` |
| `ndvi_2017.tif is on a different grid from ndvi_2015.tif ...; every file must share one grid` | files with different resolution, extent or CRS | warp onto one grid |
| `a dataset named '...' already exists ...` | name taken | choose another name or pass `overwrite=True` |
| `MissingExtraError` | rasters or features without the `geo` extra | `pip install "spacescans[geo]"` |

## 6. Check before and after the first linkage

Before linking, compare the cohort's dates with the dataset's periods:

```python
from spacescans import catalog, custom
overlay = custom.resolve_custom(["county_heat", "ndvi", "clinics"])
report = catalog.coverage(cohort, list(overlay), overlay=overlay)   # cohort: DataFrame or CSV path
for key, v in report["variables"].items():
    print(key, v["coverage_pct"], v["coverage_years"], v["warnings"])
```

Then link a small cohort (a few episodes with known locations) and read three things:

```python
from spacescans.orchestrate import link
result = link(cohort, [], custom=["county_heat", "ndvi", "clinics"])  # catalog variables may be added
print(result.exposures)          # one row per participant by default
print(result.coverage)           # share of participants with a value, per dataset
print([w for w in result.warnings if w["code"] == "custom_linkage"])  # why a rate is low
```

The engine writes one rate line per linked column; when a column has a value for fewer than
half of the participants (or episodes, at episode level), that line and a diagnosis are added
to `result.warnings` with code `custom_linkage`. To see every rate line, including the healthy
ones, pass `on_event=print` to `link()`. A low rate for a table means the file does not cover
the geographies the cohort's buffers touch (the diagnosis lists the states on both sides, so a
one-state file run against a national cohort is recognised at once); for a raster, nodata or an
extent that misses the cohort (the diagnosis prints the raster's extent); for features, years
without a file.

Finally spot-check two or three values by hand against the formula the engine uses: for an
episode `e` and variable `v`, `sum_t d_et * z_etv / sum_t d_et`, where `z_etv` is the spatial
estimate for period `t` and `d_et` the days of the episode in that period (start and end
included); periods with no value contribute nothing; participant-level output pools the same
sums over the participant's episodes. For a yearly county table a participant with one episode
from 2014-03-15 to 2018-09-30 in a county with values for 2014 and 2015 only gets
`(292 * z_2014 + 365 * z_2015) / 657`. `examples/link_example.py` shows such independent checks
for a county table and point features.

## 7. The engine is deterministic: evaluate the prepared files, not the linkage

The linkage engine has no model, no sampling and no learned component. The same cohort, the same
registered dataset and the same settings (buffer radius, rasterization resolution) give the same
exposure table. Measured on the package's own runs: the spatial step (buffer weights and
distances) is byte-identical from run to run and with any number of worker processes; the
aggregation step runs in DuckDB, whose multi-threaded sums can differ in the last binary digits
between runs, at most about 1e-12 absolute and 1e-13 relative on a 100,000-episode cohort
(`docs/workers.md`, `tests/golden/README.md`); the two registered-dataset runs of the golden
suite came back identical.

So a difference in linked exposures between two preparations of the same source data is a
property of the prepared files. To validate a preparation against a reference, compare the
prepared files: the same keys and periods, values equal within the tolerance the source
justifies, the same units. `custom.get_dataset(name).details` reports row and key counts, time
span and extent, and the prepared table is readable with pandas or DuckDB from
`<root>/<id>/values.parquet`. Linking both versions and comparing exposures adds nothing the
file comparison does not already show.

## 8. Privacy and licensing

- Register exposure files only. Cohort rows never belong in the registry, and the registry's
  records and messages never contain data values.
- If the skill runs in an agent backed by a remote model, reason from schemas, counts and the
  validation messages; do not paste rows of a restricted dataset into the conversation.
- Record the provider's terms in `license_note`, keep `visibility` private unless the data may be
  shared with every user of a web deployment, and do not redistribute data whose license forbids
  it (the catalog's own TEMIS source is such a case).

## 9. Report back

Tell the user: the dataset name, kind, temporal structure and variables with units; the record id
(`custom.get_dataset(name).id`); the coverage before linking and the linkage rate after; the
spot checks with their numbers; where the conversion script and the processing note are; and
anything you had to assume. Keep the script: it is the reproducible record of the preparation.

## References

- `docs/custom-datasets.md`: the authoritative description of kinds, periods, checks and storage.
- README, "Register your own datasets" and "Getting the exposure data".
- `examples/link_example.py`, `examples/link_example.R`: registration of a county table and point
  features, linkage by name and independent checks of every result.
- `docs/workers.md`, `tests/golden/README.md`: the run-to-run comparisons behind section 7.
