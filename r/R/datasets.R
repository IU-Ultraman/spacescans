#' Registered datasets: your own data, prepared once and reused
#'
#' Registers your own exposure data once, so that later [ss_link()] calls
#' use it by name like a catalog variable: `ss_link(cohort, "ndi", custom =
#' "my dataset")`.
#'
#' * `ss_register_dataset()` checks the files and prepares them once
#'   (normalized Parquet for tables; checked copies and a period index for
#'   rasters; GeoParquet in EPSG:5070 for features). The prepared files and
#'   a `dataset.json` record go to the registry; your original files are not
#'   changed and are no longer needed by the link.
#' * `ss_datasets()` lists the registered datasets.
#' * `ss_dataset()` returns one, with its variables and prepared files.
#' * `ss_update_dataset()` changes a dataset's name, metadata (including
#'   `visibility`) or the units and descriptions of its variables; the data
#'   stays.
#' * `ss_remove_dataset()` removes one: its record and prepared files (not
#'   your original files).
#'
#' The work is done by the registry of the Python package
#' (`spacescans.custom.register()` and its companions) through the bridge,
#' so a dataset registered from Python is visible from R and the other way
#' round. A dataset is found by its name (ignoring case), its id or its
#' variable key (`custom_` and the first 8 characters of the id).
#'
#' **Kinds.**
#' * `"polygon_table"`: a CSV file (or a data frame) of values keyed by a
#'   2010 Census tract, block group, ZCTA or county. `temporal`: `"static"`
#'   (one row per place), `"yearly"` (a year column) or `"daily"` (a date
#'   column). `key` names the geography: `"tract"`, `"block_group"`,
#'   `"zcta"` or `"county"` when the key column has its usual name (such as
#'   `GEOID10` or `GEOID`), else a list such as `list(boundary = "tract",
#'   column = "FIPS", year_column = "yr")` (`year_column` and `date_column`
#'   default to `year` and `date`).
#' * `"raster"`: GeoTIFF files on one grid. `temporal`: `"static"` (one
#'   file), `"yearly"`, `"biweekly"` or `"daily"` (one file per period). The
#'   period of each file comes from its name (a year, or a date such as
#'   `20160101` or `2016-01-01`) or from `periods`.
#' * `"features"`: point, line or polygon geometries (one geometry family per
#'   dataset) in a GeoPackage, a GeoJSON file or a zipped Shapefile, with a
#'   coordinate reference system. The variable is the distance in meters from
#'   each location to the nearest feature, named `<name>_dist_m` unless you
#'   name it. `temporal`: `"static"` or `"yearly"` (one file per year, or a
#'   year column).
#'
#' **Where.** The registry is the directory `root`, else the option
#' `spacescans.datasets_dir`, else the environment variable
#' `SPACESCANS_DATASETS_DIR`, else the Python package's default,
#' `platformdirs.user_data_dir("spacescans")/datasets` (on macOS
#' `~/Library/Application Support/spacescans/datasets`). Names are unique
#' within a registry. [ss_link()] and [ss_coverage()] look registered
#' datasets up by the same rule, so set the option when you keep them
#' elsewhere.
#'
#' **Visibility.** `"private"` (default) or `"deployment"`. The packages only
#' record it; a web app deployment enforces it (a `"deployment"` dataset is
#' offered to every user of that deployment).
#'
#' @param paths The data files: one CSV for a table; GeoTIFF files for a
#'   raster; a GeoPackage, GeoJSON or zipped Shapefile per year (or one file)
#'   for features. For a polygon table, a data frame is also accepted
#'   (written to a private temporary CSV file first).
#' @param name A name for the dataset, unique in the registry (1 to 80
#'   characters), for example `"SVI 2020"`. For `ss_update_dataset()`, the
#'   new name.
#' @param kind `"polygon_table"`, `"raster"` or `"features"`.
#' @param temporal `"static"`, `"yearly"`, `"biweekly"` (rasters) or
#'   `"daily"` (tables and rasters); features are `"static"` or `"yearly"`.
#' @param key How places (and times) are read from a polygon table: a
#'   geography name or a named list (see Details); optional for features
#'   (`list(year_column = , layer = )`); unused for rasters.
#' @param variables The variables: a character vector of names, or a data
#'   frame with a `name` column and optionally `unit`, `description`, and
#'   `column` (a table's column, when it differs from the name) or `band` (a
#'   raster band). `NULL` takes the defaults: every numeric column of a
#'   table; band 1 of a raster, named after the dataset; `<name>_dist_m` for
#'   features. For `ss_update_dataset()`, a data frame with `name` and the
#'   new `unit` and/or `description` of existing variables.
#' @param periods For rasters and yearly features: the year (a number) or the
#'   start date (a `Date` or `"2016-01-01"`) of each file, in the order of
#'   `paths`, when the file names do not carry them.
#' @param description,source,version,license_note Optional metadata: a
#'   description of the dataset, where it comes from, its version, and the
#'   terms under which it may be used.
#' @param visibility `"private"` (default) or `"deployment"`.
#' @param owner Optional owner (any text, for example your name).
#' @param root The registry directory (see Details).
#' @param overwrite Replace a dataset of the same name (it gets a new id).
#' @param name_or_id A dataset's name, id or variable key, or the record
#'   from `ss_register_dataset()` or `ss_dataset()`.
#' @return `ss_register_dataset()`, `ss_dataset()` and
#'   `ss_update_dataset()`: the dataset's record, a list of class
#'   `ss_dataset` (`id`, `name`, `kind`, `temporal`, `key`, `variables` (a
#'   data frame: `name`, `unit`, `description`), `source`, `version`,
#'   `description`, `license_note`, `visibility`, `owner`, `created_utc`,
#'   `files`, `checksum`, `spacescans_version`, `details`, `path` (its
#'   directory) and `variable_key`), with the registry directory as
#'   attribute `root`. `ss_datasets()`: a data frame with one row per dataset
#'   (variables as their names), attribute `root`. `ss_remove_dataset()`:
#'   the removed record, invisibly.
#' @examples
#' \dontrun{
#' svi <- ss_register_dataset("~/data/svi_2020_tract.csv", name = "SVI 2020",
#'                            kind = "polygon_table", temporal = "static",
#'                            key = list(boundary = "tract", column = "FIPS"),
#'                            variables = "RPL_THEMES",
#'                            source = "CDC/ATSDR SVI 2020", version = "2020")
#' ss_datasets()
#' res <- ss_link(cohort, "ndi", custom = "SVI 2020")
#' ss_update_dataset("SVI 2020", visibility = "deployment")
#' ss_remove_dataset("SVI 2020")
#' }
#' @name ss_datasets
NULL

.ss_dataset_kinds <- c("polygon_table", "raster", "features")
.ss_dataset_temporal <- list(
  polygon_table = c("static", "yearly", "daily"),
  raster = c("static", "yearly", "biweekly", "daily"),
  features = c("static", "yearly")
)
.ss_variable_fields <- c("name", "unit", "description", "column", "band")

#' @rdname ss_datasets
#' @export
ss_register_dataset <- function(paths, name, kind, temporal, key = NULL, variables = NULL,
                                periods = NULL, description = NULL, source = NULL, version = NULL,
                                license_note = NULL, visibility = c("private", "deployment"),
                                owner = NULL, root = NULL, overwrite = FALSE) {
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
  key <- .ss_key_arg(key)
  files <- .ss_dataset_files(paths, kind, name)
  periods <- .ss_periods_arg(periods, length(files))
  metadata <- .ss_metadata_arg(description = description, source = source, version = version,
                               license_note = license_note, visibility = visibility, owner = owner)
  args <- list(files = I(files), periods = periods, name = name, kind = kind, temporal = temporal,
               key = key, variables = .ss_dataset_variables(variables), metadata = metadata,
               root = .ss_datasets_root(root), overwrite = isTRUE(overwrite))
  value <- .ss_bridge("datasets-register", .ss_compact(args))
  .ss_dataset_record(value$dataset, .ss_datasets_root(root))
}

#' @rdname ss_datasets
#' @export
ss_datasets <- function(root = NULL) {
  args <- .ss_compact(list(root = .ss_datasets_root(root)))
  value <- .ss_bridge("datasets-list", args)
  rows <- value$datasets %||% list()
  df <- .ss_records_df(lapply(rows, .ss_dataset_row),
                       first = c("name", "id", "kind", "temporal", "variables", "visibility", "owner",
                                 "version", "source", "description", "created_utc", "variable_key"))
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
  value <- .ss_dataset_get(ref, root)
  .ss_dataset_record(value$dataset, value$root %||% .ss_datasets_root(root))
}

#' @rdname ss_datasets
#' @export
ss_update_dataset <- function(name_or_id, name = NULL, description = NULL, source = NULL,
                              version = NULL, license_note = NULL, visibility = NULL, owner = NULL,
                              variables = NULL, root = NULL) {
  ref <- .ss_dataset_ref(name_or_id)
  if (!is.null(visibility)) visibility <- .ss_choice(visibility, c("private", "deployment"), "visibility")
  metadata <- .ss_metadata_arg(description = description, source = source, version = version,
                               license_note = license_note, visibility = visibility, owner = owner)
  if (!is.null(variables)) {
    if (!is.data.frame(variables) || !"name" %in% names(variables) ||
        length(setdiff(names(variables), c("name", "unit", "description")))) {
      .ss_abort("config", "`variables` must be a data frame with `name` and the new `unit` and/or `description`.")
    }
  }
  args <- list(name = ref, new_name = .ss_string_arg(name, "name", null_ok = TRUE), metadata = metadata,
               variables = .ss_dataset_variables(variables), root = .ss_datasets_root(root))
  if (is.null(args$new_name) && is.null(args$metadata) && is.null(args$variables)) {
    .ss_abort("config", "Nothing to change: give a new name, metadata or variables.")
  }
  value <- .ss_bridge("datasets-update", .ss_compact(args))
  .ss_dataset_record(value$dataset, .ss_datasets_root(root))
}

#' @rdname ss_datasets
#' @export
ss_remove_dataset <- function(name_or_id, root = NULL) {
  ref <- .ss_dataset_ref(name_or_id)
  args <- .ss_compact(list(name = ref, root = .ss_datasets_root(root)))
  value <- .ss_bridge("datasets-remove", args)
  rec <- .ss_dataset_record(value$removed %||% value$dataset, .ss_datasets_root(root))
  message(sprintf("Removed the registered dataset \"%s\" (id %s); your original files were not touched.",
                  rec$name %||% ref, rec$id %||% "?"))
  invisible(rec)
}

#' @export
print.ss_dataset <- function(x, ...) {
  cat(sprintf("spacescans dataset \"%s\" (%s, %s)\n", x$name %||% "?", x$variable_key %||% x$id %||% "?",
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
  line("id", x$id)
  line("created", x$created_utc)
  line("files", length(unlist(x$files)))
  line("directory", x$path)
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

# The metadata fields given (NULL ones left out, so an update changes only those).
.ss_metadata_arg <- function(...) {
  fields <- list(...)
  for (nm in names(fields)) fields[[nm]] <- .ss_string_arg(fields[[nm]], nm, null_ok = TRUE)
  fields <- fields[!vapply(fields, is.null, logical(1))]
  if (length(fields)) fields else NULL
}

# `key`: a geography name or a named list (sent as a JSON object).
.ss_key_arg <- function(key) {
  if (is.null(key)) return(NULL)
  if (is.character(key) && length(key) == 1L && !is.na(key) && nzchar(key)) return(key)
  if (is.list(key) && length(key) && !is.null(names(key)) && all(nzchar(names(key)))) return(.ss_named(key))
  .ss_abort("config", "`key` must be a geography name such as \"tract\", or a named list such as list(boundary = \"tract\", column = \"FIPS\").")
}

# `periods`: one year or start date per file (NA for none), sent as numbers or ISO dates.
.ss_periods_arg <- function(periods, n) {
  if (is.null(periods)) return(NULL)
  if (length(periods) != n) {
    .ss_abort("config", sprintf("`periods` needs one year or date per file (%d), not %d.", n, length(periods)))
  }
  if (inherits(periods, "Date")) return(I(lapply(format(periods, "%Y-%m-%d"), function(p) if (is.na(p)) NULL else p)))
  if (is.numeric(periods)) {
    if (any(!is.na(periods) & periods != round(periods))) .ss_abort("config", "`periods` years must be whole numbers.")
    return(I(lapply(periods, function(p) if (is.na(p)) NULL else as.integer(p))))
  }
  if (is.character(periods)) return(I(lapply(periods, function(p) if (is.na(p) || !nzchar(p)) NULL else p)))
  .ss_abort("config", "`periods` must be years (numbers) or dates (Date or \"2016-01-01\").")
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

# `variables` as a list of {name, unit, description, column, band} records
# (NULL: the registry's defaults).
.ss_dataset_variables <- function(variables) {
  if (is.null(variables)) return(NULL)
  bad <- function() .ss_abort("config", paste0(
    "`variables` must be a character vector of names or a data frame with a `name` column ",
    "(and optionally `unit`, `description`, `column` or `band`)."))
  if (is.character(variables)) {
    if (!length(variables) || anyNA(variables) || any(!nzchar(variables))) bad()
    return(I(lapply(variables, function(v) list(name = v))))
  }
  if (is.data.frame(variables)) {
    if (!"name" %in% names(variables) || !nrow(variables)) bad()
    extra <- setdiff(names(variables), .ss_variable_fields)
    if (length(extra)) .ss_abort("config", sprintf("Unknown column(s) in `variables`: %s.", paste(extra, collapse = ", ")))
    rows <- lapply(seq_len(nrow(variables)), function(i) {
      r <- lapply(as.list(variables[i, , drop = FALSE]), function(v) if (is.factor(v)) as.character(v) else v)
      if (!is.null(r$band) && !is.na(r$band)) r$band <- as.integer(r$band)
      r[!vapply(r, function(v) length(v) != 1L || is.na(v), logical(1))]
    })
    if (any(vapply(rows, function(r) !is.character(r$name) || !nzchar(r$name), logical(1)))) bad()
    return(I(rows))
  }
  bad()
}

.ss_dataset_ref <- function(x) {
  if (inherits(x, "ss_dataset")) x <- x$id %||% x$name
  if (!is.character(x) || length(x) != 1L || is.na(x) || !nzchar(trimws(x))) {
    .ss_abort("config", "Give one dataset name, id or variable key (see ss_datasets()).")
  }
  x
}

# datasets-get: the record, its variable key and its linkable definition.
.ss_dataset_get <- function(ref, root = NULL) {
  args <- .ss_compact(list(name = ref, root = .ss_datasets_root(root)))
  .ss_bridge("datasets-get", args)
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
  if (is.null(root) && is.character(rec$path) && length(rec$path) == 1L) root <- dirname(rec$path)
  structure(rec, class = c("ss_dataset", "list"), root = root)
}

# One row of ss_datasets(): variables as their names; files and details left out.
.ss_dataset_row <- function(rec) {
  vars <- rec$variables %||% list()
  rec$variables <- paste(vapply(vars, function(v) as.character(if (is.list(v)) v$name %||% "" else v), ""),
                         collapse = ", ")
  rec$files <- NULL
  rec$details <- NULL
  rec$key <- if (is.list(rec$key)) paste(names(rec$key), unlist(rec$key), sep = "=", collapse = ", ") else rec$key
  rec
}

# The `custom` argument of ss_link() and ss_coverage(), in the order given:
# definitions from ss_custom_polygon()/ss_custom_raster(), and registered
# datasets (names, ids, variable keys or ss_dataset records). Checked in R;
# nothing is looked up yet.
.ss_custom_items <- function(custom) {
  if (is.null(custom)) return(list())
  if (is.character(custom)) custom <- as.list(custom)
  if (inherits(custom, c("ss_custom", "ss_dataset")) || (is.list(custom) && !is.null(custom[["variable_key"]]))) {
    custom <- list(custom)
  }
  if (!is.list(custom)) {
    .ss_abort("config", paste0("`custom` must name registered datasets (see ss_datasets()) or hold ",
                               "definitions from ss_custom_polygon() or ss_custom_raster()."))
  }
  items <- list()
  for (d in custom) {
    if (inherits(d, "ss_dataset")) {
      items[[length(items) + 1L]] <- list(registered = .ss_dataset_ref(d))
    } else if (is.character(d)) {
      if (!length(d) || anyNA(d) || any(!nzchar(trimws(d)))) {
        .ss_abort("config", "Registered dataset names in `custom` must be non-empty strings.")
      }
      for (ref in d) items[[length(items) + 1L]] <- list(registered = ref)
    } else if (is.list(d) && !is.null(d[["variable_key"]])) {
      items[[length(items) + 1L]] <- list(definition = d)
    } else {
      .ss_abort("config", paste0("Every `custom` element must be a registered dataset's name or id, ",
                                 "or a definition from ss_custom_polygon() or ss_custom_raster()."))
    }
  }
  items
}

# {variable_key: definition} for the link or coverage request, in the order
# given: a registered dataset's definition comes from the registry
# (datasets-get), so link and coverage treat it like any other custom
# definition. The same dataset given twice counts once.
.ss_custom_resolve <- function(items) {
  out <- list()
  for (it in items) {
    if (!is.null(it$registered)) {
      got <- .ss_dataset_get(it$registered)
      def <- got$definition
      key <- got$key %||% def$variable_key
      if (!is.list(def) || !is.character(key) || length(key) != 1L) {
        .ss_abort("internal", sprintf("The registry returned no linkable definition for %s.", it$registered))
      }
      def <- unclass(def)
      attr(def, "spacescans") <- NULL
      out[[key]] <- def
    } else {
      def <- unclass(it$definition)
      attr(def, "spacescans") <- NULL
      out[[def$variable_key]] <- def
    }
  }
  out
}
