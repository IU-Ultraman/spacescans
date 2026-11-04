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

#' Ontology concepts for annotating datasets
#'
#' Lists or searches the concepts of the Spatial and Contextual Exposome
#' Ontology that a registered dataset, or one of its variables, can carry as
#' `ontology_id` (see [ss_register_dataset()]). The catalog's own variables
#' carry one too (`ontology_id` in [ss_catalog()]). The concepts come with
#' the Python package, so no network is needed.
#'
#' A search compares the words of `query` with the beginnings of the words in
#' each concept's label (first) and definition, ignoring case, underscores
#' and dots (`"pm25"` finds Particulate_Matter_PM2.5).
#'
#' @param query Words to look for, or `NULL` (default) for every concept,
#'   root first.
#' @param limit Most concepts a search returns.
#' @return A data frame with `id`, `label`, `path` (the labels from the
#'   exposome root down to the concept, joined with " > ") and
#'   `definition`, best match first; attributes `ontology` and `root`.
#' @examples
#' \dontrun{
#' ss_concepts("pm2.5")
#' ss_register_dataset(tifs, name = "PM2.5 biweekly", kind = "raster", temporal = "biweekly",
#'                     ontology_id = "000102_2")
#' }
#' @export
ss_concepts <- function(query = NULL, limit = 20) {
  if (!is.null(query) && (!is.character(query) || length(query) != 1L || is.na(query))) {
    .ss_abort("config", "`query` must be a single string, or NULL for every concept.")
  }
  if (!is.numeric(limit) || length(limit) != 1L || is.na(limit) || limit < 1 || limit != round(limit)) {
    .ss_abort("config", "`limit` must be a whole number of at least 1.")
  }
  args <- if (is.null(query)) list() else list(query = query, limit = as.integer(limit))
  value <- .ss_bridge("concepts", .ss_compact(args))
  rows <- value$concepts %||% list()
  text <- function(f, sep = "") {
    vapply(rows, function(r) paste(as.character(unlist(r[[f]])), collapse = sep), character(1))
  }
  df <- data.frame(id = text("id"), label = text("label"), path = text("path", " > "),
                   definition = text("definition"), stringsAsFactors = FALSE)
  attr(df, "ontology") <- value$ontology
  attr(df, "root") <- value$root
  df
}
