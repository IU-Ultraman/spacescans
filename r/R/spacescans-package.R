#' spacescans: link patient residential histories to environmental exposures
#'
#' The package is a client of the `spacescans` Python pipeline. Every call
#' starts `python -m spacescans.bridge` in a separate process, hands it a JSON
#' request and reads back a JSON result (tables travel as Parquet files), so a
#' crash or an interrupt in Python never takes down the R session.
#'
#' Getting started (see `vignette("getting-started", package = "spacescans")`):
#' * [ss_install()] builds a private Python environment with `uv`;
#'   [ss_use_python()] or the `SPACESCANS_PYTHON` environment variable point
#'   the package at an existing one instead. [ss_doctor()] checks the setup;
#'   [ss_version()] reports versions.
#' * [ss_catalog()] and [ss_features()] describe the catalog variables and
#'   the columns they produce.
#' * [ss_data_dir()] sets the data directory; [ss_data_status()] says what
#'   is missing; [ss_fetch()] downloads it (resumably); [ss_data_verify()],
#'   [ss_data_import()] and [ss_data_terms()] manage it.
#' * [ss_coverage()] estimates coverage before a run; [ss_link()] links a
#'   cohort to variables and returns it with the exposure columns; with
#'   `wait = FALSE` it returns a job ([ss_job_status()]).
#' * [ss_custom_polygon()] and [ss_custom_raster()] bring your own data.
#' * [ss_cache_info()] and [ss_cache_clear()] manage the C3 weight cache.
#' * [ss_run()] runs one pipeline config (a YAML file or an R list);
#'   [ss_quickstart()] runs the bundled demo; [ss_config_schema()],
#'   [ss_validate()] and [ss_templates()] help write configs;
#'   [ss_read_result()] reads pipeline output.
#'
#' Options:
#' * `spacescans.version_check`: `"strict"` (default) requires the Python
#'   package to have exactly the version this R package was released with;
#'   `"warn"` accepts the same minor version with a warning; `"off"` skips
#'   the check (development only).
#' * `spacescans.interrupt_grace`: seconds to wait for the bridge to stop
#'   after Ctrl-C before its process tree is killed (default 10).
#' * `spacescans.poll_ms`: how often progress events are read (default 200).
#' * `spacescans.data_dir`: the data directory (set by [ss_data_dir()]).
#' * `spacescans.cache_dir`: the C3 cache root (default
#'   `tools::R_user_dir("spacescans", "cache")`).
#' * `spacescans.envdir`: where [ss_install()] puts the Python environment.
#'
#' @keywords internal
"_PACKAGE"

.ss_state <- new.env(parent = emptyenv())

.ss_protocol <- 1L

.onLoad <- function(libname, pkgname) {
  defaults <- list(
    spacescans.version_check = "strict",
    spacescans.interrupt_grace = 10,
    spacescans.poll_ms = 200L
  )
  missing <- setdiff(names(defaults), names(options()))
  if (length(missing)) options(defaults[missing])
  invisible()
}

`%||%` <- function(x, y) if (is.null(x)) y else x

.ss_pkg_version <- function() {
  as.character(utils::packageVersion("spacescans"))
}

# The Python package version and protocol this R package was released with.
.ss_expected <- function() {
  desc <- utils::packageDescription("spacescans")
  list(
    version = desc[["Config/spacescans/version"]] %||% .ss_pkg_version(),
    protocol = as.integer(desc[["Config/spacescans/protocol"]] %||% .ss_protocol)
  )
}
