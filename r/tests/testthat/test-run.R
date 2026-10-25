test_that("ss_run() runs a C4 config and reads the episode-level output", {
  skip_if_no_python()
  local_clean_state()
  d <- withr::local_tempdir()
  cfg <- c4_fixture(d)
  df <- ss_run(cfg, data_dir = d, threads = 1, workers = 2, progress = FALSE)
  expect_s3_class(df, "data.frame")
  expect_equal(names(df), c("PATID", "episode_id", "v"))
  expect_type(df$episode_id, "integer")
  df <- df[order(df$episode_id), ]
  expect_equal(df$episode_id, 0:2)
  expect_equal(df$v, c(10, 40, (20 * 214 + 30 * 151) / 365), tolerance = 1e-12)
  run <- attr(df, "spacescans_run")
  expect_s3_class(run, "ss_run_result")
  expect_equal(run$engine$threads, "1")
  expect_equal(run$config$engine$workers, 2L)
  expect_equal(run$rows, 3L)
  expect_gt(run$maxrss_bytes, 1e7)
  expect_output(print(run), "c4_fixture")
})

test_that("ss_run(read = FALSE) returns the summary and accepts a YAML path", {
  skip_if_no_python()
  local_clean_state()
  d <- withr::local_tempdir()
  cfg <- c4_fixture(d)
  path <- file.path(d, "c4.yaml")
  ss_write_config(cfg, path)
  out_dir <- file.path(d, "out")
  run <- ss_run(path, data_dir = d, output_dir = out_dir, read = FALSE, progress = FALSE)
  expect_equal(run$output, file.path(normalizePath(out_dir), "c4_fixture.parquet"))
  expect_equal(run$config_source$source, "yaml")
  expect_equal(run$columns, c("PATID", "geoid", "v"))
})

test_that("a missing input is a data error and the run directory is kept", {
  skip_if_no_python()
  local_clean_state()
  d <- withr::local_tempdir()
  cfg <- c4_fixture(d)
  unlink(file.path(d, "patients.parquet"))
  e <- tryCatch(ss_run(cfg, data_dir = d, progress = FALSE), error = identity)
  expect_s3_class(e, "spacescans_error_data")
  expect_match(e$details$path, "patients.parquet$")
  expect_true(dir.exists(e$run_dir))
  expect_true(file.exists(file.path(e$run_dir, "bridge.log")))
  unlink(e$run_dir, recursive = TRUE)
})

test_that("bad threads and workers are rejected before Python starts", {
  expect_error(ss_run(list(name = "x"), threads = 0), class = "spacescans_error_config")
  expect_error(ss_run(list(name = "x"), threads = 1.5), class = "spacescans_error_config")
  expect_error(ss_run(list(name = "x"), workers = 0), class = "spacescans_error_config")
  expect_error(ss_run(list(name = "x"), workers = TRUE), class = "spacescans_error_config")
  expect_error(ss_run(list(name = "x"), memory_limit = 4), class = "spacescans_error_config")
})

test_that("a pattern without its extra is a missing_extra error with an ss_install() hint", {
  skip_if_no_python()
  local_clean_state()
  d <- withr::local_tempdir()
  ss_use_python(base_only_python(d))
  cfg <- list(name = "c3", linkage_pattern = "boundary_overlap_fast", geometry_type = "polygon",
              source = list(file = file.path(d, "a.shp"), join_col = "G"),
              buffer = list(patient_file = file.path(d, "p.parquet")),
              output = list(path = file.path(d, "o.parquet")))
  e <- tryCatch(ss_run(cfg, progress = FALSE), error = identity)
  expect_s3_class(e, "spacescans_error_missing_extra")
  expect_equal(e$details$extra, "geo")
  expect_match(conditionMessage(e), 'ss_install(extras = "geo")', fixed = TRUE)
  unlink(e$run_dir, recursive = TRUE)
})

test_that("ss_quickstart() runs the bundled demo", {
  skip_if_no_geo()
  local_clean_state()
  w <- ss_quickstart(progress = FALSE)
  expect_equal(names(w), c("geoid", "GEOID10", "value"))
  expect_gt(nrow(w), 0)
  expect_true(attr(w, "spacescans_run")$demo)
})

test_that("ss_read_result() renames geoid in episode-level outputs and pins the INT64 type", {
  skip_if_no_python()
  d <- withr::local_tempdir()
  path <- file.path(d, "c4.parquet")
  code <- sprintf(paste0(
    "import pandas as pd; pd.DataFrame({'PATID': ['a', 'b'], 'geoid': pd.Series([5, 7], dtype='int64'), ",
    "'v': [1.5, None]}).to_parquet(r'%s', index=False)"), path)
  status <- system2(test_python(), c("-I", "-c", shQuote(code)))
  expect_equal(status, 0L)
  raw <- nanoparquet::read_parquet(path)
  expect_type(raw$geoid, "double")          # nanoparquet 0.5.x: INT64 -> double
  df <- ss_read_result(path)
  expect_equal(names(df), c("PATID", "episode_id", "v"))
  expect_type(df$episode_id, "integer")
  expect_equal(df$episode_id, c(5L, 7L))
  expect_true(is.na(df$v[2]))
  expect_false(inherits(df, "tbl"))
  expect_equal(names(ss_read_result(path, episode = FALSE))[2], "geoid")
  expect_error(ss_read_result(file.path(d, "none.parquet")), class = "spacescans_error_data")
})


test_that("ss_read_result() keeps CSV PATIDs and geography geoids as text (review rux F9)", {
  d <- withr::local_tempdir()
  ep <- file.path(d, "c4.csv")
  writeLines(c("PATID,geoid,v", "007,0,1.5", "7,1,2.5"), ep)
  df <- ss_read_result(ep)
  expect_equal(df$PATID, c("007", "7"))
  expect_equal(df$episode_id, c(0L, 1L))
  geo <- file.path(d, "c3.csv")
  writeLines(c("geoid,value", "01001020100,3.5", "18097320100,4.5"), geo)
  expect_equal(ss_read_result(geo)$geoid, c("01001020100", "18097320100"))
})
