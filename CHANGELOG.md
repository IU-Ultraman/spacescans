# Changelog

The Python package (`spacescans-pipeline`) and the R package in `r/` share version numbers and
release tags. R-specific changes are listed in more detail in `r/NEWS.md`. Versions before 0.3.0
have no changelog; see the git tags `v0.2.0` to `v0.2.7`.

## 0.3.0 (unreleased)

The first version meant for PyPI and the first release of the R package. It moves the
orchestration that the SPACESCANS web application used to do into the package, so the web
application, the Python API, the command line and the R package run the same code.

### Changed

- **Participant-level output is the default.** `orchestrate.link()` returns one row per
  participant: `pid`, then each requested variable averaged over all of the participant's
  location episodes, weighted by the days that contribute a value. `output_level="episode"`
  returns one row per location episode, as before. Linkage rates are computed at the level of
  the output (share of participants, or share of episodes). The R package (`ss_link()`), the
  bridge protocol and the web application use the same default and option. YAML configs keep
  `time.output_grouping` (`patient` or `episode`); the CLI templates now state it.
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
  point, line or polygon features (time-invariant or yearly), linked as the distance in metres
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

### R package

- First release: `ss_install()` builds a private Python environment with uv; `ss_link()`,
  background jobs, catalog, data, coverage, custom-data and cache functions run the Python
  package through the bridge in a separate process. See `r/README.md` and `r/NEWS.md`.

### License

- Released under the MIT License (`LICENSE`; the R package in `r/` under the same license, with
  the CRAN-form `r/LICENSE`).
