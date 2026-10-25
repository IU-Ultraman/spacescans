# spacescans (development version)

## New features

* `ss_link(workers =)` and `ss_run(workers =)`: worker processes per
  pipeline step for the polygon-overlap weights (block groups, tracts,
  ZCTAs) and ACAG's NetCDF files. The default `NULL` is one process, as in
  0.3.0. The result does not depend on the number of workers; each worker
  adds memory.

# spacescans 0.3.0

## Breaking change

* `ss_link()` returns one row per participant by default. The new argument
  `output_level = c("participant", "episode")` chooses the level; use
  `output_level = "episode"` for the behaviour of 0.2 (your data frame with
  the exposure columns added, one row per location episode).
* `output_level` is the third argument of `ss_link()`: a call that passed
  `data_dir` by position must name it (`data_dir = `).

## New features

* Participant level: `pid` (in the type of your id column) and one column
  per exposure, one row per participant in order of first appearance,
  including participants without any value. Each value is the mean of the
  participant's episode values weighted by contributing days, counted per
  variable (see `?ss_link`). The result carries `attr(, "output_level")`.
* Linkage rates (`attr(res, "coverage")`) and the warning for a variable
  with a value for fewer than 90% of the rows count participants at
  participant level and episodes at episode level. `ss_coverage()` still
  estimates per location episode.
* Registered datasets: `ss_register_dataset()` checks and prepares your own
  data once (polygon tables keyed by a Census geography, static, yearly or
  daily; GeoTIFF rasters, static, yearly, biweekly or daily; point, line or
  polygon features, linked as the distance to the nearest feature) and
  stores it with its metadata in a registry shared with the Python package.
  `ss_datasets()`, `ss_dataset()`, `ss_update_dataset()` and
  `ss_remove_dataset()` list, show, change and remove datasets; the option
  `spacescans.datasets_dir` (or the environment variable
  `SPACESCANS_DATASETS_DIR`) chooses the registry.
* `ss_link(custom = )` and `ss_coverage(custom = )` take registered dataset
  names, ids, variable keys or records, alone or together with
  `ss_custom_polygon()` and `ss_custom_raster()` definitions, in the order
  given; `variables` may then be empty.
* Jobs (`ss_link(wait = FALSE)`, `ss_job_attach()`) work at both levels;
  `job.json` records the level and counts, never participant ids. A job
  directory written by 0.2 is read as episode level.
* `ss_link()` refuses, with an `ss_install()` hint, a Python package that
  returns episodes when participants were asked for.
* `ss_install()` works after an install from r-universe: it reads the
  repository from `RemoteUrl` and installs the Python package from the
  recorded commit (`RemoteSha`), or from the tag of the package version when
  no commit is recorded.

## Package

* Released under the MIT License (`LICENSE`).
* Title: "Link Time-Referenced Geolocation Data to the Spatial and
  Contextual Exposome".

## Documentation

* The vignette and the README use the terms location episode, participant
  and time-referenced geolocation data, explain both output levels, and
  show two example scripts installed with the package:
  `inst/examples/minimal-example.R` (three synthetic participants and the
  NPS noise model, the catalog source whose files are all on the project
  mirror) and `inst/examples/registered-dataset.R` (a registered features
  dataset, no catalog data needed). The live tests run both.
* `?ss_link` and the vignette state two rules of the participant level:
  overlapping location episodes each contribute their own days, and
  variables derived after aggregation (the FARA flags, the ACAG `_nbm`
  columns) are derived from the participant's means.
