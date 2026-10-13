# The exposure catalog: variables, the data and extras they need, and the
# feature dictionary. The catalog is fixed for an installed Python package,
# so the bridge is asked once per session and interpreter.

.ss_catalog_value <- function(refresh = FALSE) {
  py <- ss_python()$path
  cached <- .ss_state$catalog
  if (!refresh && !is.null(cached) && identical(cached$python, py)) return(cached$value)
  value <- .ss_bridge("catalog", list())
  .ss_state$catalog <- list(python = py, value = value)
  value
}

# One row per variable from the bridge's catalog value.
.ss_catalog_rows <- function(value) {
  vars <- value$variables %||% list()
  df <- .ss_records_df(vars)
  if (".key" %in% names(df)) {
    if ("key" %in% names(df)) df$.key <- NULL else names(df)[names(df) == ".key"] <- "key"
  }
  if ("key" %in% names(df) && !"variable" %in% names(df)) names(df)[names(df) == "key"] <- "variable"
  df
}

#' The exposure catalog
#'
#' Lists the catalog variables: label, data source, years, spatial unit,
#' output columns, the datasets each needs and the Python extras its
#' pattern needs. With `data_dir`, a `status` column says whether the data
#' is in place (see [ss_data_status()]).
#'
#' @param data_dir A data directory to check, or `NULL` (default) for the
#'   catalog alone.
#' @return A data frame with one row per variable (`variable`, `label`,
#'   `experiment`, `data_source`, `year_min`, `year_max`, `boundary`,
#'   `value_cols`, `datasets`, `extras`, ...; list-valued fields are joined
#'   with ", "), plus `status` and `usable` when `data_dir` is given.
#' @examples
#' \dontrun{
#' cat <- ss_catalog()
#' cat[, c("variable", "label", "year_min", "year_max")]
#' }
#' @export
ss_catalog <- function(data_dir = NULL) {
  value <- .ss_catalog_value()
  df <- .ss_catalog_rows(value)
  if ("coverage_years" %in% names(df)) {
    yrs <- strsplit(as.character(df$coverage_years), ", ", fixed = TRUE)
    df$year_min <- suppressWarnings(as.integer(vapply(yrs, function(y) y[1], "")))
    df$year_max <- suppressWarnings(as.integer(vapply(yrs, function(y) utils::tail(y, 1), "")))
  }
  first <- intersect(c("variable", "label", "experiment", "data_source", "year_min", "year_max",
                       "coverage_region", "boundary", "spatial_method", "temporal", "value_cols",
                       "datasets", "extras"), names(df))
  df <- df[, c(first, setdiff(names(df), c(first, "coverage_years", "raw"))), drop = FALSE]
  if (!is.null(data_dir)) {
    st <- ss_data_status(variables = df$variable, data_dir = data_dir)
    i <- match(df$variable, st$variable)
    df$status <- st$status[i]
    df$usable <- st$usable[i]
  }
  attr(df, "data_release") <- value$data_release
  attr(df, "catalog_hash") <- value$catalog_hash
  df
}

#' The feature dictionary
#'
#' Describes every exposure column [ss_link()] can produce: its name, a short
#' and a detailed description.
#'
#' @param variables Variable keys to restrict to, or `NULL` (default) for
#'   every column.
#' @return A data frame with `feature_name`, `short_description`,
#'   `detailed_description` (and the variable, when the bridge reports it).
#' @examples
#' \dontrun{
#' ss_features("ndi")
#' }
#' @export
ss_features <- function(variables = NULL) {
  value <- .ss_catalog_value()
  df <- .ss_records_df(value$features %||% list())
  if (".key" %in% names(df)) {
    if ("feature_name" %in% names(df)) df$.key <- NULL else names(df)[names(df) == ".key"] <- "feature_name"
  }
  if (is.null(variables) || !nrow(df)) return(df)
  if (!is.character(variables)) .ss_abort("config", "`variables` must be a character vector.")
  for (col in c("variable", "variables")) {
    if (col %in% names(df)) {
      keep <- vapply(strsplit(as.character(df[[col]]), ", ", fixed = TRUE),
                     function(v) any(v %in% variables), logical(1))
      return(df[keep, , drop = FALSE])
    }
  }
  rows <- .ss_catalog_rows(value)
  cols <- unique(unlist(strsplit(as.character(rows$value_cols[rows$variable %in% variables]), ", ", fixed = TRUE)))
  keep <- df$feature_name %in% cols | Reduce(`|`, lapply(variables, function(v) startsWith(df$feature_name, paste0(v, "_"))),
                                             init = rep(FALSE, nrow(df)))
  df[keep, , drop = FALSE]
}
