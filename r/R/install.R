#' Install the Python side of spacescans
#'
#' Builds (or updates) a private virtual environment with `uv` and installs
#' the `spacescans-pipeline` wheel that matches this R package, then checks
#' it with the bridge. Nothing is downloaded or installed outside `envdir`
#' and uv's cache, and the Python on `PATH` is never touched.
#'
#' Steps:
#' 1. find `uv`: the `uv` argument, the `SPACESCANS_UV` environment variable,
#'    `PATH`, `~/.local/bin/uv`, `/opt/homebrew/bin/uv`;
#' 2. create the environment with `uv venv --python <python>` unless it
#'    exists (uv downloads a managed CPython if needed; set
#'    `UV_PYTHON_DOWNLOADS=never` and pass `python =` an interpreter path on
#'    locked-down machines);
#' 3. install the dependencies from the bundled hash-pinned lock file when
#'    there is one for this platform (`inst/python/locks/`), then the wheel
#'    with `--no-deps`; without a lock, install `wheel[extras]` and let uv
#'    resolve. Only binary wheels are accepted, so nothing is compiled;
#' 4. run the bridge once and write `env.json` in the environment.
#'
#' The wheel is looked for in this order: the `wheel` argument, the wheel in
#' the package's `python/` directory (checked against `pin.json`), the
#' `wheelhouse` directory, and the URL recorded in `pin.json` (downloaded and
#' checked against its SHA-256).
#'
#' @param extras Python extras to add: any of `"geo"`, `"rda"`, `"nc"`,
#'   `"hdf4"` (TEMIS needs `"hdf4"`). Extras already installed stay installed.
#' @param python Python version (`"3.12"`) or path to an interpreter.
#' @param envdir Environment directory. Default: option `spacescans.envdir`,
#'   then the environment variable `SPACESCANS_ENVDIR`, then
#'   `tools::R_user_dir("spacescans", "data")/r-spacescans`.
#' @param wheel Path to a `spacescans_pipeline-*.whl` to install.
#' @param wheelhouse Directory of wheels to install from (offline sites).
#' @param offline If `TRUE`, never use the network (needs `wheelhouse` or
#'   uv's cache).
#' @param uv Path to the `uv` executable.
#' @param force If `TRUE`, delete and rebuild the environment.
#' @param quiet If `TRUE`, do not echo uv's output.
#' @return Invisibly, the bridge's capabilities for the new environment.
#' @examples
#' \dontrun{
#' ss_install()
#' ss_install(extras = "hdf4")   # add TEMIS support
#' }
#' @export
ss_install <- function(extras = c("geo", "rda", "nc"), python = "3.12", envdir = NULL,
                       wheel = NULL, wheelhouse = NULL, offline = FALSE, uv = NULL,
                       force = FALSE, quiet = FALSE) {
  extras <- unique(as.character(extras))
  bad <- setdiff(extras, c("geo", "rda", "nc", "hdf4", "all"))
  if (length(bad)) .ss_abort("config", sprintf("Unknown extra(s): %s.", paste(bad, collapse = ", ")),
                             hint = 'Use any of "geo", "rda", "nc", "hdf4".')
  envdir <- .ss_absolute(envdir %||% .ss_envdir())
  if (.ss_is_cloud_synced(envdir)) {
    .ss_abort("install", sprintf("Refusing to build the Python environment inside a cloud-synced folder: %s", envdir),
              hint = "Choose a local directory with ss_install(envdir = ...).")
  }
  uv <- .ss_find_uv(uv)
  whl <- .ss_find_wheel(wheel, wheelhouse, offline)
  # Use the system certificate store, so TLS-inspecting institutional proxies work.
  uv_help <- tryCatch(processx::run(uv, "--help", error_on_status = FALSE)$stdout, error = function(e) "")
  tls <- if (grepl("system-certs", uv_help, fixed = TRUE)) c(UV_SYSTEM_CERTS = "1") else c(UV_NATIVE_TLS = "1")
  uv_version <- tryCatch(trimws(processx::run(uv, "--version")$stdout), error = function(e) NA_character_)
  uv_env <- c(Sys.getenv()[setdiff(names(Sys.getenv()), c(.ss_drop_env, "UV_NATIVE_TLS"))], tls)
  run_uv <- function(args) {
    res <- tryCatch(
      processx::run(uv, args, echo = !quiet, error_on_status = FALSE, env = uv_env,
                    stderr_to_stdout = TRUE),
      error = function(e) .ss_abort("install", sprintf("Cannot run uv (%s): %s", uv, conditionMessage(e))))
    if (res$status != 0) {
      .ss_abort("install", sprintf("uv %s failed with status %d.\n%s", args[1], res$status,
                                   paste(utils::tail(strsplit(res$stdout, "\n")[[1]], 30), collapse = "\n")))
    }
    res
  }

  if (isTRUE(force) && dir.exists(envdir)) unlink(envdir, recursive = TRUE)
  py <- file.path(envdir, "bin", "python")
  if (!file.exists(py)) {
    dir.create(dirname(envdir), recursive = TRUE, showWarnings = FALSE, mode = "0700")
    run_uv(c("venv", envdir, "--python", python, if (isTRUE(offline)) "--offline"))
  }
  net <- c(if (isTRUE(offline)) c("--offline", "--no-index"),
           if (!is.null(wheelhouse)) c("--find-links", normalizePath(wheelhouse)))
  common <- c("pip", "install", "--python", py, "--only-binary", ":all:", "--compile-bytecode", net)
  lock <- .ss_lock_file()
  if (!is.null(lock)) {
    run_uv(c(common, "--require-hashes", "-r", lock))
    run_uv(c(common, "--no-deps", whl))
  } else {
    spec <- if (length(extras)) sprintf("%s[%s]", whl, paste(extras, collapse = ",")) else whl
    run_uv(c(common, spec))
  }
  .ss_state$hello <- NULL
  caps <- .ss_bridge("capabilities", python = py, check_version = FALSE)
  .ss_check_versions(caps, list(path = py, source = "ss_install()"))
  info <- list(
    python = py, envdir = envdir, installed_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%S%z"),
    spacescans = caps$spacescans$version, code_fp = caps$spacescans$code_fp,
    python_version = caps$python$version, wheel = basename(whl),
    wheel_sha256 = .ss_sha256(whl), lock = if (!is.null(lock)) basename(lock),
    lock_sha256 = if (!is.null(lock)) .ss_sha256(lock),
    extras = names(Filter(function(e) isTRUE(e$available), caps$extras)),
    uv = uv, uv_version = uv_version, r_package = .ss_pkg_version()
  )
  writeLines(jsonlite::toJSON(info, auto_unbox = TRUE, pretty = TRUE, null = "null"),
             file.path(envdir, "env.json"))
  if (!quiet) message(sprintf("spacescans %s is ready in %s", caps$spacescans$version, envdir))
  invisible(caps)
}

.ss_find_uv <- function(uv = NULL) {
  candidates <- c(uv, Sys.getenv("SPACESCANS_UV", ""), Sys.which("uv"),
                  path.expand("~/.local/bin/uv"), "/opt/homebrew/bin/uv", "/usr/local/bin/uv")
  candidates <- candidates[nzchar(candidates)]
  for (c in candidates) if (file.exists(c)) return(normalizePath(c))
  .ss_abort("install", "uv was not found.",
            hint = paste0("Install uv (https://docs.astral.sh/uv/getting-started/installation/), ",
                          "or pass ss_install(uv = \"/path/to/uv\")."))
}

.ss_sha256 <- function(path) {
  if (exists("sha256sum", envir = asNamespace("tools"), inherits = FALSE)) {
    return(unname(tools::sha256sum(path)))
  }
  out <- system2("shasum", c("-a", "256", shQuote(path)), stdout = TRUE)
  sub(" .*$", "", out[1])
}

.ss_lock_file <- function() {
  if (!identical(Sys.info()[["sysname"]], "Darwin") || !identical(R.version$arch, "aarch64")) return(NULL)
  path <- system.file("python", "locks", "macos-arm64-py312.txt", package = "spacescans")
  if (nzchar(path)) path else NULL
}

.ss_find_wheel <- function(wheel = NULL, wheelhouse = NULL, offline = FALSE) {
  want <- .ss_expected()$version
  if (!is.null(wheel)) {
    if (!file.exists(wheel)) .ss_abort("install", sprintf("Wheel not found: %s", wheel))
    return(normalizePath(wheel))
  }
  pin <- .ss_pin()
  bundled_dir <- system.file("python", package = "spacescans")
  if (!is.null(pin$wheel) && nzchar(bundled_dir)) {
    path <- file.path(bundled_dir, pin$wheel)
    if (file.exists(path)) {
      if (!identical(.ss_sha256(path), pin$sha256)) {
        .ss_abort("install", sprintf("The bundled wheel %s does not match the SHA-256 in pin.json.", path))
      }
      return(normalizePath(path))
    }
  }
  if (!is.null(wheelhouse)) {
    found <- list.files(wheelhouse, pattern = sprintf("^spacescans_pipeline-%s-.*\\.whl$", gsub(".", "\\.", want, fixed = TRUE)),
                        full.names = TRUE)
    if (length(found)) return(normalizePath(found[1]))
  }
  if (!is.null(pin$url) && !isTRUE(offline)) {
    dest <- file.path(tempdir(), pin$wheel %||% basename(pin$url))
    utils::download.file(pin$url, dest, mode = "wb", quiet = TRUE)
    if (!identical(.ss_sha256(dest), pin$sha256)) {
      unlink(dest)
      .ss_abort("install", sprintf("The wheel downloaded from %s does not match the SHA-256 in pin.json.", pin$url))
    }
    return(dest)
  }
  .ss_abort("install",
    sprintf("No spacescans %s wheel is bundled with this R package.", want),
    hint = paste0("Build it from the repository with r/tools/build-wheel.sh, or pass ",
                  "ss_install(wheel = \"spacescans_pipeline-", want, "-py3-none-any.whl\")."))
}
