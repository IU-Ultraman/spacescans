prep <- function(df, cols = NULL) spacescans:::.ss_prepare_cohort(df, cols)

test_that("only the five required columns are kept, with the expected types", {
  out <- prep(fake_cohort())
  expect_equal(names(out$df), c("pid", "startDate", "endDate", "longitude", "latitude"))
  expect_type(out$df$pid, "character")
  expect_s3_class(out$df$startDate, "Date")
  expect_type(out$df$longitude, "double")
  expect_equal(out$n, 6L)
  expect_equal(out$n_patients, 4L)
})

test_that("numeric ids become text without scientific notation", {
  df <- fake_cohort()
  df$pid <- c(1e15, 1e15, 123456789012, 7, 7, 8)
  out <- prep(df)
  expect_equal(out$df$pid[1], "1000000000000000")
  expect_equal(out$df$pid[3], "123456789012")
  df$pid <- c(1L, 1L, 2L, 3L, 3L, 4L)
  expect_equal(prep(df)$df$pid, c("1", "1", "2", "3", "3", "4"))
})

test_that("PATID in any case stands in for pid, and cols maps other names", {
  df <- fake_cohort()
  names(df)[names(df) == "pid"] <- "PatId"
  expect_equal(prep(df)$map[["pid"]], "PatId")
  df <- fake_cohort()
  names(df)[1:5] <- c("id", "start", "end", "lon", "lat")
  out <- prep(df, cols = c(pid = "id", startDate = "start", endDate = "end", longitude = "lon", latitude = "lat"))
  expect_equal(out$df$longitude, df$lon)
  expect_error(prep(df, cols = c(patient = "id")), class = "spacescans_error_config")
  expect_error(prep(df), class = "spacescans_error_cohort")
  e <- tryCatch(prep(df), error = identity)
  expect_match(conditionMessage(e), "Missing column")
  expect_match(conditionMessage(e), "PATID")
})

test_that("problems are listed with row numbers and never with values", {
  df <- fake_cohort()
  df$pid[2] <- "SECRET-ID-42"
  df$longitude[c(2, 5)] <- c(-999.25, NA)
  df$latitude[3] <- 123.456
  df$endDate[4] <- df$startDate[4] - 7
  e <- tryCatch(prep(df), error = identity)
  expect_s3_class(e, "spacescans_error_cohort")
  msg <- conditionMessage(e)
  expect_match(msg, "Missing longitude in row 5", fixed = TRUE)
  expect_match(msg, "longitude outside -180..180 in row 2", fixed = TRUE)
  expect_match(msg, "latitude outside -90..90 in row 3", fixed = TRUE)
  expect_match(msg, "endDate before startDate in row 4", fixed = TRUE)
  expect_false(grepl("SECRET", msg))
  expect_false(grepl("999", msg))
  expect_false(grepl("123.456", msg, fixed = TRUE))
  expect_false(grepl("2015", msg))
  codes <- vapply(e$details$issues, `[[`, "", "code")
  expect_setequal(codes, c("coord_missing", "coord_range", "date_order"))
})

test_that("long row lists are cut, the count stays exact", {
  expect_equal(spacescans:::.ss_rows_text(rep(TRUE, 25)),
               "rows 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20 and 5 more")
})

test_that("text dates use one format for both columns", {
  df <- fake_cohort(3)
  df$startDate <- c("2015-01-01", "2015-02-01", "2015-03-01")
  df$endDate <- c("2016-01-01", "2016-02-01", "")
  expect_warning(out <- prep(df), "blank in row 3")
  expect_equal(out$df$startDate, as.Date(c("2015-01-01", "2015-02-01", "2015-03-01")))
  expect_true(is.na(out$df$endDate[3]))
  df$startDate <- c("1/2/2015", "2/1/2015", "12/31/2015")
  df$endDate <- c("1/2/2016", "2/1/2016", "12/31/2016")
  expect_equal(prep(df)$df$startDate, as.Date(c("2015-01-02", "2015-02-01", "2015-12-31")))
  df$endDate <- c("2016-01-02", "2/1/2016", "12/31/2016")
  e <- tryCatch(prep(df), error = identity)
  expect_s3_class(e, "spacescans_error_cohort")
  expect_match(conditionMessage(e), "one format")
  expect_false(grepl("2016", conditionMessage(e)))
  df$endDate <- c("2016-01-02 10:00", "2016-02-01", "2016-12-31")
  df$startDate <- c("2015-01-02", "2015-02-01", "2015-12-31")
  expect_error(prep(df), class = "spacescans_error_cohort")
})

test_that("date-times become dates with a warning; numbers are refused", {
  df <- fake_cohort(2)
  df$startDate <- as.POSIXct(c("2015-01-01 23:30:00", "2015-02-01 00:10:00"), tz = "America/New_York")
  expect_warning(out <- prep(df), "date-times")
  expect_equal(out$df$startDate, as.Date(c("2015-01-01", "2015-02-01")))
  df <- fake_cohort(2)
  df$startDate <- c(16436, 16467)
  expect_error(prep(df), class = "spacescans_error_cohort")
})

test_that("an empty cohort and bad coordinates types are refused", {
  df <- fake_cohort()[0, ]
  expect_error(prep(df), "no rows")
  df <- fake_cohort(2)
  df$latitude <- c("39.1", "north")
  e <- tryCatch(prep(df), error = identity)
  expect_match(conditionMessage(e), "Non-numeric latitude in row 2")
})

test_that("cohorts can be read from CSV and Parquet files", {
  d <- withr::local_tempdir()
  df <- fake_cohort()
  utils::write.csv(df, file.path(d, "c.csv"), row.names = FALSE)
  x <- spacescans:::.ss_cohort_input(file.path(d, "c.csv"))
  expect_equal(nrow(x), 6L)
  expect_type(x$pid, "character")
  expect_equal(prep(x)$df$startDate, df$startDate)
  nanoparquet::write_parquet(df[, 1:5], file.path(d, "c.parquet"))
  expect_equal(prep(spacescans:::.ss_cohort_input(file.path(d, "c.parquet")))$df$pid, df$pid)
  expect_error(spacescans:::.ss_cohort_input(file.path(d, "nope.csv")), class = "spacescans_error_cohort")
  expect_error(spacescans:::.ss_cohort_input(42), class = "spacescans_error_cohort")
})

test_that("buffer and resource arguments are checked in R", {
  expect_equal(spacescans:::.ss_whole(270, "buffer_m"), 270L)
  expect_error(spacescans:::.ss_whole(270.5, "buffer_m"), class = "spacescans_error_config")
  expect_error(spacescans:::.ss_whole(-1, "buffer_m"), class = "spacescans_error_config")
  expect_error(spacescans:::.ss_threads_arg(0), class = "spacescans_error_config")
  expect_equal(spacescans:::.ss_memory_arg("8GB"), "8GB")
  expect_error(spacescans:::.ss_memory_arg("lots"), class = "spacescans_error_config")
})


test_that("Parquet INT64 ids above 2^53 are read exactly (review rux F1)", {
  skip_if_not_installed("bit64")
  d <- withr::local_tempdir()
  big <- bit64::as.integer64("9007199254740993") + rep(0:19, each = 2) * 2L
  df <- data.frame(pid = big, startDate = as.Date("2015-01-01") + 0:39,
                   endDate = as.Date("2016-01-01") + 0:39, longitude = -86 - (1:40) / 100,
                   latitude = 39 + (1:40) / 100)
  df$income <- bit64::as.integer64("9007199254740995") + 0:39
  path <- file.path(d, "big.parquet")
  nanoparquet::write_parquet(df, path)
  x <- spacescans:::.ss_cohort_input(path)
  expect_s3_class(x$pid, "integer64")
  expect_s3_class(x$income, "integer64")
  out <- prep(x)
  expect_equal(out$n_patients, 20L)
  expect_equal(out$df$pid[1], "9007199254740993")
  expect_equal(length(unique(out$df$pid)), 20L)

  # INT64 without the INT(64) annotation, as pyarrow and pandas write it: nanoparquet
  # reads a double, so the column is read again with arrow, or refused without it.
  plain <- file.path(d, "plain.parquet")
  nanoparquet::write_parquet(df[, c("pid", "startDate", "endDate", "longitude", "latitude")], plain,
                             schema = nanoparquet::parquet_schema(pid = "INT64"))
  expect_type(nanoparquet::read_parquet(plain)$pid, "double")
  local({
    testthat::local_mocked_bindings(.ss_has_arrow = function() FALSE)
    e <- tryCatch(spacescans:::.ss_cohort_input(plain), error = identity)
    expect_s3_class(e, "spacescans_error_cohort")
    expect_match(conditionMessage(e), "pid of plain.parquet hold 64-bit integers", fixed = TRUE)
    expect_no_match(conditionMessage(e), "900719925474")
  })
  skip_if_not_installed("arrow")
  y <- spacescans:::.ss_cohort_input(plain)
  expect_s3_class(y$pid, "integer64")
  expect_equal(prep(y)$n_patients, 20L)
})

test_that("CSV columns of zero-padded codes keep their zeros; others are inferred (review rux F9)", {
  d <- withr::local_tempdir()
  path <- file.path(d, "lz.csv")
  writeLines(c("pid,startDate,endDate,longitude,latitude,zip,age,score,flag",
               "007,2015-01-01,2015-12-31,-86.1,39.7,02134,30,1.5,TRUE",
               "7,2015-01-01,2015-12-31,-86.2,39.8,46202,41,,FALSE"), path)
  x <- spacescans:::.ss_cohort_input(path)
  expect_equal(x$pid, c("007", "7"))
  expect_equal(x$zip, c("02134", "46202"))
  expect_type(x$age, "integer")
  expect_type(x$score, "double")
  expect_true(is.na(x$score[2]))
  expect_type(x$flag, "logical")
  expect_equal(prep(x)$n_patients, 2L)
})
