#' The data directory
#'
#' Reads or sets the directory that holds the exposure data. There is no
#' implicit default: it is what `ss_data_dir(path)` set in this session
#' (option `spacescans.data_dir`), otherwise the environment variable
#' `SPACESCANS_DATA_DIR`. No data ships with spacescans; [ss_fetch()]
#' downloads it into this directory.
#'
#' @param path A directory (it may not exist yet). `NULL` reads the current
#'   setting.
#' @return The absolute path (invisibly when setting). Reading raises an
#'   error of class `spacescans_error_config` when nothing is set.
#' @examples
#' old <- getOption("spacescans.data_dir")
#' ss_data_dir(file.path(tempdir(), "ss-data"))
#' ss_data_dir()
#' options(spacescans.data_dir = old)
#' @export
ss_data_dir <- function(path = NULL) {
  if (!is.null(path)) {
    if (!is.character(path) || length(path) != 1L || is.na(path) || !nzchar(path)) {
      .ss_abort("config", "`path` must be a single directory path.")
    }
    p <- .ss_absolute(path)
    .ss_warn_cloud(c(data_dir = p))
    options(spacescans.data_dir = p)
    return(invisible(p))
  }
  p <- .ss_data_dir_or_null()
  if (is.null(p)) {
    .ss_abort("config", "No data directory is set.",
              hint = 'Set one with ss_data_dir("~/ss-data") or the SPACESCANS_DATA_DIR environment variable.')
  }
  p
}

.ss_data_dir_or_null <- function() {
  opt <- getOption("spacescans.data_dir")
  if (!is.null(opt) && nzchar(opt)) return(.ss_absolute(opt))
  env <- Sys.getenv("SPACESCANS_DATA_DIR", "")
  if (nzchar(env)) return(.ss_absolute(env))
  NULL
}

# The bridge's "next" commands are written for the shell; show the R call.
# `--accept-terms ID` becomes ss_fetch()'s accept_terms argument.
.ss_r_command <- function(cmd) {
  if (is.null(cmd) || !length(cmd) || is.na(cmd) || !nzchar(cmd)) return("")
  out <- cmd
  terms <- regmatches(out, gregexpr("--accept-terms [A-Za-z0-9_.-]+", out))[[1]]
  out <- gsub(" *--accept-terms [A-Za-z0-9_.-]+", "", out)
  out <- gsub("spacescans data fetch --dataset ([A-Za-z0-9_]+)", 'ss_fetch(datasets = "\\1")', out)
  out <- gsub("spacescans data verify --dataset ([A-Za-z0-9_]+)", 'ss_data_verify(datasets = "\\1")', out)
  out <- gsub("spacescans data import --dataset ([A-Za-z0-9_]+)", 'ss_data_import("\\1", paths = ...)', out)
  # Positional variables: "spacescans data fetch ndi walkability".
  m <- regmatches(out, regexpr("spacescans data fetch( [A-Za-z0-9_]+)+", out))
  if (length(m)) {
    vars <- strsplit(sub("^spacescans data fetch ", "", m), " ", fixed = TRUE)[[1]]
    out <- sub(m, sprintf("ss_fetch(%s)", .ss_deparse_chr(vars)), out, fixed = TRUE)
  }
  out <- gsub("spacescans data fetch", "ss_fetch()", out, fixed = TRUE)
  if (length(terms)) {
    acc <- sprintf("accept_terms = %s", .ss_deparse_chr(sub("^--accept-terms ", "", terms)))
    out <- if (grepl("ss_fetch()", out, fixed = TRUE)) {
      sub("ss_fetch()", sprintf("ss_fetch(%s)", acc), out, fixed = TRUE)
    } else {
      sub("ss_fetch\\(((?:[^()]|\\([^()]*\\))*)\\)", sprintf("ss_fetch(\\1, %s)", acc), out, perl = TRUE)
    }
  }
  out
}

.ss_data_args <- function(variables, datasets, data_dir, ...) {
  if (!is.null(variables) && (!is.character(variables) || anyNA(variables))) {
    .ss_abort("config", "`variables` must be a character vector of variable keys.")
  }
  if (!is.null(datasets) && (!is.character(datasets) || anyNA(datasets))) {
    .ss_abort("config", "`datasets` must be a character vector of dataset ids.")
  }
  c(list(variables = if (!is.null(variables)) I(variables),
         datasets = if (!is.null(datasets)) I(datasets),
         data_dir = .ss_dir_value(data_dir, "data_dir")),
    list(...))
}

#' What is in the data directory
#'
#' For each variable, whether the data it needs is in place and what to do
#' next. Statuses: `ready`, `differs_from_reference` and `unverified` can be
#' used; `incomplete`, `missing`, `action_required` (a provider download by
#' hand, terms, or a file not yet on the mirror) and `corrupt` cannot.
#' Nothing is written.
#'
#' @param variables Variable keys; `NULL` checks every catalog variable.
#' @param data_dir The data directory (default [ss_data_dir()]).
#' @param deep Also hash every file with a recorded checksum (reads the whole
#'   tree; slow).
#' @param datasets Dataset ids to check as well (or instead).
#' @return A data frame of class `ss_data_status` with one row per variable
#'   (`variable`, `status`, `usable`, `datasets`, `download_bytes`,
#'   `disk_bytes`, `next`), and attributes `datasets` (one row per dataset),
#'   `ready`, `next_steps`, `warnings`, `data_dir` and `data_release`.
#' @examples
#' \dontrun{
#' ss_data_status(c("ndi", "walkability"))
#' }
#' @export
ss_data_status <- function(variables = NULL, data_dir = ss_data_dir(), deep = FALSE, datasets = NULL) {
  args <- .ss_data_args(variables, datasets, data_dir, deep = isTRUE(deep))
  value <- .ss_bridge("data-status", args, progress = isTRUE(deep) && interactive(),
                      env = list(SPACESCANS_DATA_DIR = args$data_dir))
  .ss_status_df(value)
}

.ss_status_df <- function(value) {
  vars <- .ss_records_df(value$variables,
                         first = c("variable", "status", "usable", "datasets", "download_bytes",
                                   "disk_bytes", "next"))
  if (nrow(vars) && "next" %in% names(vars)) vars[["next"]] <- vapply(vars[["next"]], .ss_r_command, character(1))
  ds_cols <- c("dataset", "status", "usable", "title", "acquire", "download_bytes", "disk_bytes",
               "present", "expected", "next")
  ds <- .ss_records_df(lapply(value$datasets %||% list(), function(d) d[intersect(ds_cols, names(d))]),
                       first = ds_cols)
  if (nrow(ds) && "next" %in% names(ds)) ds[["next"]] <- vapply(ds[["next"]], .ss_r_command, character(1))
  structure(vars,
            class = c("ss_data_status", "data.frame"),
            datasets = ds,
            ready = isTRUE(value$ready),
            next_steps = value$next_steps %||% list(),
            warnings = .ss_chr(value$warnings),
            data_dir = value$data_dir,
            data_release = value$data_release,
            raw = value)
}

#' @export
print.ss_data_status <- function(x, ...) {
  cat("spacescans data in ", attr(x, "data_dir") %||% "?", " (data release ",
      attr(x, "data_release") %||% "?", ")\n", sep = "")
  if (nrow(x)) {
    show <- as.data.frame(x)[, intersect(c("variable", "status", "download_bytes", "next"), names(x)), drop = FALSE]
    if ("download_bytes" %in% names(show)) show$download_bytes <- vapply(show$download_bytes, .ss_format_bytes, "")
    attributes(show)[c("datasets", "ready", "next_steps", "warnings", "data_dir", "data_release", "raw")] <- NULL
    print(show, row.names = FALSE, right = FALSE)
  }
  for (w in attr(x, "warnings")) cat("! ", w, "\n", sep = "")
  cat(if (isTRUE(attr(x, "ready"))) "All requested data is ready.\n" else "Some data is not ready; see `next`.\n")
  invisible(x)
}

#' Download the data variables need
#'
#' Downloads (resumably) what [ss_data_status()] reports missing. Shared
#' files, such as the block-group boundaries several variables use, are
#' downloaded once. Ctrl-C stops the download and keeps the partial files,
#' so running the same call again resumes. On macOS the computer is kept
#' awake while it runs (`caffeinate`).
#'
#' Some provider datasets require you to accept their terms. In an
#' interactive session you are asked for each one; otherwise pass their ids
#' in `accept_terms` (read them first with [ss_data_terms()]). Terms are
#' accepted for this call only.
#'
#' @param variables Variable keys; `NULL` with `datasets = NULL` fetches
#'   everything the catalog needs.
#' @param datasets Dataset ids, instead of or in addition to `variables`.
#' @param data_dir The data directory (default [ss_data_dir()]).
#' @param accept_terms Ids of provider terms you accept for this download.
#' @param keep_archives Keep the downloaded archives after extraction.
#' @param dry_run Only report what would be downloaded (`data-plan`).
#' @param progress Show progress (default: in interactive sessions).
#' @return Invisibly, the bridge's fetch report (with `dry_run = TRUE`, the
#'   plan, printed).
#' @examples
#' \dontrun{
#' ss_fetch(c("ndi", "walkability"), dry_run = TRUE)
#' ss_fetch(c("ndi", "walkability"))
#' }
#' @export
ss_fetch <- function(variables = NULL, datasets = NULL, data_dir = ss_data_dir(),
                     accept_terms = character(), keep_archives = FALSE, dry_run = FALSE,
                     progress = interactive()) {
  args <- .ss_data_args(variables, datasets, data_dir)
  accept_terms <- as.character(accept_terms %||% character())
  env <- list(SPACESCANS_DATA_DIR = args$data_dir)
  plan <- .ss_bridge("data-plan", args, env = env)
  if (isTRUE(dry_run)) {
    out <- structure(plan, class = c("ss_fetch_plan", "list"))
    print(out)
    return(invisible(out))
  }
  needed <- .ss_plan_terms(plan)
  pending <- needed[!vapply(needed, function(t) t$id %in% accept_terms, logical(1))]
  if (length(pending) && interactive()) {
    for (t in pending) {
      cat(sprintf("\n%s (%s) is distributed under terms you must accept:\n  %s\n",
                  t$dataset %||% "?", t$id, t$url %||% t$title %||% ""))
      ans <- utils::askYesNo(sprintf("Do you accept the terms '%s' for this download?", t$id), default = FALSE)
      if (isTRUE(ans)) accept_terms <- c(accept_terms, t$id)
    }
  }
  args$accept_terms <- I(unique(accept_terms))
  args$keep_archives <- isTRUE(keep_archives)
  rd <- .ss_new_run_dir()
  job <- .ss_job_start("data-fetch", args, rd, env = env, meta = list(data_dir = args$data_dir))
  job$remove_on_success <- TRUE
  awake <- .ss_caffeinate(job$pid)
  on.exit(if (!is.null(awake)) try(awake$kill(), silent = TRUE), add = TRUE)
  state <- .ss_job_wait(job, progress = progress, cancel_on_interrupt = TRUE)
  out <- .ss_job_finish(job, interrupted = identical(state, "interrupted_by_user"))
  failed <- Filter(function(i) identical(i$outcome, "failed"), out$items %||% list())
  if (length(failed)) {
    warning(sprintf("spacescans: %d download(s) failed: %s; run the same call again to resume.",
                    length(failed), paste(vapply(failed, function(i) i$artifact %||% "?", ""), collapse = ", ")),
            call. = FALSE)
  }
  out <- structure(out, class = c("ss_fetch_report", "list"))
  if (isTRUE(progress)) print(out)
  invisible(out)
}

# Terms the plan says must be accepted: [{id, dataset, title, url}].
.ss_plan_terms <- function(plan) {
  t <- plan$terms_required %||% plan$terms %||% list()
  t <- Filter(function(x) is.list(x) && !is.null(x$id), t)
  t
}

# Keep a Mac awake while `pid` runs; NULL elsewhere.
.ss_caffeinate <- function(pid) {
  if (!identical(Sys.info()[["sysname"]], "Darwin") || !file.exists("/usr/bin/caffeinate")) return(NULL)
  tryCatch(processx::process$new("/usr/bin/caffeinate", c("-i", "-w", as.character(pid)),
                                 cleanup = TRUE),
           error = function(e) NULL)
}

#' @export
print.ss_fetch_plan <- function(x, ...) {
  cat("spacescans download plan for ", x$data_dir %||% "?", "\n", sep = "")
  items <- x$items %||% list()
  if (length(items)) {
    df <- .ss_records_df(items)
    keep <- intersect(c("artifact", "datasets", "bytes", "remaining_bytes", "source"), names(df))
    df <- df[, keep, drop = FALSE]
    for (col in intersect(c("bytes", "remaining_bytes"), names(df))) df[[col]] <- vapply(df[[col]], .ss_format_bytes, "")
    print(df, row.names = FALSE, right = FALSE)
  } else {
    cat("Nothing to download from the mirror.\n")
  }
  if (!is.null(x$download_bytes)) cat("Download: ", .ss_format_bytes(x$download_bytes), "\n", sep = "")
  if (!is.null(x$disk_bytes)) cat("Disk:     ", .ss_format_bytes(x$disk_bytes), "\n", sep = "")
  .ss_print_manual(x$manual)
  for (t in .ss_plan_terms(x)) cat("Terms to accept: ", t$id, " (", t$dataset %||% "", ")\n", sep = "")
  invisible(x)
}

# Datasets a fetch cannot complete (provider downloads, files not yet on
# the mirror): [{dataset, ..., instructions}].
.ss_print_manual <- function(manual) {
  manual <- manual %||% list()
  if (!length(manual)) return(invisible())
  cat("Needs a manual step:\n")
  for (m in manual) {
    ds <- if (is.list(m)) m$dataset %||% m$name %||% "?" else as.character(m)
    why <- if (is.list(m)) m$reason %||% m$title
    cat("  ", ds, if (!is.null(why)) paste0(": ", why), "\n", sep = "")
  }
  cat("ss_data_terms(\"<dataset>\") shows the instructions; ss_data_import() places files you downloaded.\n")
  invisible()
}

#' @export
print.ss_fetch_report <- function(x, ...) {
  cat("spacescans download ", if (isFALSE(x$ok)) "finished with problems" else "finished", "\n", sep = "")
  items <- x$items %||% list()
  if (length(items)) {
    df <- .ss_records_df(items)
    keep <- intersect(c("artifact", "outcome", "bytes_transferred", "seconds"), names(df))
    df <- df[, keep, drop = FALSE]
    if ("bytes_transferred" %in% names(df)) df$bytes_transferred <- vapply(df$bytes_transferred, .ss_format_bytes, "")
    print(df, row.names = FALSE, right = FALSE)
  } else {
    cat("Nothing was downloaded.\n")
  }
  .ss_print_manual(x$manual)
  invisible(x)
}

#' Verify, import and read terms of datasets
#'
#' * `ss_data_verify()` checks the files in the data directory against the
#'   manifest and their receipts (sizes; with `deep = TRUE` also SHA-256).
#' * `ss_data_import()` places files you downloaded by hand (for example
#'   VNL or ACAG from the provider) into the data directory and records
#'   receipts.
#' * `ss_data_terms()` shows a dataset's licence, attribution, terms and
#'   download instructions.
#'
#' @inheritParams ss_data_status
#' @param dataset A dataset id (see the `datasets` attribute of
#'   [ss_data_status()]).
#' @param paths Files or directories to import.
#' @param move Move instead of copy.
#' @return The bridge's report as a list (`ss_data_terms()`: class
#'   `ss_terms`, printed).
#' @examples
#' \dontrun{
#' ss_data_verify(c("ndi"), deep = TRUE)
#' ss_data_terms("faqsd")
#' ss_data_import("vnl", "~/Downloads/vnl")
#' }
#' @export
ss_data_verify <- function(variables = NULL, datasets = NULL, data_dir = ss_data_dir(), deep = FALSE) {
  args <- .ss_data_args(variables, datasets, data_dir, deep = isTRUE(deep))
  .ss_bridge("data-verify", args, progress = interactive(),
             env = list(SPACESCANS_DATA_DIR = args$data_dir))
}

#' @rdname ss_data_verify
#' @export
ss_data_import <- function(dataset, paths, data_dir = ss_data_dir(), move = FALSE) {
  if (!is.character(dataset) || length(dataset) != 1L || is.na(dataset)) {
    .ss_abort("config", "`dataset` must be a single dataset id.")
  }
  if (!is.character(paths) || !length(paths) || anyNA(paths)) .ss_abort("config", "`paths` must be file paths.")
  missing <- paths[!file.exists(path.expand(paths))]
  if (length(missing)) .ss_abort("config", sprintf("Not found: %s", paste(missing, collapse = ", ")))
  dd <- .ss_dir_value(data_dir, "data_dir")
  .ss_bridge("data-import", list(dataset = dataset, paths = I(normalizePath(path.expand(paths))),
                                 data_dir = dd, move = isTRUE(move)),
             progress = interactive(), env = list(SPACESCANS_DATA_DIR = dd))
}

#' @rdname ss_data_verify
#' @export
ss_data_terms <- function(dataset) {
  if (!is.character(dataset) || length(dataset) != 1L || is.na(dataset)) {
    .ss_abort("config", "`dataset` must be a single dataset id.")
  }
  structure(.ss_bridge("data-terms", list(dataset = dataset)), class = c("ss_terms", "list"))
}

#' @export
print.ss_terms <- function(x, ...) {
  cat(x$title %||% x$dataset, " (", x$dataset, ")\n", sep = "")
  for (f in c("provider", "license", "redistribution", "attribution", "source_url")) {
    if (!is.null(x[[f]]) && nzchar(x[[f]])) cat(sprintf("  %-15s %s\n", f, x[[f]]))
  }
  if (is.list(x$terms)) {
    cat("  terms id        ", x$terms$id %||% "", "\n", sep = "")
    if (!is.null(x$terms$url)) cat("  terms           ", x$terms$url, "\n", sep = "")
  }
  ins <- .ss_chr(x$instructions)
  if (length(ins)) cat("Instructions:\n", paste0("  ", ins, collapse = "\n"), "\n", sep = "")
  invisible(x)
}
