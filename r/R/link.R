#' Link a cohort to catalog variables
#'
#' Computes exposures for every location episode of a cohort and returns
#' them per participant (the default) or per location episode.
#'
#' A cohort is time-referenced geolocation data: one row per location
#' episode, that is a participant, a location (longitude and latitude) and
#' the first and last day spent there. A participant who moved has one row
#' per location.
#'
#' **Output level.** With `output_level = "participant"` (the default) the
#' result has one row per participant: for each variable, the mean of the
#' participant's episode values weighted by the days that contribute to
#' them. For participant *i* and variable *v* it is
#' \deqn{\bar{x}_{iv} = \frac{\sum_{e \in E_i} \sum_{t \in T_{ev}} d_{et} z_{etv}}{\sum_{e \in E_i} \sum_{t \in T_{ev}} d_{et}}}{x_iv = sum_e sum_t d_et z_etv / sum_e sum_t d_et}
#' where \eqn{E_i} are the participant's episodes, \eqn{z_{etv}}{z_etv} is the
#' spatial estimate (buffer-weighted mean, or distance to the nearest
#' feature) for episode \eqn{e} in source period \eqn{t}, \eqn{T_{ev}}{T_ev}
#' are the periods that overlap the episode and have an estimate, and
#' \eqn{d_{et}}{d_et} is the number of days of the overlap (first and last day
#' included). A time-invariant source counts every day of the episode. The
#' contributing days are counted per variable, and a participant with no
#' contributing day gets a missing value. With `output_level = "episode"`
#' the result is the cohort itself with the exposure columns added, one row
#' per location episode, as in spacescans 0.2.
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
#'    (mode 0700, under `tools::R_user_dir("spacescans", "cache")/runs` unless
#'    you give `run_dir`). Your other columns never leave R.
#' 4. The data directory is checked ([ss_data_status()]): when data is
#'    missing the error says which [ss_fetch()] call to run, and when a
#'    Python extra is missing, which [ss_install()] call. `ss_link()` never
#'    downloads anything.
#' 5. The Python side runs the pipeline steps, showing progress. Ctrl-C asks
#'    it to stop, waits up to 10 seconds (option
#'    `spacescans.interrupt_grace`) and then kills its process tree.
#'    Completed C3 steps stay in the cache.
#' 6. Participant level: the table has `pid` (with the type of your id
#'    column) and one column per exposure, one row per participant in the
#'    order they first appear in the cohort, including participants without
#'    any value. Episode level: the exposures are joined back onto your data
#'    frame by row (the `episode_id` of row *i* is *i - 1*); its column
#'    types, class and attributes are kept, and an exposure column whose
#'    name is already taken gets the suffix `_<experiment>_dup`, as in the
#'    web app.
#' 7. With `keep = FALSE` the run directory is deleted after success; cache
#'    entries stay. After an error it is kept and its path printed. A run
#'    directory the package chose itself is recorded as owned by this R
#'    session; should the session die, the next package load, [ss_doctor()]
#'    or [ss_cache_clear()] (in any session of yours) deletes it.
#'
#' @param cohort A data frame with one row per location episode, or the
#'   path of a CSV or Parquet file.
#' @param variables Catalog variable keys, for example
#'   `c("ndi", "walkability")` (see [ss_catalog()]). The variables of
#'   `custom` datasets are added automatically, so `character()` links the
#'   custom datasets alone.
#' @param output_level `"participant"` (default): one row per participant,
#'   each variable averaged over the participant's location episodes
#'   weighted by contributing days (see Details). `"episode"`: one row per
#'   location episode, your data frame with the exposure columns added.
#' @param data_dir The data directory (default [ss_data_dir()]).
#' @param buffer_m Buffer radius around each location in meters (whole
#'   number).
#' @param raster_res_m Raster resolution in meters used by areal C3 steps.
#' @param custom Your own datasets: a dataset registered with
#'   [ss_register_dataset()] (its name, id or variable key, or the record),
#'   a definition from [ss_custom_polygon()] or [ss_custom_raster()], or a
#'   list (or character vector) of them, linked in that order.
#' @param cols Your column names for the required columns, for example
#'   `c(pid = "participant_id", startDate = "start", endDate = "end",
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
#'   empty). Default: a fresh directory under
#'   `tools::R_user_dir("spacescans", "cache")/runs`, owned by this session.
#' @param wait `TRUE` (default) waits for the result; `FALSE` returns a job
#'   at once (see [ss_job_status()]). Only a job with an explicit `run_dir`
#'   survives the R session.
#' @return Participant level: a data frame with `pid` and one column per
#'   exposure, one row per participant (a data.table when `cohort` is one).
#'   Episode level: the cohort data frame with the exposure columns added.
#'   Both carry the attributes `output_level`, `features` (the feature
#'   dictionary of the exposure columns), `coverage` (the linkage rates: per
#'   variable, the rows of the result, the rows with a value and their
#'   percentage; participants or episodes, by level), `provenance`
#'   (versions, code fingerprint, data receipts, cache keys, threads,
#'   platform; no cohort values) and `spacescans` (steps, failed and skipped
#'   sources, warnings). With `wait = FALSE`, a job.
#' @seealso [ss_coverage()] estimates, per location episode, how much of the
#'   cohort a variable covers before you link.
#' @examples
#' \dontrun{
#' ss_data_dir("~/ss-data")
#' res <- ss_link(cohort, c("ndi", "walkability"))      # one row per participant
#' attr(res, "coverage")                                # linkage rates
#' attr(res, "features")
#' ep <- ss_link(cohort, "ndi", output_level = "episode")  # one row per location episode
#' }
#' @export
ss_link <- function(cohort, variables, output_level = c("participant", "episode"),
                    data_dir = ss_data_dir(), buffer_m = 270L,
                    raster_res_m = 25L, custom = NULL, cols = NULL, cache = TRUE,
                    threads = NULL, memory_limit = NULL, on_error = c("stop", "continue"),
                    progress = interactive(), keep = FALSE, run_dir = NULL, wait = TRUE) {
  output_level <- .ss_choice(output_level, .ss_output_levels, "output_level")
  on_error <- .ss_choice(on_error, c("stop", "continue"), "on_error")
  buffer_m <- .ss_whole(buffer_m, "buffer_m", min = 1)   # the bridge and link need >= 1
  raster_res_m <- .ss_whole(raster_res_m, "raster_res_m", min = 1)
  threads <- .ss_threads_arg(threads)
  memory_limit <- .ss_memory_arg(memory_limit)
  items <- .ss_custom_items(custom)
  wanted <- .ss_link_variables(variables, list(), custom = length(items) > 0L)
  user <- .ss_cohort_input(cohort, cols)
  prepared <- .ss_prepare_cohort(user, cols)

  data_dir <- .ss_dir_value(data_dir, "data_dir")
  .ss_hello()
  cache_dir <- .ss_cache_arg(cache)
  # Registered datasets become definitions here (datasets-get), so the link
  # treats them like any other custom definition.
  custom_map <- .ss_custom_resolve(items)
  variables <- .ss_link_variables(wanted, custom_map)
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
    output_level = output_level,
    data_dir = data_dir, run_dir = rd, buffer_m = buffer_m, raster_res_m = raster_res_m,
    cache_dir = cache_dir, executor = "subprocess", threads = threads,
    memory_limit = memory_limit, on_error = on_error,
    # Episode level adds columns to the user's data frame, so its names are taken;
    # a participant table holds only pid and the variables.
    reserved_names = I(if (identical(output_level, "episode")) names(user) else character())
  )
  args <- args[!vapply(args, is.null, logical(1))]
  # job.json holds counts and settings only, never cohort values.
  meta <- list(variables = I(variables),
               output_level = output_level, n = prepared$n, participants = prepared$n_patients,
               buffer_m = buffer_m, raster_res_m = raster_res_m, cols = as.list(prepared$map),
               keep = isTRUE(keep))
  meta <- meta[!vapply(meta, is.null, logical(1))]
  job <- tryCatch(
    .ss_job_start("link", args, rd, persistent = !own_dir && !isTRUE(wait),
                  env = list(SPACESCANS_DATA_DIR = data_dir), meta = meta),
    error = function(e) {
      if (own_dir) unlink(rd, recursive = TRUE)
      stop(e)
    })
  .ss_link_remember_cohort(job, user, prepared)
  job$meta_finish <- .ss_link_finish
  job$remove_on_success <- !isTRUE(keep)
  if (!isTRUE(wait)) return(job)

  state <- .ss_job_wait(job, progress = progress, cancel_on_interrupt = TRUE)
  .ss_job_finish(job, interrupted = identical(state, "interrupted_by_user"))
}

# The output levels ss_link() offers; the first is the default.
.ss_output_levels <- c("participant", "episode")

# What the finisher needs from the cohort, kept in the job (in memory only,
# never in job.json): the user's data frame (episode level joins onto it),
# the episode count, and the participants in order of first appearance as
# text (the order the bridge writes them) with their original id values.
.ss_link_remember_cohort <- function(job, user, prepared) {
  job$cohort <- user
  job$n <- prepared$n
  first <- !duplicated(prepared$df$pid)
  job$pids <- prepared$df$pid[first]
  job$pid_values <- user[[prepared$map[["pid"]]]][first]
  invisible(job)
}

# The variables to link: yours, then the custom datasets' keys (in the order
# given). `custom`: custom datasets are coming that R cannot list yet
# (registered ones, looked up later), so no variable is needed now.
.ss_link_variables <- function(variables, custom_map, custom = FALSE) {
  if (missing(variables) || is.null(variables)) variables <- character()
  if (!is.character(variables) || anyNA(variables) || any(!nzchar(variables))) {
    .ss_abort("config", "`variables` must be a character vector of variable keys, for example c(\"ndi\").")
  }
  variables <- unique(c(variables, setdiff(names(custom_map), variables)))
  if (!length(variables) && !isTRUE(custom)) {
    .ss_abort("config", "Give at least one variable (see ss_catalog()) or a custom dataset.")
  }
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
  needed <- Filter(function(d) d$dataset %in% datasets, st$datasets %||% list())
  # A dataset spacescans downloads once its provider's terms are accepted is not a manual step.
  terms_only <- Filter(function(d) isTRUE(d$scripted) && !is.null(d$terms$id), needed)
  accept <- unique(.ss_chr(lapply(terms_only, function(d) d$terms$id)))
  manual <- Filter(function(d) identical(d$status, "action_required") && !isTRUE(d$scripted), needed)
  msg <- sprintf("The data for %d of %d variable(s) is not ready in %s:\n%s",
                 length(vars), length(variables), data_dir, paste(lines, collapse = "\n"))
  if (length(manual)) {
    msg <- paste0(msg, "\nSome data must be downloaded by hand; ss_data_status() shows the instructions.")
  }
  if (length(accept)) {
    msg <- paste0(msg, sprintf(paste0(
      "\nThe provider's terms must be accepted first (%s); ss_data_terms(%s) shows them."),
      paste(accept, collapse = ", "), .ss_deparse_chr(terms_only[[1]]$dataset)))
  }
  .ss_abort(kind, msg, hint = .ss_r_hint(kind, list(variables = names_bad, datasets = datasets,
                                                    accept_terms = accept), NULL),
            details = list(variables = names_bad, datasets = datasets, accept_terms = accept,
                           status = st))
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

# Build the result of a finished link: check the table against the cohort,
# then (episode level) join it onto the user's data frame or (participant
# level) return one row per participant; attach features / coverage /
# provenance / output_level.
.ss_link_finish <- function(job, value) {
  info <- attr(value, "spacescans")
  path <- value$exposures
  if (!is.character(path) || !file.exists(path)) {
    .ss_abort("internal", "The link result names no exposures file.", run_dir = job$run_dir)
  }
  ex <- as.data.frame(nanoparquet::read_parquet(path), stringsAsFactors = FALSE)
  level <- .ss_link_level(job, value, ex)
  built <- if (identical(level, "participant")) .ss_link_participants(job, ex) else .ss_link_episodes(job, ex)
  out <- built$out
  dt <- built$dt
  features <- if (is.character(value$features) && file.exists(value$features)) {
    utils::read.csv(value$features, stringsAsFactors = FALSE, check.names = FALSE,
                    colClasses = "character", na.strings = character())
  } else {
    .ss_records_df(value$features)
  }
  coverage <- .ss_coverage_df(value$coverage, level = level)
  provenance <- if (is.character(value$provenance) && file.exists(value$provenance)) {
    jsonlite::read_json(value$provenance, simplifyVector = TRUE)
  } else {
    value$provenance
  }
  steps <- .ss_records_df(value$steps, first = c("name", "status", "seconds", "rc", "maxrss_bytes", "cache_key"))
  failed <- .ss_chr(value$failed)
  skipped <- .ss_chr(value$skipped)
  info_attr <- list(
    output_level = level, rows = value$rows, episodes = value$episodes,
    participants = value$participants %||% value$patients, patients = value$patients, steps = steps,
    failed = failed, skipped = skipped, warnings = value$warnings %||% list(), bridge = info,
    run_dir = if (isTRUE(job$remove_on_success)) NULL else job$run_dir
  )
  attrs <- list(features = features, coverage = coverage, provenance = provenance,
                output_level = level, spacescans = info_attr)
  for (nm in names(attrs)) {
    if (dt) .ss_dt("setattr")(out, nm, attrs[[nm]]) else attr(out, nm) <- attrs[[nm]]
  }
  # R's cohort check already warned about blank dates when it had the cohort.
  .ss_link_warnings(coverage, value, failed, skipped,
                    skip = c("low_match", if (!is.null(job$cohort)) "cohort_date_missing"),
                    level = level)
  out
}

# The level of a finished link: the one requested (job.json of a job started
# before output levels existed holds none: episode), checked against the one
# the bridge reports (or, from a bridge that reports none, the table's shape).
.ss_link_level <- function(job, value, ex) {
  wanted <- job$meta$output_level %||% "episode"
  got <- value$level %||% value$output_level
  if (is.null(got)) {
    got <- if ("episode_id" %in% names(ex)) "episode" else if ("pid" %in% names(ex)) "participant" else NA_character_
  }
  if (identical(got, wanted)) return(wanted)
  version <- attr(value, "spacescans")$version %||% "unknown"
  if (identical(wanted, "participant") && identical(got, "episode")) {
    .ss_abort("protocol", sprintf(paste0(
      "Participant-level output was requested, but the spacescans Python package (version %s) ",
      "returned one row per location episode; it needs spacescans 0.3.0 or later."), version),
      hint = "ss_install() installs the Python package that matches this R package; ss_link(output_level = \"episode\") works with the installed one.",
      run_dir = job$run_dir)
  }
  .ss_abort("internal", sprintf("The link returned %s-level output, but %s-level output was requested.",
                                format(got), wanted), run_dir = job$run_dir)
}

# Episode level: check the episode ids, then join the exposures onto the
# user's data frame by row.
.ss_link_episodes <- function(job, ex) {
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
  # A data.table comes back as a data.table that := can extend in place: a
  # deep copy (the input is never modified), over-allocated, attributes set
  # by reference.
  dt <- inherits(out, "data.table") && isNamespaceLoaded("data.table")
  if (dt) out <- .ss_dt("copy")(out)
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
    if (dt) .ss_dt("set")(out, j = name, value = ex[[col]]) else out[[name]] <- ex[[col]]
    taken <- c(taken, name)
  }
  list(out = out, dt = dt)
}

# Participant level: one row per participant, in order of first appearance,
# `pid` then the variables. The ids must be exactly the cohort's; `pid` gets
# back the type the user's id column had (text when the job was attached
# without the cohort). A data.table cohort gives a data.table, a tibble a
# tibble (when tibble is loaded), anything else a data frame.
.ss_link_participants <- function(job, ex) {
  if (!"pid" %in% names(ex) || "episode_id" %in% names(ex)) {
    .ss_abort("internal", "The participant-level exposures table must have a pid column and no episode_id.",
              run_dir = job$run_dir)
  }
  pid <- .ss_pid_chr(ex$pid)
  want <- job$pids
  n_want <- if (!is.null(want)) length(want) else job$meta$participants
  same <- if (!is.null(want)) identical(pid, as.character(want)) else
    (is.null(n_want) || length(pid) == n_want) && !anyNA(pid) && !anyDuplicated(pid)
  if (!same) {
    .ss_abort("internal", sprintf(paste0(
      "The participant-level exposures table does not match the cohort: expected %s participants ",
      "in order of first appearance, got %d rows."), format(n_want %||% "?"), length(pid)),
      run_dir = job$run_dir)
  }
  ex$pid <- if (!is.null(want) && !is.null(job$pid_values)) job$pid_values else pid
  out <- ex[, c("pid", setdiff(names(ex), "pid")), drop = FALSE]
  rownames(out) <- NULL
  cohort <- job$cohort
  dt <- inherits(cohort, "data.table") && isNamespaceLoaded("data.table")
  if (dt) {
    out <- .ss_dt("as.data.table")(out)
  } else if (inherits(cohort, "tbl_df") && isNamespaceLoaded("tibble")) {
    out <- getExportedValue("tibble", "as_tibble")(out)
  }
  list(out = out, dt = dt)
}

# A data.table function, looked up only when a data.table was passed in (so
# its namespace is loaded); spacescans does not depend on data.table.
.ss_dt <- function(name) getExportedValue("data.table", name)

.ss_low_match_pct <- 90

# The link's warnings, each once: R's own coverage warning stands in for the
# bridge's low_match records; `skip` lists other codes R reported itself.
.ss_link_warnings <- function(coverage, value, failed, skipped, skip = "low_match", level = "episode") {
  if (nrow(coverage) && all(c("variable", "pct") %in% names(coverage))) {
    low <- !is.na(coverage$pct) & coverage$pct < .ss_low_match_pct
    for (i in which(low)) {
      warning(sprintf("spacescans: only %.1f%% of %ss got a %s value (below %d%%).",
                      coverage$pct[i], level, coverage$variable[i], .ss_low_match_pct), call. = FALSE)
    }
  }
  shown <- character()
  for (w in value$warnings %||% list()) {
    if (isTRUE(w$code %in% skip)) next
    if (identical(w$code, "failed") && length(failed)) next   # summarised below
    msg <- w$message %||% w$text
    if (is.null(msg) || msg %in% shown) next
    shown <- c(shown, msg)
    warning(paste0("spacescans: ", msg), call. = FALSE)
  }
  if (length(failed)) {
    warning(sprintf("spacescans: these sources failed and have no columns: %s%s.",
                    paste(failed, collapse = ", "),
                    if (length(skipped)) sprintf("; skipped: %s", paste(skipped, collapse = ", ")) else ""),
            call. = FALSE)
  }
  invisible()
}

# Coverage (linkage-rate) rows as a data frame: variable, then the counts of
# the output level (episodes or participants, with_value) and pct, the share
# of the output table's rows with a value. At participant level a bridge that
# reports participants as `patients` / `patients_with_value` gets them under
# the participant names.
.ss_coverage_df <- function(x, level = "episode") {
  unit <- if (identical(level, "participant")) "participants" else "episodes"
  if (is.null(x) || !length(x)) {
    df <- data.frame(variable = character(), n = integer(), with_value = integer(), pct = numeric())
    names(df)[2] <- unit
    return(df)
  }
  if (is.list(x) && !is.null(x$variables) && is.list(x$variables)) x <- x$variables
  df <- .ss_records_df(x)
  if (".key" %in% names(df)) {
    names(df)[names(df) == ".key"] <- "variable"
  }
  if (!"pct" %in% names(df) && "coverage_pct" %in% names(df)) df$pct <- df$coverage_pct
  for (col in intersect(c("pct", "coverage_pct"), names(df))) df[[col]] <- as.numeric(df[[col]])
  if (identical(level, "participant") && !"participants" %in% names(df) && "patients" %in% names(df)) {
    df$participants <- df$patients
    if ("patients_with_value" %in% names(df)) {
      df$with_value <- df$patients_with_value
      df$pct <- ifelse(df$participants > 0, round(100 * df$with_value / df$participants, 2), 0)
    }
    df$patients <- df$patients_with_value <- NULL
  }
  first <- intersect(c("variable", unit, "with_value", "pct"), names(df))
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
    # The participant order to check a participant-level result against (ids
    # stay text: without the cohort R does not know their original type).
    path <- file.path(job$run_dir, "cohort.parquet")
    if (file.exists(path)) {
      pid <- .ss_pid_chr(nanoparquet::read_parquet(path)$pid)
      job$pids <- unique(pid)
    }
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
  .ss_link_remember_cohort(job, user, prepared)
  invisible(job)
}

#' Estimate coverage before linking
#'
#' For each variable, how many location episodes of the cohort fall inside
#' its years and region, from the catalog's coverage years and a bounding
#' box for the contiguous US. It runs no pipeline step and needs no data.
#' Like [ss_link()], only the five required columns are handed to Python.
#' The estimate counts location episodes whatever output level you link
#' at: it checks dates and locations, before any value exists.
#'
#' @inheritParams ss_link
#' @return A data frame with one row per variable: `variable`,
#'   `coverage_pct` (or `pct`), episodes in the time window, in the region,
#'   covered, and notes; attribute `episodes`, the number of location
#'   episodes.
#' @examples
#' \dontrun{
#' ss_coverage(cohort, c("ndi", "temis"))
#' }
#' @export
ss_coverage <- function(cohort, variables, cols = NULL, custom = NULL) {
  items <- .ss_custom_items(custom)
  wanted <- .ss_link_variables(variables, list(), custom = length(items) > 0L)
  prepared <- .ss_prepare_cohort(.ss_cohort_input(cohort, cols), cols)
  custom_map <- .ss_custom_resolve(items)
  variables <- .ss_link_variables(wanted, custom_map)
  rd <- .ss_new_run_dir()
  on.exit(unlink(rd, recursive = TRUE), add = TRUE)
  path <- .ss_write_cohort(prepared$df, rd)
  value <- .ss_bridge("coverage", list(cohort = path, variables = I(variables),
                                       custom = .ss_named(custom_map)))
  df <- .ss_coverage_df(value)
  attr(df, "episodes") <- value$row_count %||% value$episodes %||% prepared$n
  df
}
