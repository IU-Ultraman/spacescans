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
#'    resolve. Only binary wheels are accepted for the dependencies, so
#'    nothing is compiled;
#' 4. run the bridge once and write `env.json` in the environment.
#'
#' The wheel is looked for in this order: the `wheel` argument, the wheel in
#' the package's `python/` directory (checked against `pin.json`), the
#' `wheelhouse` directory, and the URL recorded in `pin.json` (downloaded and
#' checked against its SHA-256). When there is none, as in an R package
#' installed from GitHub with `remotes::install_github()` or pak, or from
#' r-universe, `spacescans-pipeline` is installed from the source archive of
#' the same GitHub commit, which those tools record in the installed package's
#' `RemoteSha` field (r-universe records the repository as `RemoteUrl`; without
#' a recorded commit, the tag of this package's version, `v<version>`, is
#' used). The pipeline is pure Python, so building it compiles nothing.
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
  src <- .ss_find_wheel(wheel, wheelhouse, offline)
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
  # A GitHub source archive is the one distribution uv may build (pure Python).
  common <- c("pip", "install", "--python", py, "--only-binary", ":all:", "--compile-bytecode", net,
              if (identical(src$kind, "github")) c("--no-binary", "spacescans-pipeline"))
  lock <- .ss_lock_file()
  if (!is.null(lock)) {
    reqs <- .ss_lock_requirements(lock, extras, file.path(tempdir(), "spacescans-requirements.txt"))
    on.exit(unlink(reqs), add = TRUE)
    run_uv(c(common, "--require-hashes", "-r", reqs))
    run_uv(c(common, "--no-deps", .ss_requirement(src)))
  } else {
    run_uv(c(common, .ss_requirement(src, extras)))
  }
  .ss_state$hello <- NULL
  caps <- .ss_bridge("capabilities", python = py, check_version = FALSE)
  .ss_check_versions(caps, list(path = py, source = "ss_install()"))
  info <- list(
    python = py, envdir = envdir, installed_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%S%z"),
    spacescans = caps$spacescans$version, code_fp = caps$spacescans$code_fp,
    python_version = caps$python$version,
    wheel = if (identical(src$kind, "wheel")) basename(src$path),
    wheel_sha256 = if (identical(src$kind, "wheel")) .ss_sha256(src$path),
    source = if (identical(src$kind, "github")) src$url,
    source_commit = if (identical(src$kind, "github")) src$commit,
    lock = if (!is.null(lock)) basename(lock),
    lock_sha256 = if (!is.null(lock)) .ss_sha256(lock),
    extras = names(Filter(function(e) isTRUE(e$available), caps$extras)),
    uv = uv, uv_version = uv_version, r_package = .ss_pkg_version()
  )
  writeLines(jsonlite::toJSON(info, auto_unbox = TRUE, pretty = TRUE, null = "null"),
             file.path(envdir, "env.json"))
  if (!quiet) {
    from <- if (identical(src$kind, "github")) sprintf(" (from GitHub %s@%s)", src$repo, substr(src$commit, 1, 12)) else ""
    message(sprintf("spacescans %s is ready in %s%s", caps$spacescans$version, envdir, from))
  }
  # Later calls look for the environment at .ss_envdir(); one built elsewhere
  # is used for the rest of this session, and the message says how to keep it.
  if (!identical(envdir, .ss_absolute(.ss_envdir()))) {
    .ss_state$python <- list(path = py, source = "ss_install()")
    if (!quiet) message(sprintf('Using it in this session. In new sessions, set options(spacescans.envdir = "%s").', envdir))
  }
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
  if (nzchar(path) && file.exists(sub("\\.txt$", ".groups.json", path))) path else NULL
}

.ss_norm_pkg <- function(x) gsub("[_.]+", "-", tolower(x))

# Write the lock entries (requirement line plus its --hash lines) for the base
# install and the requested extras to `dest`. The groups file next to the lock
# (written by tools/lock.sh) says which packages each extra adds.
.ss_lock_requirements <- function(lock, extras, dest) {
  groups <- jsonlite::read_json(sub("\\.txt$", ".groups.json", lock), simplifyVector = TRUE)
  if ("all" %in% extras) extras <- setdiff(names(groups), "base")
  unknown <- setdiff(extras, names(groups))
  if (length(unknown)) {
    .ss_abort("install", sprintf("The lock has no group for extra(s): %s.", paste(unknown, collapse = ", ")))
  }
  wanted <- .ss_norm_pkg(unique(unlist(groups[c("base", extras)], use.names = FALSE)))
  lines <- readLines(lock, warn = FALSE)
  starts <- grep("^[A-Za-z0-9]", lines)
  block <- findInterval(seq_along(lines), starts)
  keep_block <- .ss_norm_pkg(sub("[=<>!~ ;\\[].*$", "", lines[starts])) %in% wanted
  keep <- block > 0 & keep_block[pmax(block, 1L)]
  found <- .ss_norm_pkg(sub("[=<>!~ ;\\[].*$", "", lines[starts][keep_block]))
  if (length(missing <- setdiff(wanted, found))) {
    .ss_abort("install", sprintf("The lock lacks pinned entries for: %s.", paste(missing, collapse = ", ")))
  }
  writeLines(lines[keep], dest)
  dest
}

# Where ss_install() gets spacescans-pipeline: a wheel (`kind = "wheel"`,
# `path`) or, failing that, the GitHub source archive of the commit this R
# package was installed from (`kind = "github"`, see .ss_github_source()).
.ss_find_wheel <- function(wheel = NULL, wheelhouse = NULL, offline = FALSE,
                           github = .ss_github_source()) {
  want <- .ss_expected()$version
  if (!is.null(wheel)) {
    if (!file.exists(wheel)) .ss_abort("install", sprintf("Wheel not found: %s", wheel))
    return(.ss_wheel_source(normalizePath(wheel)))
  }
  pin <- .ss_pin()
  bundled_dir <- system.file("python", package = "spacescans")
  if (!is.null(pin$wheel) && nzchar(bundled_dir)) {
    path <- file.path(bundled_dir, pin$wheel)
    if (file.exists(path)) {
      if (!identical(.ss_sha256(path), pin$sha256)) {
        .ss_abort("install", sprintf("The bundled wheel %s does not match the SHA-256 in pin.json.", path))
      }
      return(.ss_wheel_source(normalizePath(path)))
    }
  }
  if (!is.null(wheelhouse)) {
    found <- list.files(wheelhouse, pattern = sprintf("^spacescans_pipeline-%s-.*\\.whl$", gsub(".", "\\.", want, fixed = TRUE)),
                        full.names = TRUE)
    if (length(found)) return(.ss_wheel_source(normalizePath(found[1])))
  }
  if (!is.null(pin$url) && !isTRUE(offline)) {
    dest <- file.path(tempdir(), pin$wheel %||% basename(pin$url))
    utils::download.file(pin$url, dest, mode = "wb", quiet = TRUE)
    if (!identical(.ss_sha256(dest), pin$sha256)) {
      unlink(dest)
      .ss_abort("install", sprintf("The wheel downloaded from %s does not match the SHA-256 in pin.json.", pin$url))
    }
    return(.ss_wheel_source(dest))
  }
  if (!isTRUE(offline) && !is.null(github)) return(github)
  .ss_abort("install",
    sprintf("No spacescans %s wheel is bundled with this R package.", want),
    hint = paste0("Install the R package with remotes::install_github(\"IU-Ultraman/spacescans\", ",
                  "subdir = \"r\") or from r-universe so ss_install() can fetch the matching ",
                  "Python package, or pass ",
                  "ss_install(wheel = \"spacescans_pipeline-", want, "-py3-none-any.whl\") ",
                  "built with r/tools/build-wheel.sh."))
}

.ss_wheel_source <- function(path) list(kind = "wheel", path = path)

# The GitHub commit this R package was installed from, or NULL for any other
# kind of install. Two ways of recording it in the installed DESCRIPTION:
# * remotes and pak: RemoteType "github" with RemoteUsername, RemoteRepo and
#   RemoteSha (older remotes wrote GithubSHA1 and friends);
# * r-universe (and remotes::install_git() of a GitHub URL): no RemoteType or
#   a git one, and only RemoteUrl, RemoteRef and RemoteSha; the owner and the
#   repository come from RemoteUrl, which must be a github.com repository URL.
#   Without a commit, the tag of this package's version (v<version>) is used.
.ss_github_source <- function(desc = suppressWarnings(utils::packageDescription("spacescans")),
                              version = .ss_desc_version(desc)) {
  if (!is.list(desc)) return(NULL)
  pick <- function(...) {
    for (k in c(...)) if (!is.null(desc[[k]]) && !is.na(desc[[k]]) && nzchar(desc[[k]])) return(desc[[k]])
    NULL
  }
  type <- pick("RemoteType") %||% (if (!is.null(pick("GithubSHA1"))) "github")
  sha <- pick("RemoteSha", "GithubSHA1")
  if (identical(type, "github")) {
    user <- pick("RemoteUsername", "GithubUsername")
    repo <- pick("RemoteRepo", "GithubRepo")
    host <- pick("RemoteHost") %||% "api.github.com"
    if (is.null(sha) || is.null(user) || is.null(repo)) return(NULL)
    if (!grepl("^[0-9a-f]{40}$", sha) || !host %in% c("api.github.com", "github.com")) return(NULL)
  } else {
    if (!is.null(type) && !type %in% c("git", "git2r", "xgit")) return(NULL)
    where <- .ss_github_repo_url(pick("RemoteUrl"))
    if (is.null(where)) return(NULL)
    user <- where[["user"]]
    repo <- where[["repo"]]
    if (is.null(sha)) {
      if (is.null(version) || is.na(version) || !grepl("^[0-9]+(\\.[0-9]+)*$", version)) return(NULL)
      tag <- paste0("v", version)
      url <- sprintf("https://github.com/%s/%s/archive/refs/tags/%s.tar.gz", user, repo, tag)
      return(list(kind = "github", url = url, commit = tag, repo = paste(user, repo, sep = "/")))
    }
    if (!grepl("^[0-9a-f]{40}$", sha)) return(NULL)
  }
  url <- sprintf("https://github.com/%s/%s/archive/%s.tar.gz", user, repo, sha)
  list(kind = "github", url = url, commit = sha, repo = paste(user, repo, sep = "/"))
}

# Owner and repository of a github.com repository URL (https, optional ".git" or
# trailing slash), or NULL for anything else.
.ss_github_repo_url <- function(url) {
  if (is.null(url) || length(url) != 1L || is.na(url)) return(NULL)
  m <- regmatches(url, regexec("^https://(www\\.)?github\\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(\\.git)?/?$",
                               url, perl = TRUE))[[1]]
  if (!length(m)) return(NULL)
  list(user = m[[3]], repo = m[[4]])
}

# The Python package version this R package expects, from a DESCRIPTION.
.ss_desc_version <- function(desc) {
  if (!is.list(desc)) return(NULL)
  desc[["Config/spacescans/version"]] %||% desc[["Version"]]
}

# What uv installs for a source, with the requested extras.
.ss_requirement <- function(src, extras = character()) {
  ext <- if (length(extras)) sprintf("[%s]", paste(extras, collapse = ",")) else ""
  if (identical(src$kind, "github")) sprintf("spacescans-pipeline%s @ %s", ext, src$url) else paste0(src$path, ext)
}
