# Changelog

The Python package (`spacescans` on PyPI) and the R package in `r/` share version numbers and
release tags. R-specific changes are listed in more detail in `r/NEWS.md`. Versions before 0.3.0
have no changelog; see the git tags `v0.2.0` to `v0.2.7`.

## Unreleased

### Added

- **`workers`: worker processes for the parts of a step that DuckDB does not run.** Set it as
  `engine.workers` in a config, `link(workers=)`, `ss_link(workers =)`, `ss_run(workers =)` or
  the bridge's `workers`. It applies to the polygon-overlap weights (`boundary_overlap_fast`:
  block groups, tracts, ZCTAs) and to the NetCDF files of ACAG (`acag_multi`). The default is
  one process, which is the behaviour of 0.3.0. The output does not depend on the number of
  workers (each worker computes whole tiles or files, and the results are combined in input
  order), so cache keys do not include it. Workers are spawned, not forked, and belong to
  the step's process group, so cancelling a run stops them too. Each worker adds memory.
  `spacescans.capabilities()["flags"]["workers"]` tells clients the option exists.

### Changed

- `compute_overlap_weights_fast` computes each boundary's burn-order key once per call, not
  once per tile. The order is the same, and the step is a few percent faster.
- Out-of-memory hints mention `workers` alongside `threads` and `memory_limit`.

### Removed

- `compute_overlap_weights` (the slow polygon-overlap path) loses its `n_workers`,
  `chunk_size` and `resume_dir` parameters, which it never read.

## 0.3.0 (2026-10-23)

The first version meant for PyPI and the first release of the R package. It moves the
orchestration that the SPACESCANS web application used to do into the package, so the web
application, the Python API, the command line and the R package run the same code.

### Changed

- **The distribution is called `spacescans`**, like the module, the command and the
  repository. Until 0.3.0 it was `spacescans-pipeline`, which was only ever installed from
  GitHub; a `pip install "spacescans-pipeline @ git+..."` of a newer commit now fails pip's
  name check, so write `spacescans` there.
- **Participant-level output is the default.** `orchestrate.link()` returns one row per
  participant: `pid`, then each requested variable averaged over all of the participant's
  location episodes, weighted by the days that contribute a value. `output_level="episode"`
  returns one row per location episode, as before. Linkage rates are computed at the level of
  the output (share of participants, or share of episodes). The R package (`ss_link()`), the
  bridge protocol and the web application use the same default and option. YAML configs keep
  `time.output_grouping` (`patient` or `episode`); the CLI templates now state it. Overlapping
  location episodes each contribute their own days; FARA's flags and ACAG's `_nbm` columns are
  derived from the participant's means. A row with a blank `pid` belongs to no participant: the
  participant-level merge leaves it out (the web application's rule) and `link()` records the
  count in `provenance["cohort"]["rows_without_pid"]`; `cohort.validate` still refuses such rows
  before a link starts.
- **FARA Food Access** drops tracts with missing values from both the numerator and the
  denominator of the buffer-weighted mean, like every other source. Up to 0.2.7 such tracts
  stayed in the denominator, so values differ from 0.2.7 where a buffer overlaps a tract with
  missing FARA values.
- The error for an unrecognized date format no longer quotes a date from the cohort file.
- Package metadata: Python 3.13 is supported and tested; the platforms are Linux and macOS.

### Added

- `spacescans.orchestrate`: `link()` runs a whole linkage (a cohort and catalog or custom
  variables in; exposures, feature dictionary, linkage rates and provenance out), with private
  run directories that record their owner and are swept once the owner process is gone,
  spatial weights computed once per call and, with a cache directory, kept across calls,
  subprocess and in-process step
  executors, a run lock, run events and log lines, and the pre-run coverage estimate.
- Registered datasets in `spacescans.custom`: `register()`, `list_datasets()`, `get_dataset()`
  and `remove_dataset()` validate and prepare a dataset once and store it with its metadata;
  `link(custom=[...])` accepts their names or ids. Kinds: tables keyed by 2010 census tract,
  block group, ZCTA or county (time-invariant, yearly, or daily with a date column); rasters
  (time-invariant, yearly, biweekly or daily, one GeoTIFF per period on a common grid); and
  point, line or polygon features (time-invariant or yearly), linked as the distance in meters
  to the nearest feature.
- `spacescans.custom` upload checks for CSV tables and GeoTIFF rasters, dataset definitions,
  and the steps that link them, shared with the web application.
- `spacescans.catalog`: the 11 catalog variables, the feature dictionary, per-source recipes and
  templates.
- `spacescans.data` and the `spacescans data` command: the data manifest, `status`, `fetch`
  (resumable downloads from the project mirror, checked against published sha256; provider
  downloads once the user accepts the provider's terms), `verify`, `adopt`, `import`,
  `requirements`, `prepare` (the ACAG and TEMIS grid templates) and `mirror-check`.
- `spacescans.cohort`: cohort checks, date parsing and the pipeline input table.
- `spacescans.bridge` (`python -m spacescans.bridge`): a JSON-file protocol for clients in
  other processes, used by the R package; see `docs/bridge-protocol.md`.
- `python -m spacescans` and `spacescans.capabilities()`.
- The engine block of a config sets DuckDB threads, memory limit and spill directory.
- A source that runs out of memory reports how much memory it needed in measured runs
  (`docs/memory.md`).

### Fixed

- **Custom rasters** are weighted on the raster's grid, not on the valid cells of its first
  file. Before, a cell that was nodata in the first file of the first dataset on a grid was
  left out of every period, band and dataset on that grid, and the result could depend on the
  order of the datasets; now a period's nodata cells drop out of that period only. Weights
  cached by earlier versions are not reused.
- Uploaded rasters and feature files are refused when their content is a GDAL virtual format
  (VRT), whatever the file name, and GeoTIFFs open with the GTiff driver only, so an upload
  cannot make the server read other files or open network addresses.
- Feature datasets whose coordinates are not finite after reprojection are refused, and a link
  stops with an error when PROJ cannot transform a location (for example because a
  transformation grid could not be downloaded), instead of linking nothing.
- The linkage rate logged for a custom dataset divides by the cohort's participants or location
  episodes, not by the rows that reached the output.
- A biweekly series given by start dates only no longer stretches a period over a missing file:
  an inferred end is capped at the series' cadence, with a warning.
- The feature dictionary describes the NPS noise layers as existing conditions, natural
  conditions and their difference (the anthropogenic impact), and uses location wording.

### R package

- First release: `ss_install()` builds a private Python environment with uv; `ss_link()`,
  background jobs, catalog, data, coverage, custom-data and cache functions run the Python
  package through the bridge in a separate process. See `r/README.md` and `r/NEWS.md`.
- `ss_install()` also works after an install from r-universe, which records only `RemoteUrl`,
  `RemoteRef` and `RemoteSha`: it installs the Python package from that commit's source archive.

### License

- Released under the MIT License (`LICENSE`; the R package in `r/` under the same license, with
  the CRAN-form `r/LICENSE`).
