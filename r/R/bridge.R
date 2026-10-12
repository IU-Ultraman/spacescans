# Running `python -m spacescans.bridge`: request/result files, an interruptible
# processx poll loop, streamed events and crash detection.

# Environment variables the child must not inherit: they would redirect the
# interpreter (PYTHON*) or the GDAL/PROJ data of the wheels, or load R's own
# libraries into Python (R sets DYLD_FALLBACK_LIBRARY_PATH on macOS).
.ss_drop_env <- c(
  "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONEXECUTABLE",
  "GDAL_DATA", "PROJ_DATA", "PROJ_LIB", "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_LIBRARY_PATH"
)

.ss_child_env <- function() {
  env <- Sys.getenv()
  env[setdiff(names(env), .ss_drop_env)]
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

.ss_new_run_dir <- function(parent = tempdir()) {
  dir <- tempfile("ss-", tmpdir = parent)
  dir.create(dir, recursive = TRUE, mode = "0700")
  Sys.chmod(dir, "0700", use_umask = FALSE)
  normalizePath(dir)
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

# A minimal console progress display (no cli dependency).
.ss_progress_ui <- function(enabled) {
  state <- new.env(parent = emptyenv())
  state$shown <- FALSE
  list(
    update = function(step, frac) {
      if (!enabled) return(invisible())
      pct <- if (is.numeric(frac) && length(frac) == 1L) round(100 * frac) else NA
      label <- if (is.character(step) && length(step) == 1L) step else "spacescans"
      cat(sprintf("\r%s %s", label, if (is.na(pct)) "" else sprintf("%3d%%", pct)), file = stderr())
      state$shown <- TRUE
    },
    note = function(text) {
      if (!enabled) return(invisible())
      if (state$shown) cat("\n", file = stderr())
      state$shown <- FALSE
      cat(text, "\n", sep = "", file = stderr())
    },
    done = function() {
      if (enabled && state$shown) cat("\n", file = stderr())
      state$shown <- FALSE
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

# Run one bridge command and return its `value`, or raise a spacescans_error.
#
# progress: show a progress line; verbose: print raw pipeline lines;
# on_event: function(event) called for every event; run_dir: use this
# directory (kept) instead of a fresh temporary one; keep: keep the fresh
# directory; keep_on_error: keep it when the command fails.
.ss_bridge <- function(command, args = list(), python = NULL, progress = FALSE, verbose = FALSE,
                       on_event = NULL, run_dir = NULL, keep = FALSE, keep_on_error = FALSE,
                       check_version = TRUE) {
  py <- python %||% ss_python()$path
  if (check_version && is.null(python)) .ss_hello()
  own_dir <- is.null(run_dir)
  if (own_dir) run_dir <- .ss_new_run_dir()
  req_path <- file.path(run_dir, "request.json")
  res_path <- file.path(run_dir, "result.json")
  log_path <- file.path(run_dir, "bridge.log")
  unlink(res_path)
  request <- list(
    protocol = .ss_protocol,
    command = command,
    client = list(name = "spacescans-r", version = .ss_pkg_version(),
                  protocol_range = I(c(.ss_protocol, .ss_protocol))),
    verbose = isTRUE(verbose),
    args = .ss_named(args)
  )
  writeLines(.ss_to_json(request), req_path, useBytes = TRUE)

  ok <- FALSE
  proc <- NULL
  on.exit({
    if (!is.null(proc) && proc$is_alive()) try(proc$kill_tree(), silent = TRUE)
    if (own_dir && !keep && (ok || !keep_on_error)) unlink(run_dir, recursive = TRUE)
  }, add = TRUE)

  proc <- tryCatch(
    processx::process$new(
      py,
      c("-I", "-u", "-X", "utf8", "-m", "spacescans.bridge", command,
        "--request", req_path, "--result", res_path),
      stdout = "|", stderr = log_path, wd = run_dir, env = .ss_child_env(),
      cleanup = TRUE, cleanup_tree = TRUE
    ),
    error = function(e) {
      .ss_abort("python", sprintf("Cannot start Python (%s): %s", py, conditionMessage(e)))
    }
  )

  ui <- .ss_progress_ui(isTRUE(progress))
  py_warnings <- list()
  handle <- function(line) {
    ev <- tryCatch(jsonlite::parse_json(line, simplifyVector = FALSE), error = function(e) NULL)
    if (!is.list(ev) || is.null(ev$ev)) return(invisible())
    if (is.function(on_event)) on_event(ev)
    switch(ev$ev,
      progress = ui$update(ev$step, ev$frac),
      line = if (isTRUE(verbose)) ui$note(ev$text),
      step_start = if (isTRUE(verbose)) ui$note(sprintf("step %s (%s)", ev$step, ev$pattern %||% "")),
      warning = { py_warnings[[length(py_warnings) + 1L]] <<- ev },
      NULL
    )
    invisible()
  }

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
    .ss_interrupt_child(proc, getOption("spacescans.interrupt_grace", 5))
  })
  ui$done()
  proc$wait()
  status <- proc$get_exit_status()
  result <- if (file.exists(res_path)) {
    tryCatch(jsonlite::read_json(res_path, simplifyVector = FALSE), error = function(e) NULL)
  }
  stderr_tail <- .ss_tail(log_path)
  kept <- if (own_dir && !keep && !keep_on_error) NULL else run_dir

  if (interrupted) {
    .ss_abort("interrupted", "Interrupted; the spacescans Python process was stopped.",
              exit_status = status, run_dir = kept, result = result, stderr = stderr_tail)
  }
  if (is.list(result) && isTRUE(result$ok) && identical(status, 0L)) {
    ok <- TRUE
    for (w in result$warnings) {
      if (!is.null(w$message)) warning(paste0("spacescans: ", w$message), call. = FALSE)
    }
    attr_val <- result$value
    if (is.list(attr_val)) attr(attr_val, "spacescans") <- result$spacescans
    return(attr_val)
  }
  if (is.list(result) && identical(result$ok, FALSE) && is.list(result$error)) {
    err <- result$error
    kind <- if (is.character(err$kind) && length(err$kind) == 1L) err$kind else "internal"
    msg <- err$message %||% "the spacescans bridge reported an error"
    if (!is.null(kept)) msg <- paste0(msg, "\nRun directory kept at ", kept,
                                      "; remove it with unlink(\"", kept, "\", recursive = TRUE).")
    .ss_abort(kind, msg, hint = .ss_r_hint(kind, err$details %||% list(), err$hint),
              details = err$details, py_type = err$type, py_traceback = err$traceback,
              exit_status = status, run_dir = kept, stderr = stderr_tail, result = result)
  }
  # No (valid) result: the process crashed.
  if (is.numeric(status) && !is.na(status) && status < 0) {
    sig <- -status
    msg <- sprintf("The spacescans Python process was killed by %s.", .ss_signal_name(sig))
    hint <- if (sig == 9) {
      "SIGKILL usually means the system ran out of memory; try a smaller cohort or fewer threads."
    }
  } else {
    msg <- sprintf("The spacescans Python process exited with status %s without writing a result.",
                   format(status))
    hint <- NULL
  }
  if (length(stderr_tail)) msg <- paste0(msg, "\nLast lines of its log:\n", paste(stderr_tail, collapse = "\n"))
  .ss_abort("crashed", msg, hint = hint, exit_status = status, run_dir = kept, stderr = stderr_tail)
}
