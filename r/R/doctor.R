#' Check the spacescans setup
#'
#' Runs local checks and one bridge handshake, and reports each as `ok`,
#' `warn` or `fail`: which interpreter is used and why, versions and protocol,
#' Python extras, GDAL/GEOS/PROJ, macOS version and architecture, memory,
#' machine-wide `PROJ_*`/`GDAL_DATA` variables (they are not passed to
#' Python), and whether the data, environment or temporary directories sit in
#' a cloud-synced folder. Data checks (`ss_data_status()`) join in phase B.
#'
#' @param data_dir Data directory to check. Default: the `SPACESCANS_DATA_DIR`
#'   environment variable, if set.
#' @return Invisibly, a data frame of class `ss_doctor` with columns `check`,
#'   `status` and `message`; it is printed.
#' @examples
#' \dontrun{
#' ss_doctor()
#' }
#' @export
ss_doctor <- function(data_dir = NULL) {
  rows <- list()
  add <- function(check, status, message) {
    rows[[length(rows) + 1L]] <<- data.frame(check = check, status = status, message = message,
                                             stringsAsFactors = FALSE)
  }
  add("R", "ok", sprintf("R %s on %s; spacescans (R) %s", getRversion(), R.version$platform, .ss_pkg_version()))
  sys <- Sys.info()
  if (identical(sys[["sysname"]], "Darwin")) {
    mac <- tryCatch(system2("sw_vers", "-productVersion", stdout = TRUE), error = function(e) NA_character_)
    major <- suppressWarnings(as.integer(sub("\\..*$", "", mac)))
    arm <- identical(sys[["machine"]], "arm64")
    add("macOS", if (!is.na(major) && major >= 14 && arm) "ok" else "warn",
        sprintf("macOS %s on %s%s", mac, sys[["machine"]],
                if (!is.na(major) && major >= 14 && arm) "" else " (supported: macOS 14+ on Apple silicon)"))
    mem <- tryCatch(as.numeric(system2("sysctl", c("-n", "hw.memsize"), stdout = TRUE)), error = function(e) NA)
    if (!is.na(mem)) add("memory", if (mem >= 16 * 2^30) "ok" else "warn",
                         sprintf("%.0f GB RAM%s", mem / 2^30,
                                 if (mem < 16 * 2^30) "; large cohorts and daily sources may not fit" else ""))
  } else {
    add("platform", "warn", sprintf("%s %s: only macOS 14+ on Apple silicon is supported for now",
                                    sys[["sysname"]], sys[["machine"]]))
  }
  leaked <- c("PROJ_DATA", "PROJ_LIB", "GDAL_DATA", "PYTHONPATH", "PYTHONHOME")
  leaked <- leaked[nzchar(Sys.getenv(leaked))]
  if (length(leaked)) add("environment", "warn", sprintf(
    "%s set in this R session; they are removed for the Python process", paste(leaked, collapse = ", ")))

  py <- tryCatch(ss_python(), error = function(e) e)
  caps <- NULL
  if (inherits(py, "error")) {
    add("python", "fail", conditionMessage(py))
  } else {
    add("python", "ok", sprintf("%s (chosen by %s)", py$path, py$source))
    caps <- tryCatch(.ss_hello(refresh = TRUE), error = function(e) e)
    if (inherits(caps, "error")) {
      add("bridge", "fail", conditionMessage(caps))
      caps <- NULL
    } else {
      add("bridge", "ok", sprintf("spacescans %s, protocol %s, Python %s",
                                  caps$spacescans$version, caps$protocol, caps$python$version))
    }
  }
  if (!is.null(caps)) {
    for (name in names(caps$extras)) {
      e <- caps$extras[[name]]
      add(paste0("extra ", name), if (isTRUE(e$available)) "ok" else "warn",
          if (isTRUE(e$available)) "installed"
          else sprintf("missing %s; ss_install(extras = \"%s\")", paste(unlist(e$missing), collapse = ", "), name))
    }
    nat <- caps$native
    libs <- vapply(c("gdal", "geos", "proj"), function(k) as.character(nat[[k]] %||% NA_character_), character(1))
    add("native libraries", if (all(!is.na(libs))) "ok" else "warn",
        paste(sprintf("%s %s", toupper(names(libs)), ifelse(is.na(libs), "unavailable", libs)), collapse = ", "))
    unavailable <- names(caps$registry$unavailable)
    if (length(unavailable)) add("patterns", "warn", sprintf("%d optional module(s) unavailable: %s",
                                                            length(unavailable), paste(basename(gsub(".", "/", unavailable, fixed = TRUE)), collapse = ", ")))
  }
  data_dir <- data_dir %||% (if (nzchar(Sys.getenv("SPACESCANS_DATA_DIR"))) Sys.getenv("SPACESCANS_DATA_DIR"))
  dirs <- list(`data directory` = data_dir, `Python environment` = .ss_envdir(), `temporary directory` = tempdir())
  for (label in names(dirs)) {
    d <- dirs[[label]]
    if (is.null(d)) {
      add(label, "warn", "not set (ss_data_dir() arrives in phase B; set SPACESCANS_DATA_DIR)")
      next
    }
    if (.ss_is_cloud_synced(d)) {
      add(label, "warn", sprintf("%s is in a cloud-synced folder; keep data, caches and run files local", d))
    } else {
      add(label, if (dir.exists(d) || label == "Python environment") "ok" else "warn",
          sprintf("%s%s", d, if (dir.exists(d)) "" else " (does not exist)"))
    }
  }
  out <- do.call(rbind, rows)
  class(out) <- c("ss_doctor", "data.frame")
  print(out)
  invisible(out)
}

#' @export
print.ss_doctor <- function(x, ...) {
  mark <- c(ok = "[ok]  ", warn = "[warn]", fail = "[FAIL]")
  for (i in seq_len(nrow(x))) {
    cat(mark[[x$status[i]]], " ", format(x$check[i], width = 20), " ", x$message[i], "\n", sep = "")
  }
  invisible(x)
}
