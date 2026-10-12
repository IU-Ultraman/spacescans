#' Conditions raised by spacescans
#'
#' Every error the package raises inherits from `spacescans_error`, plus a
#' class for its kind, so callers can catch exactly what they expect:
#'
#' | kind | class | when |
#' |---|---|---|
#' | `protocol` | `spacescans_error_protocol` | versions or protocol do not match, malformed request |
#' | `python` | `spacescans_error_python` | no usable Python interpreter |
#' | `install` | `spacescans_error_install` | [ss_install()] failed |
#' | `config` | `spacescans_error_config` | invalid arguments, unknown variable, invalid pipeline config |
#' | `cohort` | `spacescans_error_cohort` | a required column is missing, a date cannot be parsed, a coordinate is missing or out of range (the message gives row numbers, never values) |
#' | `data` | `spacescans_error_data` | an input file is missing or unreadable |
#' | `data_missing` | `spacescans_error_data_missing` (also `spacescans_error_data`) | the data a variable needs is not in the data directory; the hint is the [ss_fetch()] call to run |
#' | `data_corrupt` | `spacescans_error_data_corrupt` (also `spacescans_error_data`) | a checksum or size does not match; see [ss_data_verify()] |
#' | `terms_required` | `spacescans_error_terms` | a provider's terms must be accepted with `ss_fetch(accept_terms = )` |
#' | `missing_extra` | `spacescans_error_missing_extra` | a pattern needs a Python extra that is not installed; the hint is the [ss_install()] call |
#' | `network` | `spacescans_error_network` | a download failed after retries; running it again resumes |
#' | `disk_space` | `spacescans_error_disk` | not enough free space for a download |
#' | `locked` | `spacescans_error_locked` | another download holds the data directory's lock |
#' | `step_failed` | `spacescans_error_step` | a pipeline step exited with a non-zero code (`details` has `step`, `rc` and the log tail) |
#' | `runtime` | `spacescans_error_runtime` | the pipeline raised an error |
#' | `interrupted` | `spacescans_error_interrupted` | Ctrl-C, SIGINT or SIGTERM |
#' | `crashed` | `spacescans_error_crashed` | the Python process died without a result |
#' | `internal` | `spacescans_error_internal` | a bug in the bridge or the package |
#'
#' Kinds added later by the bridge map to `spacescans_error_<kind>`
#' automatically.
#'
#' The condition object carries `kind`, `hint`, `details` (from Python),
#' `py_type` and `py_traceback` (the Python exception), `stderr` (the last
#' lines of the bridge log), `exit_status` and `run_dir` where they apply.
#'
#' @examples
#' tryCatch(
#'   spacescans:::.ss_abort("config", "bad value", hint = "fix it"),
#'   spacescans_error_config = function(e) conditionMessage(e)
#' )
#' @name spacescans-conditions
NULL

.ss_kind_suffix <- c(terms_required = "terms", disk_space = "disk", step_failed = "step")

.ss_error_class <- function(kind) {
  suffix <- if (kind %in% names(.ss_kind_suffix)) .ss_kind_suffix[[kind]] else kind
  cls <- paste0("spacescans_error_", suffix)
  if (startsWith(kind, "data_")) cls <- c(cls, "spacescans_error_data")
  c(cls, "spacescans_error", "error", "condition")
}

.ss_condition <- function(kind, message, hint = NULL, ..., call = NULL) {
  msg <- message
  if (length(hint) == 1L && !is.na(hint) && nzchar(hint)) {
    msg <- paste0(msg, "\nHint: ", hint)
  }
  structure(
    class = .ss_error_class(kind),
    c(list(message = msg, call = call, kind = kind, hint = hint), list(...))
  )
}

.ss_abort <- function(kind, message, hint = NULL, ..., call = NULL) {
  stop(.ss_condition(kind, message, hint = hint, ..., call = call))
}

# R code for a character vector: c("a", "b") or "a".
.ss_deparse_chr <- function(x) {
  x <- unique(as.character(unlist(x)))
  q <- paste0('"', gsub('"', '\\\\"', x), '"')
  if (length(q) == 1L) q else paste0("c(", paste(q, collapse = ", "), ")")
}

.ss_chr <- function(x) {
  x <- unlist(x)
  if (is.null(x)) character() else as.character(x)
}

# R-side advice for an error reported by the bridge. The bridge's own hint is
# written for the command line; here it becomes the R call to make.
.ss_r_hint <- function(kind, details, py_hint) {
  details <- details %||% list()
  switch(kind,
    missing_extra = {
      extra <- .ss_chr(details$extra %||% details$extras)
      if (length(extra)) {
        sprintf("ss_install(extras = %s) adds the missing Python extra.", .ss_deparse_chr(extra))
      } else {
        "ss_install() installs the Python extras."
      }
    },
    protocol = "ss_install() installs the Python package that matches this R package.",
    data = if (!is.null(details$path)) sprintf("Check that %s exists and is readable.", details$path) else py_hint,
    data_missing = {
      vars <- .ss_chr(details$variables)
      ds <- .ss_chr(details$datasets %||% details$missing)
      if (length(vars)) {
        sprintf("Run ss_fetch(%s) to download the missing data.", .ss_deparse_chr(vars))
      } else if (length(ds)) {
        sprintf("Run ss_fetch(datasets = %s) to download the missing data.", .ss_deparse_chr(ds))
      } else {
        "ss_data_status() shows what is missing; ss_fetch() downloads it."
      }
    },
    data_corrupt = {
      ds <- .ss_chr(details$datasets %||% details$dataset)
      if (length(ds)) {
        sprintf("ss_data_verify(datasets = %s, deep = TRUE) checks every file; ss_fetch(datasets = %s) replaces damaged ones.",
                .ss_deparse_chr(ds), .ss_deparse_chr(ds))
      } else {
        "ss_data_verify(deep = TRUE) checks every file."
      }
    },
    terms_required = {
      terms <- details$terms %||% details$terms_id %||% details$id
      ds <- .ss_chr(details$dataset %||% details$datasets)
      if (is.list(terms) && length(terms) && is.list(terms[[1]])) {
        # data.TermsRequiredError: details$terms is a list of {id, dataset, ...}
        ds <- unique(c(ds, .ss_chr(lapply(terms, function(t) t$dataset %||% t$datasets))))
        terms <- .ss_chr(lapply(terms, function(t) t$id))
      } else {
        terms <- .ss_chr(terms)
      }
      if (length(terms) && length(ds)) {
        sprintf("Read them with ss_data_terms(%s), then run ss_fetch(datasets = %s, accept_terms = %s).",
                .ss_deparse_chr(ds[1]), .ss_deparse_chr(ds), .ss_deparse_chr(terms))
      } else if (length(terms)) {
        sprintf("Accept them explicitly: ss_fetch(..., accept_terms = %s).", .ss_deparse_chr(terms))
      } else {
        "Pass accept_terms = to ss_fetch() after reading the terms with ss_data_terms()."
      }
    },
    network = "Run the same call again: downloads resume where they stopped.",
    disk_space = {
      need <- details$required_bytes %||% details$needed_bytes %||% details$required
      free <- details$free_bytes %||% details$available_bytes %||% details$free
      if (!is.null(need) && !is.null(free)) {
        sprintf("Needs %s, %s free. Free some space or choose another data directory with ss_data_dir().",
                .ss_format_bytes(need), .ss_format_bytes(free))
      } else {
        "Free some space or choose another data directory with ss_data_dir()."
      }
    },
    locked = "Another download into this data directory is running; wait for it to finish.",
    step_failed = {
      rc <- suppressWarnings(as.integer(details$rc))
      step <- details$step
      oom <- length(rc) == 1L && !is.na(rc) && rc %in% c(-9L, 137L)
      paste0(
        if (!is.null(step)) sprintf("Step %s exited with code %s. ", step, format(details$rc)) else "",
        if (oom) {
          "Code -9/137 usually means the system ran out of memory: try fewer threads, a memory_limit, or a smaller cohort."
        } else {
          "The run directory holds the step's config and log."
        })
    },
    cohort = py_hint,
    py_hint
  )
}
