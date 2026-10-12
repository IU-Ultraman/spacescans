# The once-per-session handshake ("capabilities") and the version check.

.ss_pin <- function() {
  path <- system.file("python", "pin.json", package = "spacescans")
  if (!nzchar(path)) return(NULL)
  tryCatch(jsonlite::read_json(path, simplifyVector = TRUE), error = function(e) NULL)
}

.ss_hello <- function(refresh = FALSE) {
  py <- ss_python()
  cached <- .ss_state$hello
  if (!refresh && !is.null(cached) && identical(cached$python, py$path)) return(cached$caps)
  caps <- .ss_bridge("capabilities", python = py$path, check_version = FALSE)
  .ss_check_versions(caps, py)
  .ss_state$hello <- list(python = py$path, caps = caps, time = Sys.time())
  caps
}

.ss_minor <- function(v) {
  parts <- strsplit(sub("[^0-9.].*$", "", v), ".", fixed = TRUE)[[1]]
  paste(utils::head(c(parts, "0", "0"), 2), collapse = ".")
}

.ss_check_versions <- function(caps, py) {
  want <- .ss_expected()
  proto <- caps$protocol
  if (!is.numeric(proto) || !(as.integer(proto) %in% want$protocol)) {
    .ss_abort("protocol",
      sprintf("The Python bridge at %s speaks protocol %s; this R package needs protocol %d.",
              py$path, format(proto), want$protocol),
      hint = "ss_install() installs the Python package that matches this R package.")
  }
  got <- caps$spacescans$version %||% "unknown"
  mode <- match.arg(getOption("spacescans.version_check", "strict"), c("strict", "warn", "off"))
  if (!identical(got, want$version) && mode != "off") {
    msg <- sprintf(
      "spacescans %s is installed for %s (%s), but this R package needs spacescans %s.",
      got, py$path, py$source, want$version)
    if (mode == "warn" && identical(.ss_minor(got), .ss_minor(want$version))) {
      warning(msg, call. = FALSE)
    } else {
      .ss_abort("protocol", msg,
        hint = paste0("Run ss_install() to update the environment, or set ",
                      "options(spacescans.version_check = \"warn\") to accept the same minor version."),
        details = list(installed = got, required = want$version, python = py$path))
    }
  }
  pin <- .ss_pin()
  fp <- caps$spacescans$code_fp
  if (!is.null(pin$code_fp) && !is.null(fp) && !identical(pin$code_fp, fp) && identical(got, pin$version)) {
    warning(sprintf(paste0(
      "The spacescans code at %s differs from the wheel bundled with this R package ",
      "(code fingerprint %s, expected %s): an editable or modified install?"),
      caps$spacescans$path %||% py$path, fp, pin$code_fp), call. = FALSE)
  }
  invisible(TRUE)
}

#' Versions of the R package, the Python package and the bridge protocol
#'
#' @return A list of class `ss_version` with `r_package`, `python_package`,
#'   `protocol`, `code_fp` (fingerprint of the Python code that runs),
#'   `python` (interpreter version), `executable` and `source` (why that
#'   interpreter was chosen, see [ss_use_python()]).
#' @examples
#' \dontrun{
#' ss_version()
#' }
#' @export
ss_version <- function() {
  py <- ss_python()
  caps <- .ss_hello()
  structure(list(
    r_package = .ss_pkg_version(),
    python_package = caps$spacescans$version,
    required_python_package = .ss_expected()$version,
    protocol = as.integer(caps$protocol),
    code_fp = caps$spacescans$code_fp,
    python = caps$python$version,
    executable = py$path,
    source = py$source
  ), class = "ss_version")
}

#' @export
print.ss_version <- function(x, ...) {
  cat("spacescans (R)      ", x$r_package, "\n", sep = "")
  cat("spacescans (Python) ", x$python_package, if (!identical(x$python_package, x$required_python_package))
    paste0("  (R package needs ", x$required_python_package, ")"), "\n", sep = "")
  cat("bridge protocol     ", x$protocol, "\n", sep = "")
  cat("code fingerprint    ", x$code_fp, "\n", sep = "")
  cat("Python              ", x$python, " at ", x$executable, " [", x$source, "]\n", sep = "")
  invisible(x)
}
