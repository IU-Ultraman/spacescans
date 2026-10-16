#' Registered datasets: your own data, prepared once and reused
#'
#' Registers your own exposure data once, so that later [ss_link()] calls
#' use it by name like a catalog variable: `ss_link(cohort, "ndi", custom =
#' "my_dataset")`.
#'
#' * `ss_register_dataset()` checks the files and prepares them once
#'   (normalized Parquet for tables; a checked grid and period index for
#'   rasters; GeoParquet in EPSG:5070 for features). The prepared files and
#'   a `dataset.json` record are stored in the registry; your original
#'   files are not changed and are no longer needed by the link.
#' * `ss_datasets()` lists the registered datasets.
#' * `ss_dataset()` returns one, with its variables and prepared files.
#' * `ss_remove_dataset()` removes one: its record and prepared files (not
#'   your original files).
#'
#' The work is done by the Python registry (`spacescans.custom.register()`,
#' `list_datasets()`, `get_dataset()`, `remove_dataset()`) through the
#' bridge, so a dataset registered from Python is visible from R and the
#' other way round.
#'
#' **Kinds.**
#' * `"polygon_table"`: a CSV file (or a data frame) of values keyed by a
#'   2010 Census tract, block group, ZCTA or county. `temporal`: `"static"`,
#'   `"yearly"` (a year column) or `"daily"` (a date column).
#' * `"raster"`: GeoTIFF files. `temporal`: `"static"` (one file),
#'   `"yearly"`, `"biweekly"` or `"daily"` (one file per period, all on one
#'   grid).
#' * `"features"`: point, line or polygon geometries (one geometry type per
#'   dataset) in a GeoPackage, a GeoJSON file or a zipped Shapefile, with a
#'   coordinate reference system. The variable is the distance in metres
#'   from each location to the nearest feature; its name is yours (default
#'   `<name>_dist_m`). `temporal`: `"static"` or `"yearly"`.
#'
#' **Where.** The registry is the directory `root`, else the option
#' `spacescans.datasets_dir`, else the environment variable
#' `SPACESCANS_DATASETS_DIR`, else the Python package's default,
#' `platformdirs.user_data_dir("spacescans")/datasets` (on macOS
#' `~/Library/Application Support/spacescans/datasets`). Names are unique
#' within a registry. [ss_link()] uses the same rule without `root`, so set
#' the option (or the variable) when you keep datasets elsewhere.
#'
#' **Visibility.** `"private"` (default) or `"deployment"`. The packages only
#' record it; a web app deployment enforces it (a `"deployment"` dataset is
#' offered to every user of that deployment).
#'
#' @param paths The data files: one CSV for a table; GeoTIFF files for a
#'   raster; one GeoPackage, GeoJSON or zipped Shapefile per year (or one
#'   file) for features. For a polygon table, a data frame is also accepted
#'   (written to a private temporary CSV file first).
#' @param name A unique name for the dataset in the registry, for example
#'   `"svi_2020"`.
#' @param kind `"polygon_table"`, `"raster"` or `"features"`.
#' @param temporal `"static"`, `"yearly"`, `"biweekly"` (rasters) or
#'   `"daily"` (tables and rasters); features are `"static"` or `"yearly"`.
#' @param key For a polygon table, the Census key of its rows, as the Python
#'   function `register()` takes it (a string, or a named list).
#' @param variables The variables: a character vector of names, or a data
#'   frame with columns `name` and optionally `unit` and `description` (one
#'   row per variable). `NULL` lets the registry choose (every value column
#'   of a table; the band or file names of a raster; `<name>_dist_m` for
#'   features).
#' @param description,source,version,license_note Optional metadata: a
#'   description of the dataset, where it comes from, its version, and the
#'   terms under which it may be used.
#' @param visibility `"private"` (default) or `"deployment"`.
#' @param owner Optional owner (any text, for example your name).
#' @param root The registry directory (see Details).
#' @param overwrite Replace a dataset of the same name.
#' @param options Further arguments of the Python function `register()`,
#'   as a named list, for settings the arguments above do not cover.
#' @param name_or_id A dataset's name or id, or the record from
#'   `ss_register_dataset()` or `ss_dataset()`.
#' @return `ss_register_dataset()` and `ss_dataset()`: the dataset's record,
#'   a list of class `ss_dataset` (`id`, `name`, `kind`, `temporal`, `key`,
#'   `variables` (a data frame: `name`, `unit`, `description`), `source`,
#'   `version`, `description`, `license_note`, `visibility`, `owner`,
#'   `created_utc`, `files`, `checksum`, `spacescans_version`), with the
#'   registry directory as attribute `root`. `ss_datasets()`: a data frame
#'   with one row per dataset (list fields joined with ", "), attribute
#'   `root`. `ss_remove_dataset()`: the removed record, invisibly.
#' @examples
#' \dontrun{
#' svi <- ss_register_dataset("~/data/svi_2020_tract.csv", name = "svi_2020",
#'                            kind = "polygon_table", temporal = "static",
#'                            key = "Tract", variables = "RPL_THEMES",
#'                            source = "CDC/ATSDR SVI 2020", version = "2020")
#' ss_datasets()
#' res <- ss_link(cohort, "ndi", custom = "svi_2020")
#' ss_remove_dataset("svi_2020")
#' }
#' @name ss_datasets
NULL

.ss_dataset_kinds <- c("polygon_table", "raster", "features")
.ss_dataset_temporal <- list(
  polygon_table = c("static", "yearly", "daily"),
  raster = c("static", "yearly", "biweekly", "daily"),
  features = c("static", "yearly")
)

#' @rdname ss_datasets
#' @export
ss_register_dataset <- function(paths, name, kind, temporal, key = NULL, variables = NULL,
                                description = NULL, source = NULL, version = NULL,
                                license_note = NULL, visibility = c("private", "deployment"),
                                owner = NULL, root = NULL, overwrite = FALSE, options = NULL) {
  name <- .ss_string_arg(name, "name")
  if (missing(kind)) .ss_abort("config", sprintf("`kind` is required: one of %s.", .ss_quote(.ss_dataset_kinds)))
  kind <- .ss_choice(kind, .ss_dataset_kinds, "kind")
  allowed <- .ss_dataset_temporal[[kind]]
  if (missing(temporal)) .ss_abort("config", sprintf("`temporal` is required: one of %s.", .ss_quote(allowed)))
  if (!is.character(temporal) || length(temporal) != 1L || is.na(temporal) ||
      !temporal %in% unique(unlist(.ss_dataset_temporal))) {
    .ss_abort("config", sprintf("`temporal` must be one of %s.", .ss_quote(unique(unlist(.ss_dataset_temporal)))))
  }
  if (!temporal %in% allowed) {
    .ss_abort("config", sprintf("A %s dataset is %s, not \"%s\".", kind, .ss_quote(allowed, "or"), temporal))
  }
  visibility <- .ss_choice(visibility, c("private", "deployment"), "visibility")
  if (!is.logical(overwrite) || length(overwrite) != 1L || is.na(overwrite)) {
    .ss_abort("config", "`overwrite` must be TRUE or FALSE.")
  }
  if (!is.null(options) && (!is.list(options) || (length(options) && (is.null(names(options)) ||
                                                                       any(!nzchar(names(options))))))) {
    .ss_abort("config", "`options` must be a named list.")
  }
  if (!is.null(key) && !(is.character(key) && length(key) == 1L && !is.na(key) && nzchar(key)) &&
      !(is.list(key) && length(key) && !is.null(names(key)) && all(nzchar(names(key))))) {
    .ss_abort("config", "`key` must be a single string or a named list.")
  }
  files <- .ss_dataset_files(paths, kind, name)
  metadata <- list(description = .ss_string_arg(description, "description", null_ok = TRUE),
                   source = .ss_string_arg(source, "source", null_ok = TRUE),
                   version = .ss_string_arg(version, "version", null_ok = TRUE),
                   license_note = .ss_string_arg(license_note, "license_note", null_ok = TRUE),
                   visibility = visibility,
                   owner = .ss_string_arg(owner, "owner", null_ok = TRUE))
  args <- list(paths = I(files), name = name, kind = kind, temporal = temporal, key = key,
               variables = .ss_dataset_variables(variables),
               metadata = .ss_named(metadata[!vapply(metadata, is.null, logical(1))]),
               root = .ss_datasets_root(root), overwrite = isTRUE(overwrite),
               options = if (length(options)) .ss_named(options))
  args <- args[!vapply(args, is.null, logical(1))]
  value <- .ss_bridge("dataset-register", args, env = .ss_datasets_env())
  .ss_dataset_record(value$dataset %||% value, value$root)
}

#' @rdname ss_datasets
#' @export
ss_datasets <- function(root = NULL) {
  value <- .ss_bridge("dataset-list", .ss_compact(list(root = .ss_datasets_root(root))),
                      env = .ss_datasets_env())
  rows <- value$datasets %||% list()
  df <- .ss_records_df(lapply(rows, .ss_dataset_row),
                       first = c("name", "id", "kind", "temporal", "variables", "visibility", "owner",
                                 "version", "source", "description", "created_utc"))
  if (!nrow(df)) {
    df <- data.frame(name = character(), id = character(), kind = character(), temporal = character(),
                     variables = character(), visibility = character(), stringsAsFactors = FALSE)
  }
  attr(df, "root") <- value$root
  df
}

#' @rdname ss_datasets
#' @export
ss_dataset <- function(name_or_id, root = NULL) {
  ref <- .ss_dataset_ref(name_or_id)
  value <- .ss_bridge("dataset-get", .ss_compact(list(name_or_id = ref, root = .ss_datasets_root(root))),
                      env = .ss_datasets_env())
  .ss_dataset_record(value$dataset %||% value, value$root)
}

#' @rdname ss_datasets
#' @export
ss_remove_dataset <- function(name_or_id, root = NULL) {
  ref <- .ss_dataset_ref(name_or_id)
  value <- .ss_bridge("dataset-remove", .ss_compact(list(name_or_id = ref, root = .ss_datasets_root(root))),
                      env = .ss_datasets_env())
  rec <- .ss_dataset_record(value$dataset %||% value, value$root)
  message(sprintf("Removed the registered dataset \"%s\" (id %s)%s; your original files were not touched.",
                  rec$name %||% ref, rec$id %||% "?",
                  if (!is.null(attr(rec, "root"))) paste0(" from ", attr(rec, "root")) else ""))
  invisible(rec)
}

#' @export
print.ss_dataset <- function(x, ...) {
  cat(sprintf("spacescans dataset \"%s\" (id %s, %s)\n", x$name %||% "?", x$id %||% "?",
              x$visibility %||% "private"))
  line <- function(label, value) {
    if (!is.null(value) && length(value) && !all(is.na(value)) && any(nzchar(as.character(value)))) {
      cat(sprintf("  %-12s%s\n", label, paste(as.character(value), collapse = ", ")))
    }
  }
  line("kind", paste(c(x$kind, x$temporal), collapse = ", "))
  if (!is.null(x$key)) line("key", if (is.list(x$key)) paste(names(x$key), unlist(x$key), sep = "=") else x$key)
  v <- x$variables
  if (is.data.frame(v) && nrow(v)) {
    unit <- if ("unit" %in% names(v)) ifelse(is.na(v$unit) | !nzchar(v$unit), "", paste0(" (", v$unit, ")")) else ""
    line("variables", paste0(v$name, unit))
  }
  line("description", x$description)
  line("source", x$source)
  line("version", x$version)
  line("license", x$license_note)
  line("owner", x$owner)
  line("created", x$created_utc)
  line("files", length(unlist(x$files)))
  line("registry", attr(x, "root"))
  invisible(x)
}

# --- helpers -------------------------------------------------------------------

.ss_quote <- function(x, last = "or") {
  q <- paste0('"', x, '"')
  if (length(q) <= 1L) return(q)
  paste(paste(utils::head(q, -1L), collapse = ", "), last, utils::tail(q, 1L))
}

.ss_compact <- function(x) .ss_named(x[!vapply(x, is.null, logical(1))])

.ss_string_arg <- function(x, name, null_ok = FALSE) {
  if (missing(x)) .ss_abort("config", sprintf("`%s` is required.", name))
  if (is.null(x) && null_ok) return(NULL)
  if (!is.character(x) || length(x) != 1L || is.na(x) || !nzchar(trimws(x))) {
    .ss_abort("config", sprintf("`%s` must be a single non-empty string.", name))
  }
  x
}

# The registry root to send: the argument, else the option; NULL lets
# Python decide (SPACESCANS_DATASETS_DIR, then its platformdirs default).
.ss_datasets_root <- function(root = NULL) {
  root <- root %||% getOption("spacescans.datasets_dir")
  if (is.null(root)) return(NULL)
  if (!is.character(root) || length(root) != 1L || is.na(root) || !nzchar(root)) {
    .ss_abort("config", "`root` must be a single directory path.")
  }
  .ss_absolute(root)
}

# The environment a bridge needs to find the registry when no root is sent
# (ss_link(), which passes registered names to the Python link).
.ss_datasets_env <- function() {
  root <- .ss_datasets_root(NULL)
  if (is.null(root)) list() else list(SPACESCANS_DATASETS_DIR = root)
}

# Absolute paths of the data files; a data frame (polygon tables) is written
# to a private temporary CSV file first.
.ss_dataset_files <- function(paths, kind, name) {
  if (is.data.frame(paths)) {
    if (!identical(kind, "polygon_table")) {
      .ss_abort("config", "A data frame can only be registered as a \"polygon_table\".")
    }
    dir <- file.path(tempdir(), "spacescans-datasets")
    dir.create(dir, showWarnings = FALSE, recursive = TRUE, mode = "0700")
    path <- tempfile(pattern = paste0(gsub("[^A-Za-z0-9_-]+", "_", name), "-"), tmpdir = dir,
                     fileext = ".csv")
    utils::write.csv(paths, path, row.names = FALSE, na = "")
    Sys.chmod(path, "0600", use_umask = FALSE)
    paths <- path
  }
  if (!is.character(paths) || !length(paths) || anyNA(paths) || any(!nzchar(paths))) {
    .ss_abort("config", "`paths` must be the paths of the data files.")
  }
  expanded <- path.expand(paths)
  missing <- paths[!file.exists(expanded) | dir.exists(expanded)]
  if (length(missing)) {
    .ss_abort("config", sprintf("File(s) not found: %s", paste(missing, collapse = ", ")))
  }
  normalizePath(expanded)
}

# `variables` as a list of {name, unit, description} records (NULL: the
# registry's defaults).
.ss_dataset_variables <- function(variables) {
  if (is.null(variables)) return(NULL)
  bad <- function() .ss_abort("config", paste0(
    "`variables` must be a character vector of names or a data frame with a `name` column ",
    "(and optionally `unit` and `description`)."))
  if (is.character(variables)) {
    if (!length(variables) || anyNA(variables) || any(!nzchar(variables))) bad()
    return(I(lapply(variables, function(v) list(name = v))))
  }
  if (is.data.frame(variables)) {
    if (!"name" %in% names(variables) || !nrow(variables)) bad()
    extra <- setdiff(names(variables), c("name", "unit", "description"))
    if (length(extra)) .ss_abort("config", sprintf("Unknown column(s) in `variables`: %s.", paste(extra, collapse = ", ")))
    rows <- lapply(seq_len(nrow(variables)), function(i) {
      r <- lapply(as.list(variables[i, , drop = FALSE]), function(v) if (is.factor(v)) as.character(v) else v)
      r[!vapply(r, function(v) length(v) != 1L || is.na(v), logical(1))]
    })
    if (any(vapply(rows, function(r) is.null(r$name) || !nzchar(r$name), logical(1)))) bad()
    return(I(rows))
  }
  if (is.list(variables) && length(variables) &&
      all(vapply(variables, function(r) is.list(r) && is.character(r$name) && length(r$name) == 1L, logical(1)))) {
    return(I(lapply(variables, function(r) r[intersect(names(r), c("name", "unit", "description"))])))
  }
  bad()
}

.ss_dataset_ref <- function(x) {
  if (inherits(x, "ss_dataset")) x <- x$id %||% x$name
  if (!is.character(x) || length(x) != 1L || is.na(x) || !nzchar(x)) {
    .ss_abort("config", "Give one dataset name or id (see ss_datasets()).")
  }
  x
}

# A record from the bridge as an ss_dataset: variables as a data frame,
# files as a character vector, the registry root as attribute.
.ss_dataset_record <- function(rec, root = NULL) {
  if (!is.list(rec) || is.null(rec$id %||% rec$name)) {
    .ss_abort("internal", "The bridge returned no dataset record.")
  }
  rec <- unclass(rec)
  attr(rec, "spacescans") <- NULL
  vars <- rec$variables %||% list()
  rec$variables <- if (length(vars)) {
    df <- .ss_records_df(lapply(vars, function(v) if (is.list(v)) v else list(name = v)),
                         first = c("name", "unit", "description"))
    for (col in setdiff(c("name", "unit", "description"), names(df))) df[[col]] <- NA_character_
    df[, c("name", "unit", "description", setdiff(names(df), c("name", "unit", "description"))), drop = FALSE]
  } else {
    data.frame(name = character(), unit = character(), description = character(), stringsAsFactors = FALSE)
  }
  rec$files <- .ss_chr(rec$files)
  structure(rec, class = c("ss_dataset", "list"), root = root)
}

# One row of ss_datasets(): variables as their names, the rest as given.
.ss_dataset_row <- function(rec) {
  vars <- rec$variables %||% list()
  rec$variables <- paste(vapply(vars, function(v) as.character(if (is.list(v)) v$name %||% "" else v), ""),
                         collapse = ", ")
  rec$files <- NULL
  rec
}

# The `custom` argument of ss_link() and ss_coverage(): definitions from
# ss_custom_polygon()/ss_custom_raster() (keyed by variable key) and
# registered datasets (names, ids or ss_dataset records).
.ss_custom_args <- function(custom) {
  out <- list(definitions = list(), datasets = character())
  if (is.null(custom)) return(out)
  if (is.character(custom)) custom <- as.list(custom)
  if (inherits(custom, c("ss_custom", "ss_dataset")) || (is.list(custom) && !is.null(custom$variable_key))) {
    custom <- list(custom)
  }
  if (!is.list(custom)) {
    .ss_abort("config", paste0("`custom` must name registered datasets (see ss_datasets()) or hold ",
                               "definitions from ss_custom_polygon() or ss_custom_raster()."))
  }
  defs <- list()
  for (d in custom) {
    if (inherits(d, "ss_dataset")) {
      out$datasets <- c(out$datasets, .ss_dataset_ref(d))
    } else if (is.character(d)) {
      if (!length(d) || anyNA(d) || any(!nzchar(d))) {
        .ss_abort("config", "Registered dataset names in `custom` must be non-empty strings.")
      }
      out$datasets <- c(out$datasets, d)
    } else if (is.list(d) && !is.null(d$variable_key)) {
      defs[[length(defs) + 1L]] <- d
    } else {
      .ss_abort("config", paste0("Every `custom` element must be a registered dataset's name or id, ",
                                 "or a definition from ss_custom_polygon() or ss_custom_raster()."))
    }
  }
  out$definitions <- .ss_custom_map(defs)
  out$datasets <- unique(out$datasets)
  out
}
