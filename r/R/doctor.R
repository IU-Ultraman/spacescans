#' Check the spacescans setup
#'
#' Runs local checks and one bridge handshake, and reports each as `ok`,
#' `warn` or `fail`: which interpreter is used and why, versions and protocol,
#' Python extras, GDAL/GEOS/PROJ, macOS version and architecture, memory,
#' machine-wide `PROJ_*`/`GDAL_DATA` variables (they are not passed to
#' Python), whether the data, cache, environment or temporary directories sit
#' in a cloud-synced folder, free disk space, the run directories under
#' `tools::R_user_dir("spacescans", "cache")/runs` (those whose owner process
#' no longer exists are removed, since they hold cohort data), and (when a
#' data directory is known) how many catalog variables have their data in
#' place ([ss_data_status()]).
#'
#' @param data_dir Data directory to check. Default: [ss_data_dir()] when
#'   it is set.
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
  data_dir <- data_dir %||% .ss_data_dir_or_null()
  dirs <- list(`data directory` = data_dir, `cache directory` = .ss_cache_root(),
               `Python environment` = .ss_envdir(), `temporary directory` = tempdir())
  for (label in names(dirs)) {
    d <- dirs[[label]]
    if (is.null(d)) {
      add(label, "warn", "not set; use ss_data_dir(\"~/ss-data\") or the SPACESCANS_DATA_DIR environment variable")
      next
    }
    if (.ss_is_cloud_synced(d)) {
      add(label, "warn", sprintf("%s is in a cloud-synced folder; keep data, caches and run files local", d))
    } else {
      optional <- label %in% c("Python environment", "cache directory")
      add(label, if (dir.exists(d) || optional) "ok" else "warn",
          sprintf("%s%s", d, if (dir.exists(d)) "" else if (optional) " (created on first use)" else " (does not exist)"))
    }
  }
  sw <- tryCatch(.ss_sweep_runs(), error = function(e) list(removed = character(), kept = character(),
                                                               unknown = character(), errors = conditionMessage(e)))
  add("run directories", if (length(sw$errors)) "warn" else "ok",
      sprintf("%s: %d in use, %d removed (owner process gone)%s%s", .ss_runs_root(), length(sw$kept),
              length(sw$removed),
              if (length(sw$unknown)) sprintf(", %d left alone (owner unknown)", length(sw$unknown)) else "",
              if (length(sw$errors)) sprintf("; could not remove %s", paste(sw$errors, collapse = ", ")) else ""))
  if (!is.null(data_dir) && dir.exists(data_dir)) {
    free <- .ss_free_bytes(data_dir)
    if (!is.na(free)) add("disk space", if (free >= 50e9) "ok" else "warn",
                          sprintf("%s free in the data directory%s", .ss_format_bytes(free),
                                  if (free < 50e9) "; the full catalog needs about 50 GB or more" else ""))
  }
  if (!is.null(caps) && !is.null(data_dir) && "data-status" %in% unlist(caps$commands)) {
    st <- tryCatch(ss_data_status(data_dir = data_dir), error = function(e) e)
    if (inherits(st, "error")) {
      add("data", "warn", conditionMessage(st))
    } else {
      ready <- sum(st$usable %in% TRUE)
      add("data", if (ready == nrow(st)) "ok" else "warn",
          sprintf("%d of %d catalog variables have their data in place%s", ready, nrow(st),
                  if (ready < nrow(st)) "; ss_data_status() lists the rest" else ""))
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

.ss_free_bytes <- function(path) {
  out <- tryCatch(system2("df", c("-k", shQuote(path)), stdout = TRUE, stderr = FALSE),
                  error = function(e) character(), warning = function(w) character())
  if (length(out) < 2L) return(NA_real_)
  fields <- strsplit(trimws(out[length(out)]), "[[:space:]]+")[[1]]
  avail <- suppressWarnings(as.numeric(fields[4]))
  if (is.na(avail)) NA_real_ else avail * 1024
}
