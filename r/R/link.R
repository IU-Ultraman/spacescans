#' Link a cohort to catalog variables
#'
#' Computes environmental exposures for every residential episode of a
#' cohort and returns the cohort with the exposure columns added.
#'
#' What happens, in order:
#' 1. The Python bridge is checked once per session (versions, protocol).
#' 2. The cohort is checked in R: `cols` maps your column names; `PATID` in
#'    any case stands in for `pid`; `pid` becomes text; dates become `Date`
#'    (date-times are converted with a warning; text dates must use one
#'    format, `2017-08-19` or `8/19/2017`); coordinates must be present and
#'    in range. Problems are reported with row numbers, never values.
#' 3. Only the five columns `pid`, `startDate`, `endDate`, `longitude` and
#'    `latitude` are written, to `cohort.parquet` in a private run directory
#'    (mode 0700). Your other columns never leave R.
#' 4. The data directory is checked ([ss_data_status()]): when data is
#'    missing the error says which [ss_fetch()] call to run, and when a
#'    Python extra is missing, which [ss_install()] call. `ss_link()` never
#'    downloads anything.
#' 5. The Python side runs the pipeline steps, showing progress. Ctrl-C asks
#'    it to stop, waits up to 10 seconds (option
#'    `spacescans.interrupt_grace`) and then kills its process tree.
#'    Completed C3 steps stay in the cache.
#' 6. The exposures are joined back onto your data frame by row (the
#'    `episode_id` of row *i* is *i - 1*); its column types, class and
#'    attributes are kept. An exposure column whose name is already taken
#'    gets the suffix `_<experiment>_dup`, as in the web app.
#' 7. With `keep = FALSE` the run directory is deleted after success; cache
#'    entries stay. After an error it is kept and its path printed.
#'
#' @param cohort A data frame with one row per residential episode, or the
#'   path of a CSV or Parquet file.
#' @param variables Catalog variable keys, for example
#'   `c("ndi", "walkability")` (see [ss_catalog()]). Keys of `custom`
#'   datasets are added automatically.
#' @param data_dir The data directory (default [ss_data_dir()]).
#' @param buffer_m Residential buffer radius in metres (whole number).
#' @param raster_res_m Raster resolution in metres used by areal C3 steps.
#' @param custom A custom dataset from [ss_custom_polygon()] or
#'   [ss_custom_raster()], or a list of them.
#' @param cols Your column names for the required columns, for example
#'   `c(pid = "patient_id", startDate = "start", endDate = "end",
#'   longitude = "lon", latitude = "lat")`.
#' @param cache `TRUE` (default) keeps C3 weights in the user cache
#'   directory (`tools::R_user_dir("spacescans", "cache")`, see
#'   [ss_cache_info()]), so the same cohort and buffer are not recomputed;
#'   `FALSE` disables it; a path uses that directory as the cache root.
#' @param threads,memory_limit DuckDB worker threads and memory cap (for
#'   example `"8GB"`) for the pipeline steps.
#' @param on_error `"stop"` stops at the first failing data source;
#'   `"continue"` runs the others and returns what succeeded (with a
#'   warning naming the failures).
#' @param progress Show progress (default: in interactive sessions).
#' @param keep Keep the run directory after success.
#' @param run_dir Run directory to use (created with mode 0700; must be
#'   empty). Default: a fresh directory under [tempdir()].
#' @param wait `TRUE` (default) waits for the result; `FALSE` returns a job
#'   at once (see [ss_job_status()]). Only a job with an explicit `run_dir`
#'   survives the R session.
#' @return The cohort data frame with the exposure columns added, and
#'   attributes `features` (the feature dictionary of the new columns),
#'   `coverage` (per variable: episodes, episodes with a value, percentage),
#'   `provenance` (versions, code fingerprint, data receipts, cache keys,
#'   threads, platform; no cohort values) and `spacescans` (steps, failed
#'   and skipped sources, warnings). With `wait = FALSE`, a job.
#' @examples
#' \dontrun{
#' ss_data_dir("~/ss-data")
#' res <- ss_link(cohort, c("ndi", "walkability"))
#' attr(res, "coverage")
#' attr(res, "features")
#' }
#' @export
ss_link <- function(cohort, variables, data_dir = ss_data_dir(), buffer_m = 270L,
                    raster_res_m = 25L, custom = NULL, cols = NULL, cache = TRUE,
                    threads = NULL, memory_limit = NULL, on_error = c("stop", "continue"),
                    progress = interactive(), keep = FALSE, run_dir = NULL, wait = TRUE) {
  on_error <- match.arg(on_error)
  buffer_m <- .ss_whole(buffer_m, "buffer_m", min = 1)   # the bridge and link need >= 1
  raster_res_m <- .ss_whole(raster_res_m, "raster_res_m", min = 1)
  threads <- .ss_threads_arg(threads)
  memory_limit <- .ss_memory_arg(memory_limit)
  custom_map <- .ss_custom_map(custom)
  variables <- .ss_link_variables(variables, custom_map)
  user <- .ss_cohort_input(cohort, cols)
  prepared <- .ss_prepare_cohort(user, cols)

  data_dir <- .ss_dir_value(data_dir, "data_dir")
  .ss_hello()
  cache_dir <- .ss_cache_arg(cache)
  catalog_vars <- setdiff(variables, names(custom_map))
  .ss_preflight(catalog_vars, data_dir)

  own_dir <- is.null(run_dir)
  rd <- if (own_dir) .ss_new_run_dir() else .ss_user_run_dir(run_dir)
  .ss_warn_cloud(c(data_dir = data_dir, run_dir = rd, cache_dir = cache_dir))
  cohort_path <- tryCatch(.ss_write_cohort(prepared$df, rd), error = function(e) {
    if (own_dir) unlink(rd, recursive = TRUE)
    stop(e)
  })
  args <- list(
    cohort = cohort_path, variables = I(variables), custom = .ss_named(custom_map),
    data_dir = data_dir, run_dir = rd, buffer_m = buffer_m, raster_res_m = raster_res_m,
    cache_dir = cache_dir, executor = "subprocess", threads = threads,
    memory_limit = memory_limit, on_error = on_error, reserved_names = I(names(user))
  )
  meta <- list(variables = I(variables), n = prepared$n, buffer_m = buffer_m,
               raster_res_m = raster_res_m, cols = as.list(prepared$map), keep = isTRUE(keep))
  job <- tryCatch(
    .ss_job_start("link", args, rd, persistent = !own_dir && !isTRUE(wait),
                  env = list(SPACESCANS_DATA_DIR = data_dir), meta = meta),
    error = function(e) {
      if (own_dir) unlink(rd, recursive = TRUE)
      stop(e)
    })
  job$cohort <- user
  job$n <- prepared$n
  job$meta_finish <- .ss_link_finish
  job$remove_on_success <- !isTRUE(keep)
  if (!isTRUE(wait)) return(job)

  state <- .ss_job_wait(job, progress = progress, cancel_on_interrupt = TRUE)
  .ss_job_finish(job, interrupted = identical(state, "interrupted_by_user"))
}

.ss_link_variables <- function(variables, custom_map) {
  if (missing(variables) || is.null(variables)) variables <- character()
  if (!is.character(variables) || anyNA(variables) || any(!nzchar(variables))) {
    .ss_abort("config", "`variables` must be a character vector of variable keys, for example c(\"ndi\").")
  }
  variables <- unique(c(variables, setdiff(names(custom_map), variables)))
  if (!length(variables)) .ss_abort("config", "Give at least one variable (see ss_catalog()) or a custom dataset.")
  variables
}

.ss_dir_value <- function(path, name) {
  if (is.null(path)) .ss_abort("config", sprintf("`%s` is required.", name))
  if (!is.character(path) || length(path) != 1L || is.na(path) || !nzchar(path)) {
    .ss_abort("config", sprintf("`%s` must be a single path.", name))
  }
  .ss_absolute(path)
}

# A run directory the user named: created 0700; it must be empty so two runs
# never mix their files.
.ss_user_run_dir <- function(run_dir) {
  path <- .ss_dir_value(run_dir, "run_dir")
  if (dir.exists(path)) {
    if (length(list.files(path, all.files = TRUE, no.. = TRUE))) {
      .ss_abort("config", sprintf("run_dir %s is not empty; give a new or empty directory.", path))
    }
  } else {
    dir.create(path, recursive = TRUE, mode = "0700")
  }
  Sys.chmod(path, "0700", use_umask = FALSE)
  normalizePath(path)
}

.ss_warn_cloud <- function(paths) {
  for (nm in names(paths)) {
    p <- paths[[nm]]
    if (!is.null(p) && .ss_is_cloud_synced(p)) {
      warning(sprintf(paste0(
        "spacescans: %s %s is in a cloud-synced folder (iCloud Drive, OneDrive, Dropbox, Box); ",
        "keep data, caches and run files on a local disk."), nm, p), call. = FALSE)
    }
  }
  invisible()
}

# Data and extras preflight: fail before any step starts, with the exact R
# call that fixes it.
.ss_preflight <- function(variables, data_dir) {
  if (!length(variables)) return(invisible(NULL))
  .ss_check_extras(variables)
  st <- .ss_bridge("data-status", list(variables = I(variables), data_dir = data_dir, deep = FALSE),
                   env = list(SPACESCANS_DATA_DIR = data_dir))
  if (isTRUE(st$ready)) return(invisible(st))
  vars <- Filter(function(v) !isTRUE(v$usable) && (v$variable %||% "") %in% variables, st$variables %||% list())
  if (!length(vars)) return(invisible(st))
  lines <- vapply(vars, function(v) {
    ds <- .ss_chr(v$not_usable)
    sprintf("  %s: %s%s", v$variable, v$status,
            if (length(ds)) sprintf(" (needs %s)", paste(ds, collapse = ", ")) else "")
  }, character(1))
  names_bad <- vapply(vars, function(v) v$variable, character(1))
  datasets <- unique(unlist(lapply(vars, function(v) .ss_chr(v$not_usable))))
  corrupt <- vapply(vars, function(v) identical(v$status, "corrupt"), logical(1))
  kind <- if (all(corrupt)) "data_corrupt" else "data_missing"
  manual <- Filter(function(d) identical(d$status, "action_required") && d$dataset %in% datasets,
                   st$datasets %||% list())
  msg <- sprintf("The data for %d of %d variable(s) is not ready in %s:\n%s",
                 length(vars), length(variables), data_dir, paste(lines, collapse = "\n"))
  if (length(manual)) {
    msg <- paste0(msg, "\nSome data must be downloaded by hand; ss_data_status() shows the instructions.")
  }
  .ss_abort(kind, msg, hint = .ss_r_hint(kind, list(variables = names_bad, datasets = datasets), NULL),
            details = list(variables = names_bad, datasets = datasets, status = st))
}

# Variables that need a Python extra this install lacks. The bridge's catalog
# rows carry `missing_extras` (the extras a variable's patterns and readers
# need that this Python install cannot load); a catalog that lists the extras
# each variable needs (`extras`) is checked against hello's extras instead.
.ss_check_extras <- function(variables) {
  if (!"catalog" %in% unlist(.ss_hello()$commands)) return(invisible())
  cat <- tryCatch(.ss_catalog_value(), error = function(e) NULL)
  if (is.null(cat)) return(invisible())
  rows <- .ss_catalog_rows(cat)
  split <- function(e) {
    e <- trimws(strsplit(as.character(e %||% ""), ",", fixed = TRUE)[[1]])
    e[nzchar(e) & !is.na(e)]
  }
  if (!is.null(rows$missing_extras)) {
    lacking <- lapply(rows$missing_extras, split)
  } else if (!is.null(rows$extras)) {
    caps <- .ss_hello()
    have <- names(Filter(function(e) isTRUE(e$available), caps$extras %||% list()))
    lacking <- lapply(rows$extras, function(e) setdiff(split(e), have))
  } else {
    return(invisible())
  }
  selected <- rows$variable %in% variables
  missing <- unique(unlist(lacking[selected]))
  if (length(missing)) {
    who <- rows$variable[selected & lengths(lacking) > 0L]
    .ss_abort("missing_extra", sprintf("%s need(s) the Python extra(s) %s, which are not installed.",
                                       paste(who, collapse = ", "), paste(missing, collapse = ", ")),
              hint = .ss_r_hint("missing_extra", list(extra = missing), NULL),
              details = list(extra = missing, variables = who))
  }
  invisible()
}

# Build the result of a finished link: check episode ids, join onto the
# user's data frame, attach features / coverage / provenance.
.ss_link_finish <- function(job, value) {
  info <- attr(value, "spacescans")
  path <- value$exposures
  if (!is.character(path) || !file.exists(path)) {
    .ss_abort("internal", "The link result names no exposures file.", run_dir = job$run_dir)
  }
  ex <- as.data.frame(nanoparquet::read_parquet(path), stringsAsFactors = FALSE)
  n <- job$n %||% job$meta$n
  ids <- ex$episode_id
  if (is.null(ids) || length(ids) != n || anyNA(ids) || any(ids != seq_len(n) - 1)) {
    .ss_abort("internal", sprintf(paste0(
      "The exposures table does not match the cohort: expected episode_id 0..%d in input order ",
      "(%d rows), got %d rows."), n - 1L, n, nrow(ex)), run_dir = job$run_dir)
  }
  ex$episode_id <- NULL
  out <- job$cohort
  if (is.null(out)) {
    out <- data.frame(episode_id = seq_len(n) - 1L)
  }
  taken <- names(out)
  for (col in names(ex)) {
    name <- col
    if (name %in% taken) {
      # The bridge renames clashes with reserved_names; never overwrite user data.
      i <- 1L
      while (name %in% taken) {
        name <- paste0(col, "_dup", if (i > 1L) i)
        i <- i + 1L
      }
      warning(sprintf("spacescans: exposure column %s renamed to %s (name already in the cohort).", col, name),
              call. = FALSE)
    }
    out[[name]] <- ex[[col]]
    taken <- c(taken, name)
  }
  features <- if (is.character(value$features) && file.exists(value$features)) {
    utils::read.csv(value$features, stringsAsFactors = FALSE, check.names = FALSE,
                    colClasses = "character", na.strings = character())
  } else {
    .ss_records_df(value$features)
  }
  coverage <- .ss_coverage_df(value$coverage)
  provenance <- if (is.character(value$provenance) && file.exists(value$provenance)) {
    jsonlite::read_json(value$provenance, simplifyVector = TRUE)
  } else {
    value$provenance
  }
  steps <- .ss_records_df(value$steps, first = c("name", "status", "seconds", "rc", "maxrss_bytes", "cache_key"))
  failed <- .ss_chr(value$failed)
  skipped <- .ss_chr(value$skipped)
  attr(out, "features") <- features
  attr(out, "coverage") <- coverage
  attr(out, "provenance") <- provenance
  attr(out, "spacescans") <- list(
    rows = value$rows, episodes = value$episodes, patients = value$patients, steps = steps,
    failed = failed, skipped = skipped, warnings = value$warnings %||% list(), bridge = info,
    run_dir = if (isTRUE(job$remove_on_success)) NULL else job$run_dir
  )
  .ss_link_warnings(coverage, value, failed, skipped)
  out
}

.ss_low_match_pct <- 90

.ss_link_warnings <- function(coverage, value, failed, skipped) {
  if (nrow(coverage) && all(c("variable", "pct") %in% names(coverage))) {
    low <- !is.na(coverage$pct) & coverage$pct < .ss_low_match_pct
    for (i in which(low)) {
      warning(sprintf("spacescans: only %.1f%% of episodes got a %s value (below %d%%).",
                      coverage$pct[i], coverage$variable[i], .ss_low_match_pct), call. = FALSE)
    }
  }
  for (w in value$warnings %||% list()) {
    if (identical(w$code, "low_match")) next
    msg <- w$message %||% w$text
    if (!is.null(msg)) warning(paste0("spacescans: ", msg), call. = FALSE)
  }
  if (length(failed)) {
    warning(sprintf("spacescans: these sources failed and have no columns: %s%s.",
                    paste(failed, collapse = ", "),
                    if (length(skipped)) sprintf("; skipped: %s", paste(skipped, collapse = ", ")) else ""),
            call. = FALSE)
  }
  invisible()
}

# Coverage rows as a data frame: variable, episodes, with_value, pct, ...
.ss_coverage_df <- function(x) {
  if (is.null(x) || !length(x)) return(data.frame(variable = character(), episodes = integer(),
                                                  with_value = integer(), pct = numeric()))
  if (is.list(x) && !is.null(x$variables) && is.list(x$variables)) x <- x$variables
  df <- .ss_records_df(x)
  if (".key" %in% names(df)) {
    names(df)[names(df) == ".key"] <- "variable"
  }
  if (!"pct" %in% names(df) && "coverage_pct" %in% names(df)) df$pct <- df$coverage_pct
  for (col in intersect(c("pct", "coverage_pct"), names(df))) df[[col]] <- as.numeric(df[[col]])
  first <- intersect(c("variable", "episodes", "with_value", "pct"), names(df))
  df[, c(first, setdiff(names(df), first)), drop = FALSE]
}

# Reattaching a link job: the cohort, when given, must equal the one in the
# run directory.
.ss_link_attach <- function(job, cohort, cols) {
  job$n <- job$meta$n
  job$meta_finish <- .ss_link_finish
  job$remove_on_success <- !isTRUE(job$meta$keep)
  if (is.null(cohort)) {
    job$cohort <- NULL
    return(invisible(job))
  }
  cols <- cols %||% unlist(job$meta$cols)
  user <- .ss_cohort_input(cohort, cols)
  prepared <- .ss_prepare_cohort(user, cols)
  path <- file.path(job$run_dir, "cohort.parquet")
  if (!file.exists(path)) .ss_abort("config", "The run directory has no cohort.parquet to compare the cohort with.")
  stored <- as.data.frame(nanoparquet::read_parquet(path), stringsAsFactors = FALSE)
  eq <- function(a, b) {
    a <- as.numeric(a)
    b <- as.numeric(b)
    length(a) == length(b) && identical(is.na(a), is.na(b)) && all(a[!is.na(a)] == b[!is.na(b)])
  }
  same <- nrow(stored) == nrow(prepared$df) &&
    identical(as.character(stored$pid), prepared$df$pid) &&
    eq(stored$longitude, prepared$df$longitude) && eq(stored$latitude, prepared$df$latitude) &&
    eq(stored$startDate, prepared$df$startDate) && eq(stored$endDate, prepared$df$endDate)
  if (!same) {
    .ss_abort("cohort", "This cohort is not the one the job was started with (rows or values differ).")
  }
  job$cohort <- user
  invisible(job)
}

#' Estimate coverage before linking
#'
#' For each variable, how many cohort episodes fall inside its years and
#' region, from the catalog's coverage years and a bounding box for the
#' contiguous US. It runs no pipeline step and needs no data. Like
#' [ss_link()], only the five required columns are handed to Python.
#'
#' @inheritParams ss_link
#' @return A data frame with one row per variable: `variable`,
#'   `coverage_pct` (or `pct`), episodes in the time window, in the region,
#'   covered, and notes.
#' @examples
#' \dontrun{
#' ss_coverage(cohort, c("ndi", "temis"))
#' }
#' @export
ss_coverage <- function(cohort, variables, cols = NULL, custom = NULL) {
  custom_map <- .ss_custom_map(custom)
  variables <- .ss_link_variables(variables, custom_map)
  prepared <- .ss_prepare_cohort(.ss_cohort_input(cohort, cols), cols)
  rd <- .ss_new_run_dir()
  on.exit(unlink(rd, recursive = TRUE), add = TRUE)
  path <- .ss_write_cohort(prepared$df, rd)
  value <- .ss_bridge("coverage", list(cohort = path, variables = I(variables),
                                       custom = .ss_named(custom_map)))
  df <- .ss_coverage_df(value)
  attr(df, "episodes") <- value$row_count %||% prepared$n
  df
}
