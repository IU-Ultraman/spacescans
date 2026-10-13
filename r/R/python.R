#' Choose the Python interpreter spacescans uses
#'
#' The interpreter is chosen in this order, and the first one found is used:
#' 1. the one set by `ss_use_python()` in this session;
#' 2. the environment variable `SPACESCANS_PYTHON` (a `python` executable or a
#'    virtual environment directory; IT can set it in `Renviron.site`);
#' 3. the environment built by [ss_install()].
#'
#' The `python` on `PATH`, conda environments and reticulate's choice are
#' never used implicitly.
#'
#' @param python Path to a `python` executable, or to a virtual environment
#'   directory (its `bin/python` is used).
#' @param check If `TRUE` (default), start the bridge once to make sure the
#'   interpreter has a compatible `spacescans` package, and fail otherwise.
#' @return `ss_use_python()` invisibly returns the bridge's capabilities
#'   (or `NULL` when `check = FALSE`). `ss_python()` returns a list with
#'   `path` and `source` (why it was chosen).
#' @examples
#' \dontrun{
#' ss_use_python("~/venvs/spacescans/bin/python")
#' ss_python()
#' }
#' @export
ss_use_python <- function(python, check = TRUE) {
  path <- .ss_python_exe(python)
  old <- .ss_state$python
  .ss_state$python <- list(path = path, source = "ss_use_python()")
  .ss_state$hello <- NULL
  if (!isTRUE(check)) return(invisible(NULL))
  caps <- tryCatch(.ss_hello(refresh = TRUE), error = function(e) {
    .ss_state$python <- old
    stop(e)
  })
  invisible(caps)
}

#' @rdname ss_use_python
#' @export
ss_python <- function() {
  if (!is.null(.ss_state$python)) return(.ss_state$python)
  env <- Sys.getenv("SPACESCANS_PYTHON", "")
  if (nzchar(env)) {
    return(list(path = .ss_python_exe(env, what = "SPACESCANS_PYTHON"), source = "SPACESCANS_PYTHON"))
  }
  envdir <- .ss_envdir()
  py <- file.path(envdir, "bin", "python")
  if (file.exists(py)) return(list(path = py, source = "ss_install()"))
  .ss_abort("python",
    paste0(
      "No Python environment for spacescans is configured.\n",
      "Looked for: ss_use_python(), the SPACESCANS_PYTHON environment variable, and ",
      "the ss_install() environment at ", envdir, "."
    ),
    hint = "Run ss_install() once, or point ss_use_python() at an existing environment."
  )
}

.ss_python_exe <- function(python, what = "python") {
  if (!is.character(python) || length(python) != 1L || is.na(python) || !nzchar(python)) {
    .ss_abort("python", sprintf("`%s` must be a single path.", what))
  }
  path <- path.expand(python)
  if (dir.exists(path)) path <- file.path(path, "bin", "python")
  if (!file.exists(path)) {
    .ss_abort("python", sprintf("Python interpreter not found: %s (from %s).", path, what))
  }
  .ss_abs_path(path)
}

# Absolute path WITHOUT resolving the last symlink: a venv's bin/python is a
# symlink to the base interpreter, and Python finds the venv (pyvenv.cfg)
# only through the path it was started with. normalizePath() would lose it.
.ss_abs_path <- function(path) {
  file.path(normalizePath(dirname(path), mustWork = TRUE), basename(path))
}

.ss_envdir <- function() {
  opt <- getOption("spacescans.envdir")
  if (!is.null(opt)) return(.ss_absolute(opt))
  env <- Sys.getenv("SPACESCANS_ENVDIR", "")
  if (nzchar(env)) return(.ss_absolute(env))
  file.path(tools::R_user_dir("spacescans", "data"), "r-spacescans")
}

# Make a path absolute (it may not exist yet). The bridge runs in its own
# working directory, so relative paths must never reach it.
.ss_absolute <- function(path) {
  path <- path.expand(path)
  if (!startsWith(path, "/")) path <- file.path(getwd(), path)
  # Canonicalise the deepest directory that exists (macOS: /var -> /private/var)
  # and keep the rest, including the last component, as given.
  parent <- dirname(path)
  tail <- basename(path)
  while (!dir.exists(parent) && dirname(parent) != parent) {
    tail <- c(basename(parent), tail)
    parent <- dirname(parent)
  }
  do.call(file.path, as.list(c(normalizePath(parent), tail)))
}

# Paths inside iCloud Drive, OneDrive, Dropbox, Box or Google Drive folders.
.ss_is_cloud_synced <- function(path) {
  if (is.null(path) || !nzchar(path)) return(FALSE)
  p <- path.expand(path)
  # Resolve symlinks in the deepest part that exists, keep the rest as given.
  tail <- character()
  while (!file.exists(p) && dirname(p) != p) {
    tail <- c(basename(p), tail)
    p <- dirname(p)
  }
  p <- do.call(file.path, as.list(c(normalizePath(p, winslash = "/", mustWork = FALSE), tail)))
  home <- normalizePath(path.expand("~"), winslash = "/", mustWork = FALSE)
  roots <- file.path(home, "Library", c("Mobile Documents", "CloudStorage"))
  if (any(startsWith(p, roots))) return(TRUE)
  parts <- strsplit(p, "/", fixed = TRUE)[[1]]
  any(grepl("^(OneDrive|Dropbox|Box|Google Drive|iCloud Drive)( |-|$)", parts))
}
