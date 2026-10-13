# Long-running bridge commands (link, data-fetch) run as jobs: the bridge's
# events go to run_dir/events.jsonl, its log to run_dir/bridge.log, and R
# writes run_dir/job.json (no cohort values) so a job can be found again with
# ss_job_attach() after R restarts.

.ss_job_id <- function() {
  paste0(format(Sys.time(), "%Y%m%d%H%M%S"), "-",
         paste(sample(c(letters, 0:9), 6, replace = TRUE), collapse = ""))
}

# An environment-variable name processes of this job inherit, in the format
# ps::ps_find_tree() understands (PS<random>_<unix time>).
.ss_job_marker <- function() {
  paste0("PS", paste(sample(c(LETTERS, 0:9), 10, replace = TRUE), collapse = ""), "_",
         as.integer(Sys.time()))
}

# Start `command` in `run_dir` (which exists, mode 0700). persistent: the job
# may outlive the R session (the bridge is not killed when R exits).
.ss_job_start <- function(command, args, run_dir, verbose = FALSE, persistent = FALSE,
                          env = NULL, meta = list()) {
  py <- ss_python()
  .ss_hello()
  p <- .ss_paths(run_dir)
  unlink(c(p$result, p$events))
  writeLines(.ss_to_json(.ss_request(command, args, verbose)), p$request, useBytes = TRUE)
  file.create(p$events)
  marker <- .ss_job_marker()
  proc <- .ss_spawn(py$path, command, run_dir, stdout = p$events, cleanup = !persistent,
                    env = .ss_child_env(c(env, stats::setNames(list("YES"), marker))))
  job <- new.env(parent = emptyenv())
  job$id <- .ss_job_id()
  job$command <- command
  job$run_dir <- run_dir
  job$proc <- proc
  job$pid <- proc$get_pid()
  job$create_time <- tryCatch(as.numeric(proc$get_start_time()), error = function(e) NA_real_)
  job$marker <- marker
  job$started <- Sys.time()
  job$offset <- 0
  job$partial <- ""
  job$verbose <- isTRUE(verbose)
  job$events <- .ss_event_state(.ss_progress_ui(FALSE), verbose = FALSE)
  job$meta <- meta
  job$done <- FALSE
  job$value <- NULL
  info <- list(
    id = job$id, command = command, pid = job$pid, create_time = job$create_time,
    marker = marker, python = py$path, started_at = format(job$started, "%Y-%m-%dT%H:%M:%S%z"),
    r_package = .ss_pkg_version(), persistent = isTRUE(persistent), meta = meta
  )
  writeLines(jsonlite::toJSON(info, auto_unbox = TRUE, null = "null", pretty = TRUE, digits = NA),
             p$job, useBytes = TRUE)
  Sys.chmod(p$job, "0600", use_umask = FALSE)
  class(job) <- "ss_job"
  job
}

# Read the events appended since the last poll (complete lines only).
.ss_job_poll <- function(job, handle = job$events$handle) {
  path <- .ss_paths(job$run_dir)$events
  size <- suppressWarnings(file.info(path)$size)
  if (is.na(size) || size <= job$offset) return(invisible(0L))
  con <- file(path, "rb")
  on.exit(close(con), add = TRUE)
  seek(con, job$offset)
  raw <- readBin(con, "raw", n = size - job$offset)
  job$offset <- job$offset + length(raw)
  text <- paste0(job$partial, rawToChar(raw))
  Encoding(text) <- "UTF-8"
  lines <- strsplit(text, "\n", fixed = TRUE)[[1]]
  if (!endsWith(text, "\n")) {
    job$partial <- utils::tail(lines, 1)
    lines <- utils::head(lines, -1)
  } else {
    job$partial <- ""
  }
  n <- 0L
  for (line in lines) {
    ev <- .ss_parse_event(line)
    if (!is.null(ev)) {
      job$events$handle(ev)
      if (!identical(handle, job$events$handle)) handle(ev)
      n <- n + 1L
    }
  }
  invisible(n)
}

.ss_job_alive <- function(job) {
  if (!is.null(job$proc)) return(job$proc$is_alive())
  h <- .ss_job_handle(job)
  !is.null(h) && isTRUE(tryCatch(ps::ps_is_running(h), error = function(e) FALSE))
}

# A ps handle for an attached job's bridge process, or NULL when it is gone
# (or its pid now belongs to another process).
.ss_job_handle <- function(job) {
  if (is.null(job$pid) || is.na(job$pid)) return(NULL)
  h <- tryCatch(ps::ps_handle(as.integer(job$pid)), error = function(e) NULL)
  if (is.null(h)) return(NULL)
  ct <- tryCatch(as.numeric(ps::ps_create_time(h)), error = function(e) NA_real_)
  if (!is.na(job$create_time) && !is.na(ct) && abs(ct - job$create_time) > 2) return(NULL)
  h
}

.ss_job_exit_status <- function(job) {
  if (!is.null(job$proc)) {
    job$proc$wait(1000)
    return(job$proc$get_exit_status())
  }
  NA_integer_
}

.ss_job_state <- function(job) {
  if (!is.null(job$final_state)) return(job$final_state)
  if (.ss_job_alive(job)) return("running")
  res <- .ss_read_result_file(.ss_paths(job$run_dir)$result)
  if (is.list(res) && isTRUE(res$ok)) return("succeeded")
  if (is.list(res) && is.list(res$error)) {
    return(if (identical(res$error$kind, "interrupted")) "interrupted" else "failed")
  }
  # Cancelled before the bridge could write a result (for example during start-up).
  if (isTRUE(job$cancelled)) return("interrupted")
  "crashed"
}

#' Background jobs
#'
#' `ss_link(wait = FALSE)` returns a job handle at once; these functions
#' follow it. A job's progress events, log, request and result live in its
#' run directory.
#'
#' * `ss_job_status()` reports the state (`"running"`, `"succeeded"`,
#'   `"failed"`, `"interrupted"` or `"crashed"`), the current step and the
#'   progress.
#' * `ss_job_wait()` waits for the job, showing progress. Pressing Ctrl-C
#'   stops waiting but leaves the job running (use `ss_job_cancel()` to stop
#'   it).
#' * `ss_job_cancel()` asks the job to stop (SIGINT), waits up to `grace`
#'   seconds, then kills its whole process tree. Completed C3 steps stay in
#'   the cache, so running the same link again is fast.
#' * `ss_job_result()` waits (unless `wait = FALSE`) and returns what the
#'   finished command returns (for `ss_link()`, the linked data frame), or
#'   raises its error.
#' * `ss_job_attach()` finds a job again from its run directory, for example
#'   after R restarted. Only jobs started with an explicit `run_dir` survive
#'   the R session: a run directory under [tempdir()] is removed when R exits.
#'
#' @param job A job from `ss_link(wait = FALSE)` or `ss_job_attach()`.
#' @param timeout Seconds to wait; `Inf` (default) waits until the job ends.
#' @param progress Show a progress line.
#' @param grace Seconds to wait after SIGINT before killing the process
#'   tree (default: option `spacescans.interrupt_grace`, 10).
#' @param wait For `ss_job_result()`: wait for the job to finish first.
#' @param run_dir The job's run directory.
#' @param cohort For `ss_job_attach()` of a link job: the cohort data frame
#'   the job was started with, so the result can be joined back onto it. It
#'   is compared with the cohort file in the run directory. Without it,
#'   `ss_job_result()` returns the exposures with an `episode_id` column
#'   (the 0-based row number in the cohort).
#' @param cols Column mapping used when the job was started (see
#'   [ss_link()]).
#' @return `ss_job_status()`: a list of class `ss_job_status`.
#'   `ss_job_wait()` and `ss_job_cancel()`: the status, invisibly.
#'   `ss_job_result()`: the command's result. `ss_job_attach()`: a job.
#' @examples
#' \dontrun{
#' job <- ss_link(cohort, c("ndi", "walkability"), wait = FALSE,
#'                run_dir = "~/ss-runs/ndi-walk")
#' ss_job_status(job)
#' res <- ss_job_result(job)
#'
#' # after restarting R
#' job <- ss_job_attach("~/ss-runs/ndi-walk", cohort = cohort)
#' res <- ss_job_result(job)
#' }
#' @name ss_job
NULL

#' @rdname ss_job
#' @export
ss_job_status <- function(job) {
  .ss_check_job(job)
  .ss_job_poll(job)
  state <- .ss_job_state(job)
  if (state != "running") .ss_job_poll(job)
  ev <- job$events
  res <- if (state %in% c("failed", "interrupted")) .ss_read_result_file(.ss_paths(job$run_dir)$result)
  structure(list(
    id = job$id, command = job$command, state = state, pid = job$pid, run_dir = job$run_dir,
    elapsed_s = as.numeric(difftime(Sys.time(), job$started, units = "secs")),
    step = ev$step, frac = ev$frac, overall = ev$overall, events = ev$n_events,
    error = if (is.list(res)) list(kind = res$error$kind, message = res$error$message)
  ), class = "ss_job_status")
}

#' @export
print.ss_job_status <- function(x, ...) {
  cat("spacescans job ", x$id, " (", x$command, "): ", x$state, "\n", sep = "")
  if (!is.null(x$step)) {
    cat("  step     ", x$step, if (!is.na(x$frac)) sprintf(" %d%%", round(100 * x$frac)), "\n", sep = "")
  }
  if (!is.na(x$overall)) cat("  overall  ", sprintf("%d%%", round(100 * x$overall)), "\n", sep = "")
  cat("  elapsed  ", sprintf("%.1f s", x$elapsed_s), "\n", sep = "")
  cat("  run_dir  ", x$run_dir, "\n", sep = "")
  if (!is.null(x$error)) cat("  error    ", x$error$kind, ": ", x$error$message, "\n", sep = "")
  invisible(x)
}

#' @export
print.ss_job <- function(x, ...) {
  print(ss_job_status(x))
  invisible(x)
}

.ss_check_job <- function(job) {
  if (!inherits(job, "ss_job")) .ss_abort("config", "`job` must be a job from ss_link(wait = FALSE) or ss_job_attach().")
}

# Wait for the job, polling its events every spacescans.poll_ms (200 ms).
# cancel_on_interrupt: Ctrl-C cancels the job (ss_link(wait = TRUE));
# otherwise it only stops waiting. Returns the final state, or "running"
# after a timeout or an interrupt that did not cancel.
.ss_job_wait <- function(job, timeout = Inf, progress = FALSE, cancel_on_interrupt = FALSE,
                         verbose = FALSE) {
  ui <- .ss_progress_ui(isTRUE(progress))
  view <- .ss_event_state(ui, verbose = verbose)
  # Replay what happened so far onto the display state.
  view$step <- job$events$step
  view$frac <- job$events$frac
  view$overall <- job$events$overall
  poll_ms <- as.integer(getOption("spacescans.poll_ms", 200L))
  t0 <- Sys.time()
  interrupted <- FALSE
  tryCatch({
    repeat {
      .ss_job_poll(job, handle = view$handle)
      if (!.ss_job_alive(job)) break
      if (as.numeric(difftime(Sys.time(), t0, units = "secs")) >= timeout) break
      if (!is.null(job$proc)) job$proc$wait(poll_ms) else Sys.sleep(poll_ms / 1000)
    }
    .ss_job_poll(job, handle = view$handle)
  }, interrupt = function(e) {
    interrupted <<- TRUE
    ui$done()
    if (cancel_on_interrupt) {
      .ss_job_cancel(job, getOption("spacescans.interrupt_grace", 10))
    } else {
      message("Stopped waiting; the job keeps running. ss_job_cancel(job) stops it.")
    }
  })
  ui$done()
  if (interrupted && cancel_on_interrupt) return("interrupted_by_user")
  .ss_job_state(job)
}

#' @rdname ss_job
#' @export
ss_job_wait <- function(job, timeout = Inf, progress = interactive()) {
  .ss_check_job(job)
  .ss_job_wait(job, timeout = timeout, progress = progress)
  invisible(ss_job_status(job))
}

.ss_job_cancel <- function(job, grace) {
  job$cancelled <- TRUE
  suspendInterrupts({
    if (!is.null(job$proc)) {
      if (job$proc$is_alive()) {
        try(job$proc$interrupt(), silent = TRUE)
        try(job$proc$wait(as.integer(max(grace, 0) * 1000)), silent = TRUE)
      }
      try(job$proc$kill_tree(), silent = TRUE)
    } else {
      h <- .ss_job_handle(job)
      if (!is.null(h) && isTRUE(tryCatch(ps::ps_is_running(h), error = function(e) FALSE))) {
        try(ps::ps_interrupt(h), silent = TRUE)
        deadline <- Sys.time() + grace
        while (Sys.time() < deadline && isTRUE(tryCatch(ps::ps_is_running(h), error = function(e) FALSE))) {
          Sys.sleep(0.1)
        }
      }
    }
    # Whatever is left of the tree (the bridge, pipeline steps) carries the marker.
    if (!is.null(job$marker)) try(ps::ps_kill_tree(job$marker), silent = TRUE)
  })
  invisible()
}

#' @rdname ss_job
#' @export
ss_job_cancel <- function(job, grace = getOption("spacescans.interrupt_grace", 10)) {
  .ss_check_job(job)
  .ss_job_cancel(job, grace)
  invisible(ss_job_status(job))
}

#' @rdname ss_job
#' @export
ss_job_result <- function(job, wait = TRUE, progress = interactive()) {
  .ss_check_job(job)
  if (isTRUE(job$done)) return(job$value)
  if (isTRUE(wait)) .ss_job_wait(job, progress = progress)
  .ss_job_finish(job)
}

# Read the result of a finished job and hand its value to the command's
# finisher (for link: join onto the cohort). Raises the job's error.
.ss_job_finish <- function(job, interrupted = FALSE) {
  if (.ss_job_alive(job) && !interrupted) {
    .ss_abort("config", sprintf("Job %s is still running.", job$id),
              hint = "ss_job_wait(job) waits for it; ss_job_status(job) shows its progress.")
  }
  .ss_job_poll(job)
  p <- .ss_paths(job$run_dir)
  status <- .ss_job_exit_status(job)
  result <- .ss_read_result_file(p$result)
  if (is.na(status) && is.list(result)) status <- if (isTRUE(result$ok)) 0L else 1L
  if (is.list(result) && is.list(result$error) && identical(result$error$kind, "interrupted")) {
    interrupted <- TRUE
  }
  if (!is.list(result) && isTRUE(job$cancelled)) interrupted <- TRUE
  # ss_link()'s finisher reports the link's warnings itself, once.
  value <- .ss_outcome(status, result, .ss_tail(p$log), kept = job$run_dir, interrupted = interrupted,
                       emit_warnings = !identical(job$command, "link"))
  finish <- job$meta_finish %||% function(job, value) value
  out <- tryCatch(finish(job, value), error = function(e) {
    # The run directory stays after an error: say where, and how to remove it.
    if (dir.exists(job$run_dir) && !grepl("Run directory kept at", conditionMessage(e), fixed = TRUE)) {
      e$message <- paste0(conditionMessage(e), .ss_kept_note(job$run_dir))
      if (is.null(e$run_dir)) e$run_dir <- job$run_dir
    }
    stop(e)
  })
  job$done <- TRUE
  job$value <- out
  job$final_state <- "succeeded"
  if (isTRUE(job$remove_on_success)) unlink(job$run_dir, recursive = TRUE)
  out
}

#' @rdname ss_job
#' @export
ss_job_attach <- function(run_dir, cohort = NULL, cols = NULL) {
  if (!is.character(run_dir) || length(run_dir) != 1L || !dir.exists(run_dir)) {
    .ss_abort("config", sprintf("Run directory not found: %s", format(run_dir)))
  }
  run_dir <- normalizePath(run_dir)
  p <- .ss_paths(run_dir)
  if (!file.exists(p$job)) {
    .ss_abort("config", sprintf("%s is not a spacescans job directory (no job.json).", run_dir))
  }
  info <- jsonlite::read_json(p$job, simplifyVector = TRUE)
  job <- new.env(parent = emptyenv())
  job$id <- info$id
  job$command <- info$command
  job$run_dir <- run_dir
  job$proc <- NULL
  job$pid <- info$pid
  job$create_time <- info$create_time %||% NA_real_
  job$marker <- info$marker
  job$started <- tryCatch(as.POSIXct(info$started_at, format = "%Y-%m-%dT%H:%M:%S%z"),
                          error = function(e) Sys.time())
  if (is.na(job$started)) job$started <- Sys.time()
  job$offset <- 0
  job$partial <- ""
  job$verbose <- FALSE
  job$events <- .ss_event_state(.ss_progress_ui(FALSE), verbose = FALSE)
  job$meta <- info$meta %||% list()
  job$done <- FALSE
  job$value <- NULL
  job$remove_on_success <- FALSE
  class(job) <- "ss_job"
  if (identical(job$command, "link")) .ss_link_attach(job, cohort, cols)
  .ss_job_poll(job)
  job
}
