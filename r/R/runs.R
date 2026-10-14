# Run directories under one per-user root, with an owner record and a sweep
# (phase-C decision 2). A run directory holds cohort data (cohort.parquet,
# input.parquet, C3 and C4 outputs), so one left behind by a session that was
# killed must not survive: every directory the package creates for itself gets
# an owner.json (pid, start time, host) and .ss_sweep_runs() deletes (never
# archives) those whose owner process no longer exists. The sweep runs at
# package load, in ss_doctor() and in ss_cache_clear(); the Python side runs the
# same sweep (spacescans.orchestrate.runs) from link() and the bridge's cache-*
# commands, so R and Python clean up after each other.

.ss_runs_root <- function() {
  file.path(tools::R_user_dir("spacescans", "cache"), "runs")
}

.ss_hostname <- function() {
  tryCatch(as.character(Sys.info()[["nodename"]]), error = function(e) "")
}

# Start time of a process as Unix seconds (NA when it cannot be read).
.ss_process_start <- function(pid = Sys.getpid()) {
  tryCatch({
    h <- ps::ps_handle(as.integer(pid))
    as.numeric(ps::ps_create_time(h))
  }, error = function(e) NA_real_)
}

# The owner record of a run directory: this process (or `pid`), never a cohort value.
.ss_write_owner <- function(run_dir, pid = Sys.getpid(), client = NULL) {
  start <- .ss_process_start(pid)
  record <- list(
    schema = 1L, pid = as.integer(pid), start_time = if (is.na(start)) NULL else start,
    host = .ss_hostname(), client = client %||% paste("spacescans-r", .ss_pkg_version()),
    created_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%S%z")
  )
  path <- file.path(run_dir, "owner.json")
  tmp <- tempfile(pattern = ".owner.json.", tmpdir = run_dir)
  writeLines(jsonlite::toJSON(record, auto_unbox = TRUE, null = "null", digits = NA, pretty = TRUE),
             tmp, useBytes = TRUE)
  Sys.chmod(tmp, "0600", use_umask = FALSE)
  file.rename(tmp, path)
  invisible(record)
}

.ss_read_owner <- function(run_dir) {
  path <- file.path(run_dir, "owner.json")
  if (!file.exists(path)) return(NULL)
  rec <- tryCatch(jsonlite::read_json(path, simplifyVector = TRUE), error = function(e) NULL)
  if (!is.list(rec)) return(NULL)
  rec
}

# TRUE: the owner process runs (same start time, within 2 s, when both sides
# know it); FALSE: no such pid, or the pid now belongs to another process;
# NA: the record is from another host or has no usable pid.
.ss_owner_alive <- function(owner, host = .ss_hostname()) {
  if (!is.list(owner)) return(NA)
  rec_host <- as.character(owner$host %||% "")
  if (length(rec_host) == 1L && nzchar(rec_host) && nzchar(host) && !identical(rec_host, host)) return(NA)
  pid <- suppressWarnings(as.integer(owner$pid))
  if (length(pid) != 1L || is.na(pid) || pid <= 0L) return(NA)
  h <- tryCatch(ps::ps_handle(pid), error = function(e) NULL)
  if (is.null(h)) return(FALSE)
  if (!isTRUE(tryCatch(ps::ps_is_running(h), error = function(e) FALSE))) return(FALSE)
  recorded <- suppressWarnings(as.numeric(owner$start_time))
  now <- tryCatch(as.numeric(ps::ps_create_time(h)), error = function(e) NA_real_)
  if (length(recorded) == 1L && !is.na(recorded) && !is.na(now) && abs(now - recorded) > 2) return(FALSE)
  TRUE
}

.ss_dir_bytes <- function(path) {
  files <- list.files(path, all.files = TRUE, full.names = TRUE, recursive = TRUE, no.. = TRUE)
  sizes <- suppressWarnings(file.info(files, extra_cols = FALSE)$size)
  sum(sizes, na.rm = TRUE)
}

# Remove the run directories under `root` whose owner process is gone. Only
# immediate subdirectories (no symlinks) with an owner.json are judged; the
# rest is left alone and listed as unknown. Returns removed, kept and unknown
# (directory names), bytes freed, errors, and `message` (NULL when nothing
# was removed). Nothing is created; a missing root is an empty result.
.ss_sweep_runs <- function(root = .ss_runs_root(), dry_run = FALSE) {
  out <- list(root = root, dry_run = isTRUE(dry_run), removed = character(), kept = character(),
              unknown = character(), bytes = 0, errors = character(), message = NULL)
  if (!dir.exists(root)) return(out)
  entries <- sort(list.files(root, all.files = TRUE, no.. = TRUE))
  host <- .ss_hostname()
  for (name in entries) {
    path <- file.path(root, name)
    info <- suppressWarnings(file.info(path, extra_cols = FALSE))
    if (!isTRUE(info$isdir) || !is.na(Sys.readlink(path)) && nzchar(Sys.readlink(path))) next
    owner <- .ss_read_owner(path)
    if (is.null(owner)) {
      out$unknown <- c(out$unknown, name)
      next
    }
    alive <- tryCatch(.ss_owner_alive(owner, host), error = function(e) NA)
    if (is.na(alive)) {
      out$unknown <- c(out$unknown, name)
    } else if (alive) {
      out$kept <- c(out$kept, name)
    } else {
      size <- .ss_dir_bytes(path)
      if (!isTRUE(dry_run)) {
        unlink(path, recursive = TRUE, force = TRUE)
        if (dir.exists(path)) {
          out$errors <- c(out$errors, name)
          next
        }
      }
      out$removed <- c(out$removed, name)
      out$bytes <- out$bytes + size
    }
  }
  n <- length(out$removed)
  if (n) {
    out$message <- sprintf("%s %d run director%s left by processes that no longer exist under %s (%.1f MB)",
                           if (isTRUE(dry_run)) "would remove" else "removed", n,
                           if (n == 1L) "y" else "ies", root, out$bytes / 1e6)
  }
  out
}

# The sweep as the package runs it at load and in ss_doctor() / ss_cache_clear():
# never fails, and says in a message how many directories went.
.ss_sweep_and_tell <- function(startup = FALSE) {
  sw <- tryCatch(.ss_sweep_runs(), error = function(e) {
    list(removed = character(), kept = character(), unknown = character(), bytes = 0,
         errors = conditionMessage(e), message = NULL)
  })
  if (!is.null(sw$message)) {
    text <- paste0("spacescans: ", sw$message, ".")
    if (startup) packageStartupMessage(text) else message(text)
  }
  invisible(sw)
}
