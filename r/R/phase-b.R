#' Functions that arrive in phase B
#'
#' These are the planned entry points for linking a cohort to catalog
#' variables and for managing the data directory. They exist so code and
#' documentation can refer to them; each currently raises an error of class
#' `spacescans_error_not_implemented`.
#'
#' * `ss_link()` links a cohort (one row per residential episode) to catalog
#'   variables and returns the cohort with exposure columns.
#' * `ss_catalog()` lists the catalog variables and the data each needs.
#' * `ss_data_status()` reports which variables are ready in the data
#'   directory and what is missing.
#' * `ss_fetch()` downloads (resumably) the data the variables need.
#'
#' @param cohort A data frame with `pid`, `startDate`, `endDate`,
#'   `longitude` and `latitude` (or a path to such a CSV/Parquet file).
#' @param variables Catalog variable keys, for example `c("ndi", "walkability")`.
#' @param datasets Dataset ids, as an alternative to `variables`.
#' @param data_dir The data directory.
#' @param ... Further arguments of the phase B design.
#' @return Nothing yet: these functions always raise an error.
#' @examples
#' try(ss_catalog())
#' @name phase-b
NULL

.ss_phase_b <- function(fn) {
  .ss_abort("not_implemented", sprintf("%s() is available in phase B of the spacescans R package.", fn),
            hint = "Use ss_run() with a pipeline config in the meantime.")
}

#' @rdname phase-b
#' @export
ss_link <- function(cohort, variables, data_dir = NULL, ...) .ss_phase_b("ss_link")

#' @rdname phase-b
#' @export
ss_catalog <- function(data_dir = NULL, ...) .ss_phase_b("ss_catalog")

#' @rdname phase-b
#' @export
ss_data_status <- function(variables = NULL, data_dir = NULL, ...) .ss_phase_b("ss_data_status")

#' @rdname phase-b
#' @export
ss_fetch <- function(variables = NULL, datasets = NULL, data_dir = NULL, ...) .ss_phase_b("ss_fetch")
