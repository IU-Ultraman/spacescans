# Running `python -m spacescans.bridge`: request/result files, an interruptible
# processx poll loop, streamed events and crash detection. Short commands use
# .ss_bridge() (events through a pipe); long ones (link, data-fetch) run as
# jobs (job.R) whose events go to run_dir/events.jsonl. Both share the
# request, launch, event and outcome helpers below.

# Environment variables the child must not inherit: they would redirect the
# interpreter (PYTHON*) or the GDAL/PROJ data of the wheels, or load R's own
# libraries into Python (R sets DYLD_FALLBACK_LIBRARY_PATH on macOS).
.ss_drop_env <- c(
  "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONEXECUTABLE",
  "GDAL_DATA", "PROJ_DATA", "PROJ_LIB", "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_LIBRARY_PATH"
)

.ss_child_env <- function(extra = NULL) {
  env <- Sys.getenv()
  env <- env[setdiff(names(env), .ss_drop_env)]
  if (length(extra)) {
    extra <- extra[!vapply(extra, is.null, logical(1))]
    env[names(extra)] <- vapply(extra, as.character, character(1))
  }
  env
}

.ss_named <- function(x) {
  if (is.null(x) || length(x) == 0L) return(structure(list(), names = character(0)))
  if (is.null(names(x)) || any(!nzchar(names(x)))) {
    .ss_abort("internal", "bridge arguments must be a named list")
  }
  x
}

.ss_to_json <- function(x) {
  jsonlite::toJSON(x, auto_unbox = TRUE, null = "null", na = "null", digits = NA,
                   POSIXt = "ISO8601", Date = "ISO8601", force = TRUE)
}

# A fresh private (0700) directory. Without `parent` it goes under the per-user
# run root (tools::R_user_dir("spacescans", "cache")/runs, see runs.R) and gets
# an owner.json naming this R session, so a later sweep removes it should this
# session die before it can (phase-C decision 2). With `parent` (output
# directories under tempdir()) it is an ordinary temp directory.
.ss_new_run_dir <- function(parent = NULL) {
  if (is.null(parent)) {
    dir <- tryCatch(.ss_owned_run_dir(), error = function(e) NULL)
    if (!is.null(dir)) return(dir)
    if (!isTRUE(.ss_state$runs_root_warned)) {
      .ss_state$runs_root_warned <- TRUE
      warning(sprintf(paste0("spacescans: cannot create the run directory root %s; using tempdir() ",
                             "for this session (set R_USER_CACHE_DIR to a writable location)."),
                      .ss_runs_root()), call. = FALSE)
    }
    parent <- tempdir()
  }
  dir <- tempfile("ss-", tmpdir = parent)
  dir.create(dir, recursive = TRUE, mode = "0700")
  Sys.chmod(dir, "0700", use_umask = FALSE)
  normalizePath(dir)
}

# A new directory under the per-user run root with this session's owner record,
# or an error when the root, the directory or the record cannot be made (where
# dir.create() and file.rename() only warn), so the caller can fall back.
.ss_owned_run_dir <- function() {
  root <- .ss_runs_root()
  if (!dir.exists(root) && !suppressWarnings(dir.create(root, recursive = TRUE, mode = "0700"))) {
    stop("cannot create ", root)
  }
  root <- .ss_ensure_private_dir(root)
  dir <- tempfile("ss-", tmpdir = root)
  if (!suppressWarnings(dir.create(dir, mode = "0700")) || !dir.exists(dir)) stop("cannot create ", dir)
  Sys.chmod(dir, "0700", use_umask = FALSE)
  dir <- normalizePath(dir, mustWork = TRUE)
  ok <- tryCatch({
    suppressWarnings(.ss_write_owner(dir))
    file.exists(file.path(dir, "owner.json"))
  }, error = function(e) FALSE)
  if (!ok) {
    unlink(dir, recursive = TRUE)
    stop("cannot write the owner record in ", dir)
  }
  dir
}

.ss_tail <- function(path, n = 20L) {
  if (is.null(path) || !file.exists(path)) return(character())
  lines <- tryCatch(readLines(path, warn = FALSE, encoding = "UTF-8"), error = function(e) character())
  utils::tail(lines, n)
}

# Simplify a parsed JSON value: arrays of scalars of one type become vectors,
# objects stay named lists, null stays NULL.
.ss_simplify <- function(x) {
  if (!is.list(x)) return(x)
  if (is.null(names(x)) && length(x) > 0L &&
      all(vapply(x, function(v) is.atomic(v) && length(v) == 1L, logical(1)))) {
    types <- unique(vapply(x, function(v) class(v)[1], character(1)))
    if (length(types) == 1L || all(types %in% c("integer", "numeric"))) return(unlist(x))
  }
  for (i in seq_along(x)) {
    if (!is.null(x[[i]])) x[[i]] <- .ss_simplify(x[[i]])
  }
  x
}

.ss_signal_name <- function(sig) {
  known <- c(`1` = "SIGHUP", `2` = "SIGINT", `6` = "SIGABRT", `9` = "SIGKILL",
             `10` = "SIGBUS", `11` = "SIGSEGV", `15` = "SIGTERM")
  if (as.character(sig) %in% names(known)) known[[as.character(sig)]] else paste0("signal ", sig)
}

.ss_format_bytes <- function(x) {
  x <- suppressWarnings(as.numeric(x))
  if (!length(x) || is.na(x)) return("?")
  units <- c("B", "KB", "MB", "GB", "TB")
  i <- if (x <= 0) 1L else min(length(units), floor(log(x, 1000)) + 1L)
  if (i == 1L) sprintf("%.0f B", x) else sprintf("%.1f %s", x / 1000^(i - 1L), units[i])
}

# A minimal console progress display (no cli dependency). update() redraws one
# line; note() prints a line of its own.
.ss_progress_ui <- function(enabled) {
  state <- new.env(parent = emptyenv())
  state$shown <- FALSE
  state$width <- 0L
  draw <- function(text) {
    pad <- max(0L, state$width - nchar(text))
    cat("\r", text, strrep(" ", pad), sep = "", file = stderr())
    state$width <- nchar(text)
    state$shown <- TRUE
  }
  list(
    update = function(step, frac, overall = NULL, text = NULL) {
      if (!enabled) return(invisible())
      pct <- if (is.numeric(frac) && length(frac) == 1L && !is.na(frac)) round(100 * frac) else NA
      label <- if (is.character(step) && length(step) == 1L) step else "spacescans"
      line <- sprintf("%s %s", label, if (is.na(pct)) "" else sprintf("%3d%%", pct))
      if (is.numeric(overall) && length(overall) == 1L && !is.na(overall)) {
        line <- sprintf("[%3d%% overall] %s", round(100 * overall), line)
      }
      if (!is.null(text)) line <- paste(line, text)
      draw(line)
    },
    status = function(text) {
      if (!enabled) return(invisible())
      draw(text)
    },
    note = function(text) {
      if (!enabled) return(invisible())
      if (state$shown) cat("\n", file = stderr())
      state$shown <- FALSE
      state$width <- 0L
      cat(text, "\n", sep = "", file = stderr())
    },
    done = function() {
      if (enabled && state$shown) cat("\n", file = stderr())
      state$shown <- FALSE
      state$width <- 0L
    }
  )
}

# The request object every command sends.
.ss_request <- function(command, args = list(), verbose = FALSE) {
  list(
    protocol = .ss_protocol,
    command = command,
    client = list(name = "spacescans-r", version = .ss_pkg_version(),
                  protocol_range = I(c(.ss_protocol, .ss_protocol))),
    verbose = isTRUE(verbose),
    args = .ss_named(args)
  )
}

.ss_paths <- function(run_dir) {
  list(request = file.path(run_dir, "request.json"), result = file.path(run_dir, "result.json"),
       log = file.path(run_dir, "bridge.log"), events = file.path(run_dir, "events.jsonl"),
       job = file.path(run_dir, "job.json"))
}

# Start the bridge for `command` in `run_dir` (request already written).
# stdout: "|" for a pipe, or a file path. cleanup: the process must not
# outlive R. Its tree is killed when the handle is garbage collected or R
# exits; processx's supervisor stops it when R itself is killed, and the
# bridge stops its work when its parent process (this R) goes away
# (SPACESCANS_BRIDGE_PARENT). A persistent job (cleanup = FALSE) keeps running.
.ss_spawn <- function(py, command, run_dir, stdout = "|", cleanup = TRUE, env = NULL) {
  p <- .ss_paths(run_dir)
  env <- env %||% .ss_child_env()
  if (cleanup) env[["SPACESCANS_BRIDGE_PARENT"]] <- as.character(Sys.getpid())
  tryCatch(
    processx::process$new(
      py,
      c("-I", "-u", "-X", "utf8", "-m", "spacescans.bridge", command,
        "--request", p$request, "--result", p$result),
      stdout = stdout, stderr = p$log, wd = run_dir, env = env,
      cleanup = cleanup, cleanup_tree = cleanup, supervise = cleanup
    ),
    error = function(e) {
      .ss_abort("python", sprintf("Cannot start Python (%s): %s", py, conditionMessage(e)))
    }
  )
}

.ss_interrupt_child <- function(proc, grace) {
  suspendInterrupts({
    if (proc$is_alive()) {
      try(proc$interrupt(), silent = TRUE)
      try(proc$wait(as.integer(max(grace, 0) * 1000)), silent = TRUE)
    }
    # Also reaps grandchildren (pipeline steps) by processx's environment marker.
    try(proc$kill_tree(), silent = TRUE)
  })
}

# An event consumer: keeps the latest step / fraction / overall fraction,
# collects warning events, drives the progress display and calls on_event.
.ss_event_state <- function(ui, verbose = FALSE, on_event = NULL) {
  st <- new.env(parent = emptyenv())
  st$step <- NULL
  st$frac <- NA_real_
  st$overall <- NA_real_
  st$n_events <- 0L
  st$warnings <- list()
  st$last <- NULL
  st$handle <- function(ev) {
    if (!is.list(ev) || is.null(ev$ev)) return(invisible())
    st$n_events <- st$n_events + 1L
    st$last <- ev
    if (is.function(on_event)) on_event(ev)
    num <- function(x) if (is.numeric(x) && length(x) == 1L) as.numeric(x) else NA_real_
    switch(ev$ev,
      step_start = {
        st$step <- ev$step
        st$frac <- 0
        if (!is.na(num(ev$overall))) st$overall <- num(ev$overall)
        if (isTRUE(verbose)) {
          ui$note(sprintf("step %s%s", ev$step %||% "",
                          if (!is.null(ev$pattern)) sprintf(" (%s)", ev$pattern) else ""))
        } else {
          ui$update(st$step, st$frac, st$overall)
        }
      },
      progress = {
        if (!is.null(ev$step)) st$step <- ev$step
        st$frac <- num(ev$frac)
        if (!is.na(num(ev$overall))) st$overall <- num(ev$overall)
        ui$update(st$step, st$frac, st$overall)
      },
      cache_hit = ui$update(ev$step, 1, st$overall, "(cached)"),
      step_end = {
        if (identical(num(ev$rc), 0) && !isTRUE(ev$cancelled) && !isTRUE(ev$timed_out)) st$frac <- 1
        if (!is.na(num(ev$overall))) st$overall <- num(ev$overall)
        if (isTRUE(verbose)) {
          ui$note(sprintf("step %s finished (%s s)", ev$step %||% "", format(ev$seconds %||% NA)))
        }
      },
      download = {
        total <- num(ev$total)
        got <- num(ev$bytes)
        frac <- if (!is.na(total) && total > 0 && !is.na(got)) got / total else NA_real_
        ui$update(ev$artifact %||% "download", frac, NULL,
                  sprintf("%s of %s", .ss_format_bytes(got), .ss_format_bytes(total)))
      },
      verify = ui$status(sprintf("verifying %s", ev$artifact %||% ev$file %||% "")),
      line = if (isTRUE(verbose)) ui$note(ev$text %||% ""),
      warning = { st$warnings[[length(st$warnings) + 1L]] <- ev },
      NULL
    )
    invisible()
  }
  st
}

.ss_parse_event <- function(line) {
  ev <- tryCatch(jsonlite::parse_json(line, simplifyVector = FALSE), error = function(e) NULL)
  if (is.list(ev) && !is.null(ev$ev)) ev else NULL
}

.ss_read_result_file <- function(path) {
  if (!file.exists(path)) return(NULL)
  tryCatch(jsonlite::read_json(path, simplifyVector = FALSE), error = function(e) NULL)
}

# Turn the end of a bridge process into its value, or raise the matching
# spacescans_error. `kept` is the run directory to report (NULL when it is
# about to be removed).
.ss_outcome <- function(status, result, stderr_tail, kept = NULL, interrupted = FALSE,
                        emit_warnings = TRUE) {
  if (interrupted) {
    .ss_abort("interrupted", paste0(
      "Interrupted; the spacescans Python process was stopped.",
      if (!is.null(kept)) paste0("\nRun directory kept at ", kept, "; remove it with unlink(\"",
                                 kept, "\", recursive = TRUE).")),
      exit_status = status, run_dir = kept, result = result, stderr = stderr_tail)
  }
  if (is.list(result) && isTRUE(result$ok) && identical(as.integer(status), 0L)) {
    if (emit_warnings) {
      for (w in result$warnings) {
        if (!is.null(w$message)) warning(paste0("spacescans: ", w$message), call. = FALSE)
      }
    }
    val <- result$value
    if (is.list(val)) attr(val, "spacescans") <- result$spacescans
    return(val)
  }
  if (is.list(result) && identical(result$ok, FALSE) && is.list(result$error)) {
    err <- result$error
    kind <- if (is.character(err$kind) && length(err$kind) == 1L) err$kind else "internal"
    msg <- err$message %||% "the spacescans bridge reported an error"
    details <- err$details %||% list()
    if (identical(kind, "step_failed")) msg <- .ss_step_message(msg, details)
    if (!is.null(kept)) msg <- paste0(msg, "\nRun directory kept at ", kept,
                                      "; remove it with unlink(\"", kept, "\", recursive = TRUE).")
    .ss_abort(kind, msg, hint = .ss_r_hint(kind, details, err$hint),
              details = details, py_type = err$type, py_traceback = err$traceback,
              exit_status = status, run_dir = kept, stderr = stderr_tail, result = result)
  }
  # No (valid) result: the process crashed.
  if (is.numeric(status) && !is.na(status) && status < 0) {
    sig <- -status
    msg <- sprintf("The spacescans Python process was killed by %s.", .ss_signal_name(sig))
    hint <- if (sig == 9) {
      "SIGKILL usually means the system ran out of memory; try a smaller cohort, fewer threads or fewer workers."
    }
  } else {
    msg <- sprintf("The spacescans Python process exited with status %s without writing a result.",
                   format(status))
    hint <- NULL
  }
  if (length(stderr_tail)) msg <- paste0(msg, "\nLast lines of its log:\n", paste(stderr_tail, collapse = "\n"))
  if (!is.null(kept)) msg <- paste0(msg, "\nRun directory kept at ", kept, ".")
  .ss_abort("crashed", msg, hint = hint, exit_status = status, run_dir = kept, stderr = stderr_tail)
}

# A failed step: name the step, its exit code and the last lines of its log.
.ss_step_message <- function(msg, details) {
  tail <- unlist(details$log_tail %||% details$tail)
  if (length(tail)) {
    msg <- paste0(msg, "\nLast lines of the step's log:\n", paste(utils::tail(tail, 20), collapse = "\n"))
  }
  msg
}

# Run one bridge command and return its `value`, or raise a spacescans_error.
#
# progress: show a progress line; verbose: print raw pipeline lines;
# on_event: function(event) called for every event; run_dir: use this
# directory (kept) instead of a fresh temporary one; keep: keep the fresh
# directory; keep_on_error: keep it when the command fails; env: extra
# environment variables for the bridge process.
.ss_bridge <- function(command, args = list(), python = NULL, progress = FALSE, verbose = FALSE,
                       on_event = NULL, run_dir = NULL, keep = FALSE, keep_on_error = FALSE,
                       check_version = TRUE, env = NULL) {
  py <- python %||% ss_python()$path
  if (check_version && is.null(python)) .ss_hello()
  own_dir <- is.null(run_dir)
  if (own_dir) run_dir <- .ss_new_run_dir()
  p <- .ss_paths(run_dir)
  unlink(p$result)
  writeLines(.ss_to_json(.ss_request(command, args, verbose)), p$request, useBytes = TRUE)

  ok <- FALSE
  proc <- NULL
  on.exit({
    if (!is.null(proc) && proc$is_alive()) try(proc$kill_tree(), silent = TRUE)
    if (own_dir && !keep && (ok || !keep_on_error)) unlink(run_dir, recursive = TRUE)
  }, add = TRUE)

  proc <- .ss_spawn(py, command, run_dir, stdout = "|", cleanup = TRUE, env = .ss_child_env(env))
  ui <- .ss_progress_ui(isTRUE(progress))
  evs <- .ss_event_state(ui, verbose = verbose, on_event = on_event)
  handle <- function(line) evs$handle(.ss_parse_event(line))

  poll_ms <- as.integer(getOption("spacescans.poll_ms", 200L))
  interrupted <- FALSE
  tryCatch({
    repeat {
      proc$poll_io(poll_ms)
      for (line in proc$read_output_lines()) handle(line)
      if (!proc$is_alive()) break
    }
    for (line in proc$read_all_output_lines()) handle(line)
  }, interrupt = function(e) {
    interrupted <<- TRUE
    .ss_interrupt_child(proc, getOption("spacescans.interrupt_grace", 10))
  })
  ui$done()
  proc$wait()
  status <- proc$get_exit_status()
  result <- .ss_read_result_file(p$result)
  stderr_tail <- .ss_tail(p$log)
  kept <- if (own_dir && !keep && !keep_on_error) NULL else run_dir
  value <- .ss_outcome(status, result, stderr_tail, kept = kept, interrupted = interrupted)
  ok <- TRUE
  value
}

# Data-frame helpers for bridge values -------------------------------------

# A list of records (JSON objects) as a data frame. Scalar fields become
# columns; array fields are joined with ", " (or kept as list columns with
# list_cols); missing fields are NA.
.ss_records_df <- function(rows, list_cols = character(), first = character()) {
  if (is.null(rows) || !length(rows)) return(data.frame())
  if (!is.null(names(rows)) && all(nzchar(names(rows))) &&
      all(vapply(rows, is.list, logical(1)))) {
    # an object keyed by id: {key: {...}} -> records with the key as first field
    keys <- names(rows)
    rows <- lapply(seq_along(rows), function(i) c(list(.key = keys[i]), rows[[i]]))
  }
  fields <- unique(unlist(lapply(rows, names)))
  fields <- c(intersect(first, fields), setdiff(fields, first))
  cols <- lapply(fields, function(f) {
    vals <- lapply(rows, function(r) r[[f]])
    if (f %in% list_cols) return(I(lapply(vals, function(v) unlist(v) %||% character())))
    scalar <- vapply(vals, function(v) is.null(v) || (is.atomic(v) && length(v) <= 1L), logical(1))
    if (all(scalar)) {
      vals <- lapply(vals, function(v) if (is.null(v) || !length(v)) NA else v)
      types <- unique(vapply(vals[!vapply(vals, function(v) length(v) == 1L && is.na(v), logical(1))],
                             function(v) class(v)[1], character(1)))
      out <- unlist(vals)
      if (length(types) == 1L && types == "integer") out <- as.integer(out)
      return(out)
    }
    vapply(vals, function(v) {
      if (is.null(v) || !length(v)) return(NA_character_)
      flat <- unlist(v)
      if (is.null(flat)) NA_character_ else paste(as.character(flat), collapse = ", ")
    }, character(1))
  })
  names(cols) <- fields
  df <- as.data.frame(cols, stringsAsFactors = FALSE, optional = TRUE)
  names(df) <- fields
  df
}
