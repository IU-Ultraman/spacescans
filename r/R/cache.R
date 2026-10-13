# The C3 weight cache used by ss_link(cache = TRUE): v2 keys under
# tools::R_user_dir("spacescans", "cache"); never shared with the web app.

.ss_cache_root <- function() {
  opt <- getOption("spacescans.cache_dir")
  if (!is.null(opt)) return(.ss_absolute(opt))
  tools::R_user_dir("spacescans", "cache")
}

.ss_ensure_private_dir <- function(path) {
  if (!dir.exists(path)) dir.create(path, recursive = TRUE, mode = "0700")
  Sys.chmod(path, "0700", use_umask = FALSE)
  normalizePath(path)
}

# The cache_dir argument for link: NULL (no cache) or the root, created 0700.
.ss_cache_arg <- function(cache) {
  if (isFALSE(cache) || is.null(cache)) return(NULL)
  if (isTRUE(cache)) return(.ss_ensure_private_dir(.ss_cache_root()))
  if (is.character(cache) && length(cache) == 1L && !is.na(cache) && nzchar(cache)) {
    return(.ss_ensure_private_dir(.ss_absolute(cache)))
  }
  .ss_abort("config", "`cache` must be TRUE, FALSE or a directory path.")
}

#' The C3 weight cache
#'
#' [ss_link()] keeps the C3 weights it computes (which census units, grid
#' cells or features each residential buffer overlaps) in the user cache
#' directory, so linking the same cohort with the same buffer again skips
#' the slowest steps. Each entry is keyed by the cohort's coordinates, the
#' buffer and raster settings, the code and data fingerprints and the
#' native library versions; a change in any of them is a miss. The entries
#' can reveal where cohort members lived, so the directory is private
#' (0700) and you may want to clear it when a project ends.
#'
#' @param older_than Remove only entries older than this: a number of days
#'   or a [difftime]. `NULL` (default) removes every entry.
#' @param cache_dir The cache root (default: option `spacescans.cache_dir`,
#'   then `tools::R_user_dir("spacescans", "cache")`).
#' @return `ss_cache_info()`: a list with `cache_dir`, `entries`, `bytes`,
#'   `oldest`, `newest`, and `temp_files`/`temp_bytes` (copies left by
#'   writes that were interrupted; they hold weights too and
#'   `ss_cache_clear()` removes them). `ss_cache_clear()`: invisibly, what
#'   was removed.
#' @examples
#' \dontrun{
#' ss_cache_info()
#' ss_cache_clear(older_than = 90)
#' }
#' @export
ss_cache_info <- function(cache_dir = NULL) {
  root <- .ss_absolute(cache_dir %||% .ss_cache_root())
  value <- .ss_bridge("cache-info", list(cache_dir = root))
  attr(value, "spacescans") <- NULL
  value$cache_dir <- value$cache_dir %||% root
  structure(value, class = c("ss_cache_info", "list"))
}

#' @export
print.ss_cache_info <- function(x, ...) {
  cat("spacescans C3 cache at ", x$cache_dir, "\n", sep = "")
  cat("  entries  ", format(x$entries %||% 0), "\n", sep = "")
  cat("  size     ", .ss_format_bytes(x$bytes %||% 0), "\n", sep = "")
  if (!is.null(x$oldest)) cat("  oldest   ", format(x$oldest), "\n", sep = "")
  if (!is.null(x$newest)) cat("  newest   ", format(x$newest), "\n", sep = "")
  if (isTRUE((x$temp_files %||% 0) > 0)) {
    cat("  leftover ", format(x$temp_files), " temp file(s) of interrupted writes, ",
        .ss_format_bytes(x$temp_bytes %||% 0), " (ss_cache_clear() removes them)\n", sep = "")
  }
  invisible(x)
}

#' @rdname ss_cache_info
#' @export
ss_cache_clear <- function(older_than = NULL, cache_dir = NULL) {
  secs <- NULL
  if (!is.null(older_than)) {
    secs <- if (inherits(older_than, "difftime")) {
      as.numeric(older_than, units = "secs")
    } else if (is.numeric(older_than) && length(older_than) == 1L && !is.na(older_than) && older_than >= 0) {
      older_than * 86400
    } else {
      .ss_abort("config", "`older_than` must be a number of days or a difftime.")
    }
  }
  root <- .ss_absolute(cache_dir %||% .ss_cache_root())
  value <- .ss_bridge("cache-clear", list(cache_dir = root, older_than_s = secs))
  attr(value, "spacescans") <- NULL
  invisible(value)
}
