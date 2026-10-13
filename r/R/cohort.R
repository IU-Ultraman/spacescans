# Cohort checks done in R before anything reaches Python. Messages name
# columns and row numbers, never values (cohort values are protected data).

.ss_cohort_cols <- c("pid", "startDate", "endDate", "longitude", "latitude")

# Optional FIPS/GEOID columns the Python side reads as text (cohort.FIPS_STR_COLS).
.ss_fips_cols <- c("state_fips", "county_fips", "tract_geoid", "bg_geoid", "zcta5")

# Rows listed in a message; the count always covers all of them.
.ss_rows_text <- function(rows, max = 20L) {
  rows <- which(rows)
  shown <- utils::head(rows, max)
  more <- length(rows) - length(shown)
  paste0(if (length(rows) == 1L) "row " else "rows ", paste(shown, collapse = ", "),
         if (more > 0) sprintf(" and %d more", more) else "")
}

.ss_issue <- function(code, message, rows = NULL) {
  list(code = code, message = message,
       rows = if (!is.null(rows)) utils::head(which(rows), 20L),
       count = if (!is.null(rows)) sum(rows) else NA_integer_)
}

# A cohort given as a data frame or as the path of a CSV or Parquet file.
.ss_cohort_input <- function(cohort, cols = NULL) {
  if (is.data.frame(cohort)) return(cohort)
  if (is.character(cohort) && length(cohort) == 1L && !is.na(cohort)) {
    path <- path.expand(cohort)
    if (!file.exists(path)) .ss_abort("cohort", sprintf("Cohort file not found: %s", cohort))
    if (grepl("\\.parquet$", path, ignore.case = TRUE)) {
      return(as.data.frame(nanoparquet::read_parquet(path), stringsAsFactors = FALSE))
    }
    if (grepl("\\.(csv|txt)$", path, ignore.case = TRUE)) {
      # Ids, dates and FIPS/GEOID codes stay text (leading zeros); the rest is inferred.
      header <- names(utils::read.csv(path, nrows = 1L, check.names = FALSE))
      text <- header[tolower(header) %in% tolower(c(.ss_cohort_cols[1:3], "patid", .ss_fips_cols)) |
                       header %in% unname(cols)]
      return(utils::read.csv(path, stringsAsFactors = FALSE, check.names = FALSE,
                             colClasses = stats::setNames(rep("character", length(text)), text),
                             na.strings = c("", "NA")))
    }
    .ss_abort("cohort", sprintf("Unsupported cohort file: %s (expected .csv or .parquet).", cohort))
  }
  .ss_abort("cohort", "`cohort` must be a data frame or the path of a CSV or Parquet file.")
}

# Which user column provides each required column.
.ss_cohort_map <- function(df, cols = NULL) {
  map <- stats::setNames(.ss_cohort_cols, .ss_cohort_cols)
  if (!is.null(cols)) {
    if (!is.character(cols) || is.null(names(cols)) || any(!nzchar(names(cols)))) {
      .ss_abort("config", paste0("`cols` must be a named character vector, for example ",
                                 'c(pid = "patient_id", longitude = "lon").'))
    }
    bad <- setdiff(names(cols), .ss_cohort_cols)
    if (length(bad)) {
      .ss_abort("config", sprintf("Unknown name(s) in `cols`: %s. Use any of %s.",
                                  paste(bad, collapse = ", "), paste(.ss_cohort_cols, collapse = ", ")))
    }
    map[names(cols)] <- unname(cols)
  }
  # PATID in any case stands in for pid when there is no pid column.
  if (identical(map[["pid"]], "pid") && !"pid" %in% names(df)) {
    alias <- names(df)[tolower(names(df)) == "patid"]
    if (length(alias) >= 1L) map[["pid"]] <- alias[1]
  }
  map
}

.ss_blank <- function(x) {
  if (is.character(x)) is.na(x) | !nzchar(trimws(x)) else is.na(x)
}

.ss_pid_chr <- function(x) {
  if (is.factor(x)) return(as.character(x))
  if (is.character(x)) return(x)
  if (inherits(x, "integer64")) return(as.character(x))
  if (is.double(x)) {
    out <- format(x, scientific = FALSE, trim = TRUE, digits = 15)
    out[is.na(x)] <- NA_character_
    return(out)
  }
  out <- as.character(x)
  out[is.na(x)] <- NA_character_
  out
}

# Parse text dates with one format shared by both columns, as the Python
# side does: ISO 2017-08-19 first, then US 8/19/2017. A value must match the
# whole format (as.Date() alone ignores trailing text such as a time).
.ss_parse_fmt <- function(x, fmt) {
  d <- as.Date(x, format = fmt)
  back <- format(d, fmt)
  # strptime accepts 8/9/2017 for %m/%d/%Y; compare without leading zeros.
  strip <- function(v) gsub("(^|[^0-9])0+([0-9])", "\\1\\2", v)
  ok <- is.na(x) | (!is.na(d) & strip(x) == strip(back))
  list(d = d, bad = !ok)
}

.ss_text_dates <- function(cols) {
  vals <- lapply(cols, function(x) {
    x <- trimws(as.character(x))
    x[!nzchar(x)] <- NA_character_
    x
  })
  for (fmt in c("%Y-%m-%d", "%m/%d/%Y")) {
    parsed <- lapply(vals, .ss_parse_fmt, fmt = fmt)
    if (!any(vapply(parsed, function(p) any(p$bad), logical(1)))) {
      return(list(dates = lapply(parsed, `[[`, "d"), bad = NULL))
    }
  }
  # Report the rows that fit neither format, or the ISO failures when every
  # row fits one of them but the columns mix formats.
  bad <- lapply(vals, function(x) .ss_parse_fmt(x, "%Y-%m-%d")$bad & .ss_parse_fmt(x, "%m/%d/%Y")$bad)
  if (!any(vapply(bad, any, logical(1)))) bad <- lapply(vals, function(x) .ss_parse_fmt(x, "%Y-%m-%d")$bad)
  list(dates = NULL, bad = bad)
}

# Check and convert the cohort. Returns list(df = the 5 columns, n,
# n_patients, map); raises spacescans_error_cohort listing every problem.
.ss_prepare_cohort <- function(df, cols = NULL) {
  map <- .ss_cohort_map(df, cols)
  n <- nrow(df)
  issues <- list()
  add <- function(issue) issues[[length(issues) + 1L]] <<- issue
  missing <- .ss_cohort_cols[!map %in% names(df)]
  if (length(missing)) {
    add(.ss_issue("missing_columns", sprintf(
      "Missing column(s): %s%s. Map other names with cols = c(%s = \"...\").",
      paste(map[missing], collapse = ", "),
      if ("pid" %in% missing) " (the patient id may be named pid or PATID)" else "",
      missing[1])))
  }
  if (n == 0L) add(.ss_issue("empty", "The cohort has no rows."))
  out <- list()
  warn <- character()
  if (!"pid" %in% missing && n > 0L) {
    pid <- .ss_pid_chr(df[[map[["pid"]]]])
    bad <- .ss_blank(pid)
    if (any(bad)) add(.ss_issue("pid_missing", sprintf("Missing pid in %s.", .ss_rows_text(bad)), bad))
    out$pid <- pid
  }
  date_cols <- intersect(c("startDate", "endDate"), setdiff(.ss_cohort_cols, missing))
  if (length(date_cols) && n > 0L) {
    raw <- lapply(map[date_cols], function(col) df[[col]])
    names(raw) <- date_cols
    is_text <- vapply(raw, function(x) is.character(x) || is.factor(x), logical(1))
    conv <- list()
    for (dc in date_cols) {
      x <- raw[[dc]]
      if (inherits(x, "Date")) {
        conv[[dc]] <- as.Date(x)
      } else if (inherits(x, c("POSIXct", "POSIXlt"))) {
        tz <- attr(as.POSIXlt(x), "tzone")[1]
        conv[[dc]] <- as.Date(as.POSIXct(x), tz = if (is.null(tz) || !nzchar(tz)) "" else tz)
        warn <- c(warn, sprintf(
          "%s holds date-times; they were converted to dates in their own time zone (episodes are whole days).",
          map[[dc]]))
      } else if (!is_text[[dc]]) {
        add(.ss_issue("date_type", sprintf(
          "%s must be a Date, a date-time or text such as 2017-08-19 or 8/19/2017.", map[[dc]])))
      }
    }
    text_cols <- date_cols[is_text]
    if (length(text_cols)) {
      td <- .ss_text_dates(raw[text_cols])
      if (is.null(td$bad)) {
        conv[text_cols] <- td$dates
      } else {
        for (i in seq_along(text_cols)) {
          if (any(td$bad[[i]])) {
            add(.ss_issue("date_format", sprintf(
              "%s: dates must use one format in both date columns, 2017-08-19 (ISO, preferred) or 8/19/2017; %s do not.",
              map[[text_cols[i]]], .ss_rows_text(td$bad[[i]])), td$bad[[i]]))
          }
        }
      }
    }
    for (dc in names(conv)) {
      blank <- is.na(conv[[dc]])
      if (any(blank)) {
        warn <- c(warn, sprintf("%s is blank in %s; those episodes get no time-varying exposure.",
                                map[[dc]], .ss_rows_text(blank)))
      }
      out[[dc]] <- conv[[dc]]
    }
    if (all(c("startDate", "endDate") %in% names(conv))) {
      order <- !is.na(conv$startDate) & !is.na(conv$endDate) & conv$endDate < conv$startDate
      if (any(order)) add(.ss_issue("date_order", sprintf("endDate before startDate in %s.", .ss_rows_text(order)), order))
    }
  }
  for (cc in list(c("longitude", -180, 180), c("latitude", -90, 90))) {
    col <- cc[1]
    if (col %in% missing || n == 0L) next
    lo <- as.numeric(cc[2])
    hi <- as.numeric(cc[3])
    x <- df[[map[[col]]]]
    blank <- .ss_blank(x)
    num <- if (is.numeric(x)) as.double(x) else suppressWarnings(as.double(as.character(x)))
    not_num <- is.na(num) & !blank
    if (any(blank)) add(.ss_issue("coord_missing", sprintf("Missing %s in %s.", map[[col]], .ss_rows_text(blank)), blank))
    if (any(not_num)) add(.ss_issue("coord_not_numeric", sprintf("Non-numeric %s in %s.", map[[col]], .ss_rows_text(not_num)), not_num))
    out_rng <- !is.na(num) & (num < lo | num > hi | !is.finite(num))
    if (any(out_rng)) add(.ss_issue("coord_range", sprintf("%s outside %g..%g in %s.", map[[col]], lo, hi, .ss_rows_text(out_rng)), out_rng))
    out[[col]] <- num
  }
  if (length(issues)) {
    .ss_abort("cohort", paste0("The cohort cannot be linked:\n",
                               paste0("* ", vapply(issues, `[[`, "", "message"), collapse = "\n")),
              hint = "Fix the rows named above; row numbers count from 1.",
              details = list(issues = issues))
  }
  for (w in unique(warn)) warning(paste0("spacescans: ", w), call. = FALSE)
  five <- data.frame(pid = out$pid, startDate = out$startDate, endDate = out$endDate,
                     longitude = out$longitude, latitude = out$latitude, stringsAsFactors = FALSE)
  list(df = five, n = n, n_patients = length(unique(out$pid)), map = map)
}

# Write the 5 columns (and nothing else) for the bridge; the file is 0600
# inside the 0700 run directory.
.ss_write_cohort <- function(five, run_dir) {
  path <- file.path(run_dir, "cohort.parquet")
  nanoparquet::write_parquet(five, path)
  Sys.chmod(path, "0600", use_umask = FALSE)
  path
}

.ss_whole <- function(x, name, min = 0) {
  if (!is.numeric(x) || length(x) != 1L || is.na(x) || !is.finite(x) || x != round(x) || x < min) {
    .ss_abort("config", sprintf("`%s` must be a single whole number of at least %s (metres).", name, format(min)))
  }
  as.integer(x)
}

.ss_threads_arg <- function(threads) {
  if (is.null(threads)) return(NULL)
  if (!is.numeric(threads) || length(threads) != 1L || is.na(threads) || threads < 1 ||
      threads != round(threads)) {
    .ss_abort("config", "`threads` must be a single whole number of at least 1.")
  }
  as.integer(threads)
}

.ss_memory_arg <- function(memory_limit) {
  if (is.null(memory_limit)) return(NULL)
  if (!is.character(memory_limit) || length(memory_limit) != 1L || is.na(memory_limit) ||
      !grepl("^[0-9.]+ *[KMGT]i?B$", memory_limit, ignore.case = TRUE)) {
    .ss_abort("config", "`memory_limit` must be a single string such as \"8GB\".")
  }
  memory_limit
}
