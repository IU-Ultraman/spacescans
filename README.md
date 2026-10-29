# spacescans

**Link time-referenced geolocation data to the spatial and contextual exposome.**

spacescans links *location episodes* to environmental and contextual exposures. A location
episode is a participant identifier, a location (longitude and latitude) and the first and last
day spent there; a residential history is a series of them. For every episode spacescans
computes a buffer-weighted value of each exposure source (or the distance to the nearest
feature) and weights it by the days the episode overlaps each source period. By default the
result has **one row per participant**: each exposure averaged over all of the participant's
episodes, weighted by days. One row per location episode is available with
`output_level="episode"`.

Exposures come from a catalog of 11 public data products (each site downloads them itself; see
[Getting the exposure data](#getting-the-exposure-data)) or from datasets you register: tables
keyed by census geography, rasters, and point, line or polygon features. The same engine runs
behind the Python API, the command line, the R package in [`r/`](https://github.com/IU-Ultraman/spacescans/tree/main/r)
and the SPACESCANS web application.

[![Python 3.10–3.14](https://img.shields.io/badge/python-3.10--3.14-blue.svg)](https://www.python.org/)
[![Website](https://img.shields.io/badge/website-spacescans.com-1f6feb.svg)](https://www.spacescans.com)

## Install

```bash
pip install "spacescans[geo]"
```

The development version installs from GitHub:

```bash
pip install "spacescans[geo] @ git+https://github.com/IU-Ultraman/spacescans.git"
```

You install and import `spacescans`. (Before 0.3.0 the distribution was called
`spacescans-pipeline`; it was never on PyPI.) It needs Python 3.10 to 3.14 on Linux or macOS. The base install is light; the extras add what each kind of
source needs:

| Extra | Needed for | Adds |
| --- | --- | --- |
| (none) | the catalog, `spacescans data`, cohort checks, exposure-value aggregation from existing weights | pandas, DuckDB, pyarrow, pydantic |
| `geo` | spatial weighting, which every catalog source needs; registered rasters and features; the quickstart | geopandas, rasterio, shapely, exactextract |
| `rda` | sources distributed as R data files: EPA Walkability Index, Community Organization Density, FARA Food Access, Neighborhood Deprivation Index | pyreadr |
| `nc` | ACAG PM2.5 Composition | xarray, netCDF4 |
| `hdf4` | TEMIS UV Exposure | pyhdf (wheels for common platforms; otherwise it builds against the system HDF4 library) |
| `all` | everything above | |

`pip install "spacescans[geo,rda,nc]"` covers every catalog source except TEMIS UV
Exposure. An optional reader or pattern whose extra is missing raises `MissingExtraError`, which
names the `pip install` to run.

## Quickstart (no data needed)

```bash
spacescans quickstart --output-dir ./demo-out
```

The quickstart needs the `geo` extra. It runs one spatial-weighting step (C3) on synthetic
sample data that comes with the package: 270 m buffers around 10 synthetic locations,
intersected with three sample county polygons. It writes the spatial weights to
`./demo-out/quickstart_county_weights.parquet`. It does not link exposure values; the next
section does.

## Link location episodes (Python)

### Your cohort

One row per location episode; a participant who moved has one row per location.

| Column | Content |
| --- | --- |
| `pid` | participant identifier (a `PATID` column is accepted too) |
| `startDate`, `endDate` | first and last day at the location: `2017-08-19` (ISO, preferred) or `8/19/2017` |
| `longitude`, `latitude` | the location, in decimal degrees |

Every row needs a `pid` and a location: the cohort check refuses a file with a blank `pid` or a
missing, non-numeric or out-of-range coordinate, or with an `endDate` before its `startDate`,
before anything runs. A blank date is allowed, with a warning: that episode contributes no days.
Only these five columns are written to disk, into a private (0700) run directory; other columns
stay in your session. Cohort values (identifiers, dates, coordinates) never appear in messages,
events, warnings or provenance.

### Link

```python
import pandas as pd
from spacescans import data
from spacescans.orchestrate import link

# Once: download NPS Soundscape Noise (1.2 GB) from the project mirror.
data.fetch(["noise"], data_dir="~/ss-data")

cohort = pd.DataFrame({
    "pid":       ["P1", "P1", "P2", "P3"],
    "startDate": ["2015-01-01", "2017-06-01", "2016-03-15", "2014-07-01"],
    "endDate":   ["2017-05-31", "2019-12-31", "2019-12-31", "2018-06-30"],
    "longitude": [-86.158, -86.131, -85.680, -87.630],
    "latitude":  [39.768, 39.790, 40.193, 41.880],
})

res = link(cohort, ["noise"], data_dir="~/ss-data")
res.exposures    # one row per participant: pid, l50dba_exi, l50dba_imp, l50dba_nat
res.coverage     # linkage rate of each variable: the share of participants with a value
res.features     # feature dictionary: what each output column is, with its unit
res.provenance   # versions, data receipts, cache keys and settings; no cohort values

by_episode = link(cohort, ["noise"], data_dir="~/ss-data", output_level="episode")
```

`cohort` can also be the path of a CSV file. Other arguments: `buffer_m` (buffer radius in
meters, default 270), `raster_res_m` (default 25), `cache` (a directory that keeps spatial
weights between calls, so further sources on the same geography reuse them), `custom`
(registered datasets, below), `on_error` (`"stop"` or `"continue"` when a source fails),
`threads` and `memory_limit` (DuckDB limits per step) and `workers` (processes per step for the
spatial computation of every catalog source and for decoding ACAG's NetCDF and TEMIS's HDF4
files; default one, and the result is the same with any number). `spacescans.catalog.variables()` lists
the catalog keys; `spacescans.catalog.coverage(cohort, keys)` checks before a run which episodes
fall inside each source's spatial and temporal coverage.

### Participant level and episode level

With `output_level="participant"` (the default) the result has one row per participant id in
the cohort, in order of first appearance: `pid`, then the requested variables, with no dates,
coordinates or episode ids. For participant *i* and variable *v*:

```text
x̄(i,v) = Σ_e Σ_t d(e,t) · z(e,t,v)  /  Σ_e Σ_t d(e,t)
```

where *e* runs over the participant's episodes, *t* over the source periods (year, two-week
period, day; a single period for time-invariant sources) that overlap the episode,
z(e,t,v) is the spatial estimate for the episode's location in that period (the
buffer-weighted mean, or the distance to the nearest feature), and d(e,t) is the number of
days of the episode in that period, first and last day included. Only periods with a defined
estimate count, separately for each variable; a participant with no contributing day keeps
their row, with missing values. This is the same as averaging the episode values, each
weighted by its contributing days.

- **Overlapping location episodes.** When two episodes of one participant overlap in time, each
  contributes its own days: a calendar day that both cover counts once for each of the two
  locations, so the participant value is a mean over episode-days, not over calendar days. Trim
  the overlaps in your cohort if a day should count only once.
- **Derived variables** are derived from the participant's means: FARA Food Access's flags (such
  as `LILATracts_1And10`) are recoded from the participant's mean rates and urban share, so they
  stay 0 or 1, and each ACAG `_nbm` (non-biomass) column is the participant's mean of the species
  minus the participant's mean of its biomass part (`_bm`). At the episode level the same rules
  apply to each episode's means.

With `output_level="episode"` the result has one row per location episode, with `episode_id`
(the 0-based row number of the episode in your input) and the variables.

Linkage rates are computed at the level of the output: the share of participants with a value,
or the share of episodes in episode mode.

## Register your own datasets

`spacescans.custom` keeps a library of datasets you add. Registration validates the files and
prepares them once (normalized Parquet for tables, a checked grid and period index for rasters,
GeoParquet in EPSG:5070 for features), stores them with their metadata (`dataset.json`), and
every later linkage reuses the prepared files.

```python
from pathlib import Path
from spacescans import custom

custom.register(["clinics.gpkg"], name="clinics", kind="features", temporal="static")
custom.register(sorted(Path("pm_biweekly").glob("*.tif")), name="pm_biweekly",
                kind="raster", temporal="biweekly")

custom.list_datasets()           # DatasetRecord: id, name, kind, temporal, variables, metadata
res = link(cohort, ["noise"], data_dir="~/ss-data", custom=["clinics", "pm_biweekly"])
custom.remove_dataset("pm_biweekly")
```

| `kind` | Files | `temporal` | Linked as |
| --- | --- | --- | --- |
| `polygon_table` | CSV keyed by 2010 census tract, block group, ZIP Code Tabulation Area or county | `static`, `yearly` (a year column), `daily` (a date column) | area-weighted mean over the buffer |
| `raster` | GeoTIFF: one, or one per period on a common grid | `static`, `yearly`, `biweekly`, `daily` | buffer-weighted mean of the cells |
| `features` | points, lines or polygons (one geometry type per dataset) with a coordinate reference system, as GeoPackage, GeoJSON, zipped Shapefile or GeoParquet | `static`, `yearly` (one file per year, or a year column) | distance in meters from the location to the nearest feature (`<name>_dist_m` unless you name the variable) |

`register()` also takes `key` (a table's geography and key column, such as
`{"boundary": "tract", "column": "GEOID"}`; for features optionally a year column or a layer),
`variables` (name, unit and description of each value), `metadata` (source, version,
description, license note, visibility, owner) and `overwrite`. A record has
an `id`, a `name` unique in its library, the checksum of the original files and the spacescans
version that prepared it. The library lives in `root=`, else
`$SPACESCANS_DATASETS_DIR`, else the user data directory (`platformdirs.user_data_dir("spacescans")`
followed by `datasets`). Each record's `visibility` (`"private"` or `"deployment"`) is
informational in the package; the web application enforces it, sharing a `"deployment"` dataset
with every user of that deployment. `get_dataset(name_or_id)` returns one record. The reference
is [docs/custom-datasets.md](https://github.com/IU-Ultraman/spacescans/blob/main/docs/custom-datasets.md).

`custom` also accepts in-memory definitions made with `custom.define_polygon()` and
`custom.define_raster()`, as a mapping of variable key to definition.

An agent skill, [skills/spacescans-custom-datasets/SKILL.md](skills/spacescans-custom-datasets/SKILL.md),
walks an AI coding agent through preparing a dataset for registration: choosing the shape,
reshaping tables and aligning rasters, writing the metadata, reading the validation messages, and
checking coverage and a few values after the first linkage. Copy the folder into the agent's
skills directory (`~/.claude/skills/` for Claude Code, `~/.agents/skills/` for Codex, or the same
folders inside a project).

## Getting the exposure data

No exposure data comes with spacescans. `spacescans data` knows where each catalog variable's
data comes from and what a complete copy looks like under the data directory
(`spacescans/data/manifest.json`); it needs no extras.

```bash
spacescans data status ndi walkability --data-dir ~/ss-data   # present, missing, sizes, next step
spacescans data fetch noise tiger_proximity --data-dir ~/ss-data --dry-run
spacescans data fetch noise tiger_proximity --data-dir ~/ss-data   # resumable; re-run after a drop
spacescans data verify --data-dir ~/ss-data                   # sha256 of everything placed
spacescans data adopt --data-dir /existing/web/pipeline-data  # receipts for data placed earlier
```

- Files the project mirrors download from the project's OneDrive mirror (or `--mirror URL|DIR`,
  `$SPACESCANS_DATA_MIRROR`), resume after interruptions, are checked against their published
  sha256, and are recorded in `<data-dir>/.spacescans/receipts/`.
- EPA FAQSD Air Quality, TEMIS UV Exposure and ACAG PM2.5 Composition download from their
  providers once you accept the provider's terms in the call (`--accept-terms ID`; `status` and
  `fetch --dry-run` print each ID with its terms). VIIRS Nighttime Lights needs a manual
  download; `status` prints the instructions, license and attribution. Provider terms are never
  accepted on your behalf.
- The same functions are available from Python: `spacescans.data.status()`, `fetch()`,
  `verify()`, `adopt()`, `terms()`, `import_files()`.

The catalog (every source covers the conterminous United States; the time-varying sources cover
2013–2019, the Neighborhood Deprivation Index 2012–2022):

| Key | Source | Spatial data | Time step | Extras |
| --- | --- | --- | --- | --- |
| `walkability` | EPA Walkability Index | polygon (block group) | time-invariant | `geo`, `rda` |
| `cbp_zcta5` | Community Organization Density | polygon (ZCTA, county fallback) | yearly | `geo`, `rda` |
| `fara_tract` | FARA Food Access | polygon (census tract) | yearly | `geo`, `rda` |
| `ndi` | Neighborhood Deprivation Index | polygon (block group, 2010 and 2020) | yearly | `geo`, `rda` |
| `faqsd` | EPA FAQSD Air Quality | polygon (census tract) | daily | `geo` |
| `noise` | NPS Soundscape Noise | raster (270 m) | time-invariant | `geo` |
| `vnl` | VIIRS Nighttime Lights | raster (15″) | yearly | `geo` |
| `acag` | ACAG PM2.5 Composition | raster (0.01°) | biweekly | `geo`, `nc` |
| `temis` | TEMIS UV Exposure | raster (0.25°) | daily | `geo`, `hdf4` |
| `nhd_bluespace` | NHD Bluespace | line (distance to the nearest water feature) | time-invariant | `geo` |
| `tiger_proximity` | TIGER Road Distance | line (distance to the nearest road) | yearly | `geo` |

## Command line

```bash
export SPACESCANS_DATA_DIR=/path/to/exposome-data
export SPACESCANS_OUTPUT_DIR=/path/to/results

# Copy the editable template configs, then point their paths at your data
spacescans init-config --out ./configs

# Run the pipelines in dependency order
spacescans run ./configs/c3/county.yaml
spacescans run ./configs/c3/zcta5.yaml
spacescans run ./configs/c4/zbp.yaml            # depends on C3 zcta5
spacescans run ./configs/c4/cbp_fallback.yaml   # depends on C3 county + C4 zbp
```

A C4 config's `time.output_grouping` is `patient` (one row per participant, the default) or
`episode` (one row per location episode); the templates state it. Paths resolve in three tiers,
**CLI flag > `$SPACESCANS_DATA_DIR` > YAML `base_dir:`**, with `${SPACESCANS_DATA_DIR}`
expansion inside YAML paths.

## How it works

Every linkage runs in two steps, both driven by YAML configs and the same DuckDB engine:

1. **Spatial weighting (C3, needs `geo`)**: the share of each location's buffer that falls in
   each polygon or raster cell, or the distance from the location to the nearest line or
   feature. The weights depend only on the cohort's locations, the source geography, the
   buffer radius and the rasterization resolution, so they are computed once per `link()` call
   (and across calls with a `cache` directory) and reused by every further source on the same
   geography.
2. **Exposure-value aggregation (C4, base install)**: the weights applied to each source
   period's values, weighted in turn by the days each episode overlaps the period, per episode
   or per participant.

### C3 patterns

| Pattern | What it does | Typical data |
| --- | --- | --- |
| `boundary_overlap_fast` | buffer × polygon area-overlap weights (per-tile bulk rasterization) | block group, tract, ZCTA, county |
| `boundary_overlap` | the same without per-tile rasterization (slower) | |
| `grid_weights` | buffer × raster-cell coverage weights | ACAG, noise, TEMIS, VIIRS |
| `tiger_proximity` | distance from each location to the nearest primary and secondary road, per year | TIGER roads |
| `nhd_proximity` | distance from each location to the nearest flowline, waterbody, area and coastline | NHD |

Distances to your own point, line or polygon features take one of two routes, which give the
same exact nearest-feature distance:

- **Registered features** (`custom.register(..., kind="features")`, then `link(custom=[...])`;
  also R's `ss_register_dataset()` and the web application's feature uploads): the distances
  from each location to the nearest feature are computed at the start of each link
  (`spacescans.custom.features`, in EPSG:5070 meters) and aggregated by the `static_areal` or
  `yearly_areal` C4 pattern. No C3 step.
- **Hand-written YAML** run with `spacescans run`: the `proximity` pattern does both steps in
  one. Its config names the features (`source.file`: one vector or GeoParquet file, or one per
  year of `time.years`, or one file with `source.year_col`), the output column
  (`exposure.value_cols`, default `distance_m`) and optionally `source.category_col` (one
  column per category); distances are in meters in `buffer.aea_crs` (an Albers equal-area
  projection of the conterminous US) and `time.output_grouping` applies as in every C4 config.

### C4 patterns

| Pattern | Time | What it does | Typical data |
| --- | --- | --- | --- |
| `static_areal` | time-invariant | area- or cell-weighted value, weighted by episode days | walkability, noise |
| `yearly_areal` | yearly | area-weighted yearly values, weighted by the days in each year | ZCTA business patterns |
| `yearly_areal_bg_vintage` | yearly | `yearly_areal` over 2010 and 2020 block groups, by year | NDI |
| `cbp_fallback` | yearly | ZCTA values, falling back to county values where there is no ZCTA value | Community Organization Density |
| `fara_tract` | yearly | `yearly_areal` with the FARA flag recodes (needs `rda`) | FARA |
| `faqsd_daily_areal` | daily | daily tract values with day-level overlap | EPA FAQSD |
| `gridded` | yearly, biweekly, daily | per-period values over the C3 weights of raster cells or polygons (needs `geo`) | VIIRS, TEMIS; registered daily tables and daily or biweekly rasters |
| `acag_multi` | biweekly | the ACAG species over the C3 cell weights (needs `geo`, `nc`) | ACAG |
| `precomputed_areal` | yearly | per-(location, year) values such as road distances, weighted by days | TIGER distances |
| `precomputed_static` | time-invariant | per-location values such as water distances, weighted by days | NHD distances |

## Limits

- Catalog sources cover the conterminous United States and, when they vary over time, at least
  2013–2019.
- Each pipeline step runs as a single process, using about one processor core. Support for
  several cores is planned.
- The daily sources need memory roughly in proportion to the number of episodes: EPA FAQSD Air
  Quality and TEMIS UV Exposure ran out of memory in a 24 GiB container at 100,000 episodes.
  Measured peaks per source are in
  [docs/memory.md](https://github.com/IU-Ultraman/spacescans/blob/main/docs/memory.md).

## R package

The R package in [`r/`](https://github.com/IU-Ultraman/spacescans/tree/main/r) (`ss_link()`,
`ss_fetch()`, `ss_register_dataset()` and friends) runs this package in a separate Python
process through a JSON-file bridge ([docs/bridge-protocol.md](https://github.com/IU-Ultraman/spacescans/blob/main/docs/bridge-protocol.md)).
It is released with the same version as the Python package. Install the release from
[R-universe](https://iu-ultraman.r-universe.dev/spacescans) with
`install.packages("spacescans", repos = c("https://iu-ultraman.r-universe.dev", "https://cloud.r-project.org"))`
(the development version: `remotes::install_github("IU-Ultraman/spacescans", subdir = "r")`),
then run `ss_install()` once; see its [README](https://github.com/IU-Ultraman/spacescans/blob/main/r/README.md).

## Project layout

```text
src/spacescans/
├── orchestrate/         link(): a cohort and variables in, exposures out; run dirs, cache, merge
├── catalog/             the catalog variables, feature dictionary, recipes and templates
├── custom/              registered datasets and uploaded values (tables, rasters, features)
├── data/                spacescans data: manifest, status, fetch, verify, adopt
├── cohort.py            cohort checks and date parsing
├── bridge/              python -m spacescans.bridge, the JSON-file protocol the R package uses
├── cli/                 run / quickstart / init-config / data
├── config_resolution.py 3-tier path resolution (CLI > env > YAML) + ${VAR} expansion
├── pipeline/            config loader, pattern registry and runner
├── models/              pydantic config schema
├── engine/              DuckDB aggregation engine
├── io/                  readers (Parquet, CSV, .Rda) and writers
├── geometry/            buffers, overlap, grid weights, proximity (geo)
├── transforms/          filter / recode / derive / date_parse / spatial_fix
├── linkage/             linkage-pattern implementations
├── plugins/             source-specific readers (extras-gated)
└── resources/           synthetic sample data and template configs
```

`import spacescans` loads only the base dependencies.

Outside the package: `examples/` holds a worked example in Python and in R (a synthetic cohort
linked to a catalog source and to two registered datasets, with independent checks), and
`benchmarks/` the synthetic 100,000-episode cohort, its generator, and the scripts and results
of the runtime and memory measurements per catalog source. Neither folder is part of the
installed package.

## Development

```bash
pip install -e '.[all,dev]'
pytest                  # full suite (base + any extras present in the environment)
pytest -m "not extras"  # base-only tests
pytest -m geo           # geo-extra tests
```

Opt-in suites (real data, golden references, web parity) are described in
[docs/tier2-notes.md](https://github.com/IU-Ultraman/spacescans/blob/main/docs/tier2-notes.md).
Changes are listed in [CHANGELOG.md](https://github.com/IU-Ultraman/spacescans/blob/main/CHANGELOG.md);
the release procedure is [docs/release.md](https://github.com/IU-Ultraman/spacescans/blob/main/docs/release.md).

## License

spacescans is released under the MIT License
([LICENSE](https://github.com/IU-Ultraman/spacescans/blob/main/LICENSE)); the R package in
`r/` is released under the same license. The license covers the software only: each catalog
source keeps its provider's terms, which `spacescans data status` prints.

---

Learn more at [www.spacescans.com](https://www.spacescans.com).
