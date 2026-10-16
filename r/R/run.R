#' Run one pipeline config
#'
#' Runs a single C3 or C4 config (the tier-1 interface) in a separate Python
#' process. Progress is shown while it runs; Ctrl-C stops the Python process
#' tree. On success the output is read back into R.
#'
#' @param config Path to a YAML config, or the config as a named list (see
#'   [ss_templates()] and [ss_config_schema()]).
#' @param data_dir Root for relative input paths. Default: the
#'   `SPACESCANS_DATA_DIR` environment variable, then the config's `base_dir`.
#' @param output_dir Root for a relative `output.path`. Default: a fresh
#'   directory under [tempdir()], removed when the R session ends.
#' @param threads,memory_limit DuckDB worker threads and memory cap (for
#'   example `"8GB"`). They cap DuckDB only: C3 patterns and the pandas-based
#'   FAQSD and TEMIS readers do not run inside DuckDB.
#' @param read If `TRUE` (default), return the output table; otherwise return
#'   the run summary.
#' @param progress Show a progress line (default: in interactive sessions).
#' @param verbose Print the pipeline's own log lines.
#' @param strict If `TRUE`, unknown config keys are an error.
#' @return With `read = TRUE`, a data frame (see [ss_read_result()]) with
#'   attribute `spacescans_run` holding the run summary. Otherwise the summary:
#'   a list with `output`, `rows`, `columns`, `seconds`, `maxrss_bytes`,
#'   `engine`, the resolved `config` and more.
#' @examples
#' \dontrun{
#' cfg <- ss_templates("c4/zbp")
#' res <- ss_run(cfg, data_dir = "~/ss-data", threads = 4)
#' }
#' @export
ss_run <- function(config, data_dir = NULL, output_dir = NULL, threads = NULL, memory_limit = NULL,
                   read = TRUE, progress = interactive(), verbose = FALSE, strict = FALSE) {
  if (!is.null(threads)) {
    if (!is.numeric(threads) || length(threads) != 1L || is.na(threads) || threads < 1 ||
        threads != round(threads)) {
      .ss_abort("config", "`threads` must be a single whole number of at least 1.")
    }
    threads <- as.integer(threads)
  }
  if (!is.null(memory_limit) && (!is.character(memory_limit) || length(memory_limit) != 1L)) {
    .ss_abort("config", "`memory_limit` must be a single string such as \"8GB\".")
  }
  if (is.null(output_dir)) output_dir <- .ss_new_run_dir(file.path(tempdir(), "spacescans-output"))
  args <- list(config = .ss_config_arg(config),
               data_dir = .ss_dir_arg(data_dir, "data_dir"),
               output_dir = .ss_dir_arg(output_dir, "output_dir", create = TRUE),
               threads = threads, memory_limit = memory_limit, strict = isTRUE(strict))
  value <- .ss_bridge("run", Filter(Negate(is.null), args), progress = progress, verbose = verbose,
                      keep_on_error = TRUE)
  .ss_run_value(value, read)
}

#' Run the bundled quickstart demo
#'
#' Runs a small C3 weight computation (`boundary_overlap_fast`) on the
#' synthetic sample counties and locations that come with the Python package.
#' Needs the `geo` extra. Useful to check that an installation works; it runs
#' one spatial-weighting step, not a linkage.
#'
#' @inheritParams ss_run
#' @param threads DuckDB worker threads.
#' @return As [ss_run()].
#' @examples
#' \dontrun{
#' w <- ss_quickstart()
#' head(w)
#' }
#' @export
ss_quickstart <- function(output_dir = NULL, threads = NULL, read = TRUE, progress = interactive(),
                          verbose = FALSE) {
  if (is.null(output_dir)) output_dir <- .ss_new_run_dir(file.path(tempdir(), "spacescans-output"))
  args <- list(output_dir = .ss_dir_arg(output_dir, "output_dir", create = TRUE),
               threads = if (!is.null(threads)) as.integer(threads))
  value <- .ss_bridge("quickstart", Filter(Negate(is.null), args), progress = progress,
                      verbose = verbose, keep_on_error = TRUE)
  .ss_run_value(value, read)
}

.ss_run_value <- function(value, read) {
  info <- attr(value, "spacescans")
  value$columns <- unlist(value$columns) %||% character()
  value$config <- .ss_simplify(value$config)
  value$unknown_keys <- unlist(value$unknown_keys) %||% character()
  value$bridge <- info
  attr(value, "spacescans") <- NULL
  class(value) <- c("ss_run_result", "list")
  if (!isTRUE(read)) return(value)
  episode <- identical(value$output_grouping, "episode")
  df <- ss_read_result(value$output, episode = if (episode) TRUE else NULL)
  attr(df, "spacescans_run") <- value
  df
}

#' @export
print.ss_run_result <- function(x, ...) {
  cat("spacescans run: ", x$name, " (", x$linkage_pattern, ")\n", sep = "")
  cat("  output   ", x$output, "\n", sep = "")
  cat("  rows     ", format(x$rows), "; columns: ", paste(x$columns, collapse = ", "), "\n", sep = "")
  cat("  seconds  ", format(x$seconds), "; peak RSS ", format(round((x$maxrss_bytes %||% NA) / 2^20)), " MiB\n", sep = "")
  invisible(x)
}

#' Read a pipeline output table
#'
#' Reads a Parquet output with \pkg{nanoparquet} (no Arrow needed).
#' Episode-level C4 outputs carry the episode id in a column named `geoid`;
#' it is renamed to `episode_id` so it can be joined back to the cohort rows
#' (`episode_id` is the 0-based input row number).
#'
#' 64-bit integer columns are read as R doubles by nanoparquet, which is exact
#' up to 2^53; `geoid`/`episode_id` columns whose values fit are converted to
#' integer.
#'
#' @param path Path to a `.parquet` (or `.csv`) file.
#' @param episode `TRUE` to rename `geoid` to `episode_id`, `FALSE` to leave
#'   it, `NULL` (default) to rename only when the table has both `PATID` and
#'   `geoid` columns (an episode-level C4 output).
#' @return A data frame.
#' @examples
#' \dontrun{
#' df <- ss_read_result("output/c4_ndi.parquet")
#' }
#' @export
ss_read_result <- function(path, episode = NULL) {
  if (!is.character(path) || length(path) != 1L || !file.exists(path)) {
    .ss_abort("data", sprintf("Result file not found: %s", format(path)), details = list(path = path))
  }
  df <- if (grepl("\\.csv$", path, ignore.case = TRUE)) {
    # PATID, and a geoid that is a geography code rather than an episode id,
    # stay text: "007" and "7" are different patients, 01001 a FIPS code.
    header <- names(utils::read.csv(path, nrows = 1L, check.names = FALSE))
    episodes <- if (is.null(episode)) all(c("PATID", "geoid") %in% header) else isTRUE(episode)
    text <- intersect(c("PATID", if (!episodes) "geoid"), header)
    utils::read.csv(path, stringsAsFactors = FALSE, check.names = FALSE,
                    colClasses = stats::setNames(rep("character", length(text)), text))
  } else if (grepl("\\.parquet$", path, ignore.case = TRUE)) {
    nanoparquet::read_parquet(path)
  } else {
    .ss_abort("data", sprintf("Unsupported result format: %s (expected .parquet or .csv)", path))
  }
  df <- as.data.frame(df, stringsAsFactors = FALSE)
  rename <- if (is.null(episode)) all(c("PATID", "geoid") %in% names(df)) else isTRUE(episode)
  if (rename && "geoid" %in% names(df) && !"episode_id" %in% names(df)) {
    names(df)[names(df) == "geoid"] <- "episode_id"
  }
  for (col in intersect(c("episode_id", "geoid"), names(df))) {
    v <- df[[col]]
    if (is.double(v) && all(is.na(v) | (v == round(v) & abs(v) <= .Machine$integer.max))) {
      df[[col]] <- as.integer(v)
    }
  }
  df
}
