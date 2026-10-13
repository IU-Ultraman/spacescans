#' Bring your own exposure data
#'
#' Validates your own values so [ss_link()] can link them like a catalog
#' variable:
#' * `ss_custom_polygon()`: a CSV (or data frame) of values keyed by a
#'   Census geography (`Tract`, `BG`, `ZCTA5` or `County`), static or one row
#'   per year;
#' * `ss_custom_raster()`: a GeoTIFF, or one per year on the same grid, with
#'   one or more bands (needs the `geo` extra).
#'
#' The definition points at your files where they are; keep them in place
#' until the link has run. Pass it to `ss_link(custom = )`; its key
#' (`custom_<id>`) is added to the variables automatically.
#'
#' @param values Path of a CSV file, or a data frame (written to a CSV file
#'   of its own under [tempdir()] for this session).
#' @param boundary The geography the key column holds.
#' @param key_col Column with the geography id (GEOID).
#' @param value_cols Columns with the values to link.
#' @param year_col Column with the year, for yearly data; `NULL` for static.
#' @param name A label for the dataset (default: the file name).
#' @param labels,units Named character vectors of labels and units, keyed by
#'   value column.
#' @param paths GeoTIFF paths.
#' @param years The year of each raster, or `NULL` for one static raster.
#' @param bands Band numbers to read.
#' @return A definition: a list of class `ss_custom` (`variable_key`,
#'   `label`, `boundary`, `value_cols`, ...).
#' @examples
#' \dontrun{
#' svi <- ss_custom_polygon("svi_tract.csv", boundary = "Tract",
#'                          key_col = "FIPS", value_cols = c("RPL_THEMES"))
#' res <- ss_link(cohort, "ndi", custom = svi)
#' }
#' @export
ss_custom_polygon <- function(values, boundary = c("Tract", "BG", "ZCTA5", "County"), key_col,
                              value_cols, year_col = NULL, name = NULL, labels = NULL, units = NULL) {
  boundary <- .ss_choice(boundary, c("Tract", "BG", "ZCTA5", "County"), "boundary")
  if (missing(key_col) || !is.character(key_col) || length(key_col) != 1L) {
    .ss_abort("config", "`key_col` must name the column with the geography id.")
  }
  if (missing(value_cols) || !is.character(value_cols) || !length(value_cols)) {
    .ss_abort("config", "`value_cols` must name at least one value column.")
  }
  if (is.data.frame(values)) {
    label <- name %||% "values"
    dir <- file.path(tempdir(), "spacescans-custom")
    dir.create(dir, showWarnings = FALSE, recursive = TRUE, mode = "0700")
    # A file per definition: a second data frame (or a name that differs only
    # in punctuation) must never overwrite the file an earlier definition hashed.
    path <- tempfile(pattern = paste0(gsub("[^A-Za-z0-9_-]+", "_", label), "-"), tmpdir = dir,
                     fileext = ".csv")
    utils::write.csv(values, path, row.names = FALSE, na = "")
    values <- path
  }
  if (!is.character(values) || length(values) != 1L || !file.exists(path.expand(values))) {
    .ss_abort("config", sprintf("Values file not found: %s", format(values)))
  }
  options <- list(boundary = boundary, key_col = key_col, value_cols = I(value_cols),
                  year_col = year_col, label = name,
                  labels = if (!is.null(labels)) as.list(labels),
                  units = if (!is.null(units)) as.list(units))
  .ss_custom_validate("polygon", normalizePath(path.expand(values)), options)
}

#' @rdname ss_custom_polygon
#' @export
ss_custom_raster <- function(paths, years = NULL, bands = 1L, value_cols = NULL, name = NULL,
                             labels = NULL, units = NULL) {
  if (!is.character(paths) || !length(paths)) .ss_abort("config", "`paths` must be GeoTIFF file paths.")
  missing <- paths[!file.exists(path.expand(paths))]
  if (length(missing)) .ss_abort("config", sprintf("Raster not found: %s", paste(missing, collapse = ", ")))
  if (!is.null(years) && length(years) != length(paths)) {
    .ss_abort("config", "`years` needs one year per raster (or NULL for one static raster).")
  }
  bands <- as.integer(bands)
  if (anyNA(bands) || any(bands < 1L)) .ss_abort("config", "`bands` must be band numbers (1, 2, ...).")
  options <- list(years = if (!is.null(years)) I(as.integer(years)), bands = I(bands),
                  value_cols = if (!is.null(value_cols)) I(value_cols), label = name,
                  labels = if (!is.null(labels)) as.list(labels),
                  units = if (!is.null(units)) as.list(units))
  .ss_custom_validate("raster", normalizePath(path.expand(paths)), options)
}

.ss_custom_validate <- function(kind, files, options) {
  options <- options[!vapply(options, is.null, logical(1))]
  value <- .ss_bridge("custom-validate", list(kind = kind, files = I(files), options = .ss_named(options)))
  def <- value$definition %||% value
  attr(def, "spacescans") <- NULL
  if (is.null(def$variable_key)) .ss_abort("internal", "custom-validate returned no variable_key.")
  structure(def, class = c("ss_custom", "list"))
}

#' @export
print.ss_custom <- function(x, ...) {
  cat("spacescans custom dataset ", x$variable_key, ": ", x$label %||% "", "\n", sep = "")
  cat("  ", x$geometry %||% "", " on ", x$boundary %||% "", ", ", x$temporal %||% "", "; columns: ",
      paste(unlist(x$value_cols), collapse = ", "), "\n", sep = "")
  invisible(x)
}

# A named list {variable_key: definition} for the link request.
.ss_custom_map <- function(custom) {
  if (is.null(custom)) return(list())
  if (inherits(custom, "ss_custom") || (is.list(custom) && !is.null(custom$variable_key))) custom <- list(custom)
  if (!is.list(custom)) .ss_abort("config", "`custom` must be a definition from ss_custom_polygon() or ss_custom_raster(), or a list of them.")
  out <- list()
  for (d in custom) {
    if (!is.list(d) || is.null(d$variable_key)) {
      .ss_abort("config", "Every `custom` element must be a definition from ss_custom_polygon() or ss_custom_raster().")
    }
    def <- unclass(d)
    attr(def, "spacescans") <- NULL
    out[[d$variable_key]] <- def
  }
  out
}
