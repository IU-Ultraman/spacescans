# spacescans (R)

Link time-referenced geolocation data to the spatial and contextual exposome from R. `ss_link()`
takes a cohort of location episodes (a participant, a location and the days spent there), links
each one to the exposure sources you choose, and returns one row per participant with one column
per exposure variable, or, with `output_level = "episode"`, your data frame with the exposure
columns added. The work is done by the spacescans Python package, which the R package runs in a
separate process.

## Quick start

```r
# install.packages("remotes")
remotes::install_github("IU-Ultraman/spacescans", subdir = "r")
```

```r
library(spacescans)
ss_install()                                   # once: the matching Python environment
ss_data_dir("~/ss-data")
ss_data_status(c("ndi", "walkability"))        # what is missing, and the next call
ss_fetch(c("ndi", "walkability"))              # resumable downloads
res <- ss_link(cohort, c("ndi", "walkability"))                          # one row per participant
ep <- ss_link(cohort, c("ndi", "walkability"), output_level = "episode")  # one row per episode
attr(res, "coverage"); attr(res, "features"); attr(res, "provenance")
```

`ss_install()` needs [uv](https://docs.astral.sh/uv/) (for example `brew install uv`). It builds a
private Python environment and installs the spacescans Python package from the same GitHub commit
as the R package. `ss_catalog()` lists every exposure variable; the TEMIS ultraviolet source also
needs `ss_install(extras = "hdf4")`. No exposure data ships with the package: `ss_fetch()`
downloads each catalog source from its provider or from the project mirror, where the provider's
terms allow it.

A minimal example with three synthetic participants and the NPS noise model (the catalog source
whose files are all on the mirror; `ss_fetch("noise")` downloads about 1.2 GB) is installed with
the package and shown in the "Getting started" vignette:

```r
source(system.file("examples", "minimal-example.R", package = "spacescans"))
```

## Your cohort

`cohort` is your own data, time-referenced geolocation data: one row per location episode, with
these five columns.

| Column | Content |
| --- | --- |
| `pid` | Participant identifier |
| `startDate`, `endDate` | First and last day at the location, as `2017-08-19` or `8/19/2017` |
| `longitude`, `latitude` | The geocoded location, in decimal degrees |

```csv
pid,startDate,endDate,longitude,latitude
P001,2013-01-01,2015-06-30,-86.1581,39.7684
P001,2015-07-01,2018-12-31,-86.2760,39.8010
P002,2014-03-15,2019-12-31,-84.5120,39.1031
```

A participant who moved has one row per location. Keep the file anywhere on your computer,
preferably in a local, access-controlled project folder rather than a cloud-synced one, and either
read it into R yourself or give `ss_link()` its path:

```r
cohort <- read.csv("~/projects/my-study/cohort.csv")
res <- ss_link(cohort, c("ndi", "walkability"))

res <- ss_link("~/projects/my-study/cohort.csv", c("ndi", "walkability"))   # .csv or .parquet
```

If your columns have other names, map them with `cols`:

```r
res <- ss_link(cohort, "ndi", cols = c(pid = "participant_id", startDate = "start",
                                      endDate = "end", longitude = "lon", latitude = "lat"))
```

During the run only the five columns above are written, to a private temporary folder that is
deleted afterwards; your other columns (age, diagnoses and so on) stay in R.

## Output levels

| `output_level` | Rows | Columns | Linkage rates (`attr(res, "coverage")`) |
| --- | --- | --- | --- |
| `"participant"` (default) | one per participant, in order of first appearance | `pid` (your id's type) and the exposures | share of participants with a value |
| `"episode"` | one per location episode, in your order | your columns, unchanged, and the exposures | share of episodes with a value |

A participant's value is the mean of their episode values weighted by contributing days: the days
of each episode that fall in a source period (year, two weeks or day) with an estimate, first and
last day included; a time-invariant source counts every day of the episode. Days are counted per
variable, and a participant with no contributing day is kept with a missing value. `?ss_link`
gives the formula.

## Your own data

Register a dataset once and link it by name later, like a catalog variable:

```r
ss_register_dataset("~/data/svi_2020_tract.csv", name = "SVI 2020", kind = "polygon_table",
                    temporal = "static", key = list(boundary = "tract", column = "FIPS"),
                    source = "CDC/ATSDR SVI 2020")
ss_datasets()                                       # what is registered
res <- ss_link(cohort, "ndi", custom = "SVI 2020")
ss_update_dataset("SVI 2020", visibility = "deployment")
ss_remove_dataset("SVI 2020")
```

Registration checks and prepares the files once and stores them, with a record of their metadata,
in a registry directory (by default the Python package's, `~/Library/Application
Support/spacescans/datasets` on macOS; the option `spacescans.datasets_dir` or the environment
variable `SPACESCANS_DATASETS_DIR` moves it). Kinds: `"polygon_table"` (a CSV keyed by a 2010
tract, block group, ZCTA or county; static, yearly or daily), `"raster"` (GeoTIFFs on one grid;
static, yearly, biweekly or daily) and `"features"` (points, lines or polygons; the variable is the
distance in meters to the nearest feature; static or yearly). A dataset registered from Python is
visible from R and the other way round. `ss_custom_polygon()` and `ss_custom_raster()` still build
one-off definitions that point at your files where they are.

## How it works

Every call runs `python -m spacescans.bridge` in a separate process and exchanges JSON request
and result files (tables as Parquet); see `docs/bridge-protocol.md` in this repository. There is
no reticulate dependency: a crash or Ctrl-C in Python never takes the R session down. Imports:
processx, ps, jsonlite, nanoparquet.

`ss_link()` writes only `pid`, `startDate`, `endDate`, `longitude` and `latitude` to a private
(0700) run directory under `tools::R_user_dir("spacescans", "cache")/runs`, deleted after success
unless `keep = TRUE`; cohort values never appear in messages, events or provenance. Each run
directory records the R session that owns it; one left behind by a session that was killed is
deleted (never archived) the next time the package loads, or by `ss_doctor()` or
`ss_cache_clear()`.

Functions: `ss_install()`, `ss_use_python()`, `ss_python()`, `ss_doctor()`, `ss_version()`;
`ss_catalog()`, `ss_features()`; `ss_data_dir()`, `ss_data_status()`,
`ss_fetch()`, `ss_data_verify()`, `ss_data_import()`, `ss_data_terms()`;
`ss_coverage()`, `ss_link()` and the job functions `ss_job_status()`,
`ss_job_wait()`, `ss_job_cancel()`, `ss_job_result()`, `ss_job_attach()`;
`ss_register_dataset()`, `ss_datasets()`, `ss_dataset()`, `ss_update_dataset()`, `ss_remove_dataset()`;
`ss_custom_polygon()`, `ss_custom_raster()`; `ss_cache_info()`,
`ss_cache_clear()`; and the tier-1 interface `ss_run()`, `ss_quickstart()`,
`ss_templates()`, `ss_config_schema()`, `ss_validate()`, `ss_read_config()`,
`ss_write_config()`, `ss_read_result()`. See the "Getting started" vignette.

Supported platform: macOS 14 or later on Apple silicon, Python 3.12.

## Versions

The R package and the Python package carry the same version and are
released with the same tag. `DESCRIPTION` records it twice (`Version` and
`Config/spacescans/version`) together with `Config/spacescans/protocol`.
On the first call in a session the package runs the bridge's `capabilities`
command and refuses a Python package of another version
(`options(spacescans.version_check = "warn")` accepts the same minor
version). It warns when the running code's fingerprint differs from the
bundled wheel's (an editable or modified install).
`tests/test_r_package_version.py` keeps the versions in step in Python CI.

## The bundled wheel

`tools/build-wheel.sh` builds the wheel from `git archive HEAD` (committed
files only) with `SOURCE_DATE_EPOCH` set to the commit time, so a commit
always gives the same bytes, and writes `inst/python/pin.json` (version,
sha256, code fingerprint, commit). It refuses a dirty tree and mismatched
versions. The wheel and pin.json are not committed; the release job runs the
script at the release commit (with `--url` of the wheel attached to the
GitHub release) before `R CMD build r`. Until a release bundles it,
`ss_install()` installs the `spacescans` Python package from the source archive of the
GitHub commit that `remotes::install_github()` recorded (`RemoteSha`).

## The dependency lock

`tools/lock.sh` writes `inst/python/locks/macos-arm64-py312.txt` (every
dependency of every extra, with sha256 hashes, resolved for CPython 3.12 on
Apple silicon with a macOS 14 floor: rasterio 1.5.1, pyhdf 0.11.6) and a
`.groups.json` naming the packages of the base install and of each extra.
When both are present, `ss_install()` installs the base packages plus those
of the requested extras with `--require-hashes`, then the wheel with
`--no-deps`; without them it lets uv resolve `wheel[extras]`. Like the wheel,
the lock is produced by the release job, not committed.

## Development

```sh
r/tools/build-wheel.sh                          # bundle the wheel
r/tools/lock.sh                                 # optional: the hashed lock
R CMD build r && R CMD check --no-manual spacescans_*.tar.gz
```

Most tests run against a fake bridge (`tests/testthat/fixtures/fake_bridge.R`,
an Rscript that speaks protocol 1 from canned values and keeps a file-backed
dataset registry), so they need no Python.
Tests of the real bridge run when `SPACESCANS_PYTHON` points at an environment
with spacescans installed (for example the repository's `.venv/bin/python`);
otherwise they skip. `tests/testthat/test-live.R` also needs
`SPACESCANS_LIVE=1`; each of its tests skips while the installed bridge does
not list the command it uses (the registry tests need `datasets-register`).
`SPACESCANS_LIVE_LINK=1` adds real links: the registered-dataset example
(`inst/examples/registered-dataset.R`, no catalog data needed) and, with
`SPACESCANS_LIVE_DATA_DIR` (read only, checked unchanged afterwards; it must
hold the noise and walkability data), the minimal example
(`inst/examples/minimal-example.R`) and links at both output levels. The `ss_install()` tests also need
`SPACESCANS_TEST_INSTALL=true` plus `SPACESCANS_TEST_WHEEL=<wheel>` (install from
a wheel) or `SPACESCANS_TEST_GITHUB_SHA=<pushed commit>` (install from GitHub).

## License

MIT (`LICENSE`), as the Python package. The license covers the software only: each catalog
source keeps its provider's terms (`ss_data_terms()` shows a dataset's license, attribution and
terms).
