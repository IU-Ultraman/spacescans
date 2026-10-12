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
#' | `config` | `spacescans_error_config` | invalid arguments or pipeline config |
#' | `data` | `spacescans_error_data` | an input file is missing or unreadable |
#' | `data_missing`, `data_corrupt` | `spacescans_error_data_missing`, `..._data_corrupt` (both also `spacescans_error_data`) | phase B data checks |
#' | `missing_extra` | `spacescans_error_missing_extra` | a pattern needs a Python extra that is not installed |
#' | `runtime` | `spacescans_error_runtime` | the pipeline raised an error |
#' | `interrupted` | `spacescans_error_interrupted` | Ctrl-C, SIGINT or SIGTERM |
#' | `crashed` | `spacescans_error_crashed` | the Python process died without a result |
#' | `internal` | `spacescans_error_internal` | a bug in the bridge |
#' | `not_implemented` | `spacescans_error_not_implemented` | a phase B function |
#'
#' Kinds added later by the bridge map to `spacescans_error_<kind>`; the design
#' names `terms_required`, `disk_space` and `step_failed` as
#' `spacescans_error_terms`, `spacescans_error_disk` and `spacescans_error_step`.
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

# R-side advice for an error reported by the bridge.
.ss_r_hint <- function(kind, details, py_hint) {
  switch(kind,
    missing_extra = {
      extra <- details$extra
      if (length(extra) == 1L && is.character(extra)) {
        sprintf('ss_install(extras = "%s") adds the missing Python extra.', extra)
      } else {
        "ss_install() installs the Python extras."
      }
    },
    protocol = "ss_install() installs the Python package that matches this R package.",
    data = if (!is.null(details$path)) sprintf("Check that %s exists and is readable.", details$path),
    py_hint
  )
}
