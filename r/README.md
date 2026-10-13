# spacescans (R)

Link residential histories to the spatial and contextual exposome from R. `ss_link()` takes a
cohort of residential episodes, links each one to the exposure sources you choose, and returns
your data frame with one column per exposure variable. The work is done by the spacescans Python
package, which the R package runs in a separate process.

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
res <- ss_link(cohort, c("ndi", "walkability"))
attr(res, "coverage"); attr(res, "features"); attr(res, "provenance")
```

`ss_install()` needs [uv](https://docs.astral.sh/uv/) (for example `brew install uv`). It builds a
private Python environment and installs the spacescans Python package from the same GitHub commit
as the R package. `ss_catalog()` lists every exposure variable; the TEMIS ultraviolet source also
needs `ss_install(extras = "hdf4")`.

## Your cohort

`cohort` is your own data: one row per residential episode, with these five columns.

| Column | Content |
| --- | --- |
| `pid` | Patient identifier |
| `startDate`, `endDate` | First and last day at the address, as `2017-08-19` or `8/19/2017` |
| `longitude`, `latitude` | The geocoded residence, in decimal degrees |

```csv
pid,startDate,endDate,longitude,latitude
P001,2013-01-01,2015-06-30,-86.1581,39.7684
P001,2015-07-01,2018-12-31,-86.2760,39.8010
P002,2014-03-15,2019-12-31,-84.5120,39.1031
```

A patient who moved has one row per address. Keep the file anywhere on your computer, preferably
in a local, access-controlled project folder rather than a cloud-synced one, and either read it
into R yourself or give `ss_link()` its path:

```r
cohort <- read.csv("~/projects/my-study/cohort.csv")
res <- ss_link(cohort, c("ndi", "walkability"))

res <- ss_link("~/projects/my-study/cohort.csv", c("ndi", "walkability"))   # .csv or .parquet
```

If your columns have other names, map them with `cols`:

```r
res <- ss_link(cohort, "ndi", cols = c(pid = "patient_id", startDate = "start",
                                      endDate = "end", longitude = "lon", latitude = "lat"))
```

Your other columns (age, diagnoses and so on) stay in R and come back unchanged in `res`, in your
row order. During the run only the five columns above are written, to a private temporary folder
that is deleted afterwards.

## How it works

Every call runs `python -m spacescans.bridge` in a separate process and exchanges JSON request
and result files (tables as Parquet); see `docs/bridge-protocol.md` in this repository. There is
no reticulate dependency: a crash or Ctrl-C in Python never takes the R session down. Imports:
processx, ps, jsonlite, nanoparquet.

`ss_link()` writes only `pid`, `startDate`, `endDate`, `longitude` and `latitude` to a private
(0700) run directory under `tempdir()`, deleted after success unless `keep = TRUE`; cohort values
never appear in messages, events or provenance.

Functions: `ss_install()`, `ss_use_python()`, `ss_doctor()`, `ss_version()`;
`ss_catalog()`, `ss_features()`; `ss_data_dir()`, `ss_data_status()`,
`ss_fetch()`, `ss_data_verify()`, `ss_data_import()`, `ss_data_terms()`;
`ss_coverage()`, `ss_link()` and the job functions `ss_job_status()`,
`ss_job_wait()`, `ss_job_cancel()`, `ss_job_result()`, `ss_job_attach()`;
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
`ss_install()` installs `spacescans-pipeline` from the source archive of the
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
an Rscript that speaks protocol 1 from canned values), so they need no Python.
Tests of the real bridge run when `SPACESCANS_PYTHON` points at an environment
with spacescans installed (for example the repository's `.venv/bin/python`);
otherwise they skip. `tests/testthat/test-live.R` also needs
`SPACESCANS_LIVE=1`; each of its tests skips while the installed bridge does
not list the command it uses. `SPACESCANS_LIVE_DATA_DIR` (read only, checked
unchanged afterwards) and `SPACESCANS_LIVE_LINK=1` enable the data-status and
real-link tests. The `ss_install()` tests also need
`SPACESCANS_TEST_INSTALL=true` plus `SPACESCANS_TEST_WHEEL=<wheel>` (install from
a wheel) or `SPACESCANS_TEST_GITHUB_SHA=<pushed commit>` (install from GitHub).

`R CMD check` reports one WARNING, "Non-standard license specification",
until a licence is chosen.
