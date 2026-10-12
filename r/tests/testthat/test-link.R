link <- function(cohort = fake_cohort(), variables = c("ndi", "walkability"), ...) {
  ss_link(cohort, variables, data_dir = tempdir(), progress = FALSE, cache = FALSE, ...)
}

test_that("ss_link() joins the exposures onto the cohort, keeping its types and attributes", {
  dir <- local_fake_bridge()
  co <- fake_cohort()
  res <- link(co)
  expect_equal(names(res), c(names(co), "ndi", "NatWalkInd"))
  expect_identical(res[names(co)], structure(co, coverage = NULL), ignore_attr = TRUE)
  expect_s3_class(res$sex, "factor")
  expect_type(res$age, "integer")
  expect_equal(attr(res, "study"), "fake-study")
  expect_equal(res$ndi, round(seq_len(6) / 7 + 1, 4))
  cov <- attr(res, "coverage")
  expect_equal(cov$variable, c("ndi", "walkability"))
  expect_equal(cov$pct, c(100, 100))
  expect_equal(attr(res, "features")$feature_name, c("ndi", "NatWalkInd"))
  expect_equal(attr(res, "provenance")$variables, c("ndi", "walkability"))
  info <- attr(res, "spacescans")
  expect_equal(info$steps$name, c("c3_ndi", "c3_walkability"))
  expect_equal(info$patients, 4L)
  expect_null(info$run_dir)
})

test_that("only the five cohort columns reach Python, in a private run directory that is removed", {
  dir <- local_fake_bridge()
  link()
  req <- last_request(dir, "link")
  seen <- attr(req, "seen")
  expect_equal(seen$cohort_columns, c("pid", "startDate", "endDate", "longitude", "latitude"))
  expect_equal(unname(seen$cohort_types), c("character", "Date", "Date", "numeric", "numeric"))
  expect_equal(seen$run_dir_mode, "700")
  expect_equal(seen$cohort_mode, "600")
  expect_false(dir.exists(req$args$run_dir))
  expect_equal(unlist(req$args$reserved_names), names(fake_cohort()))
  expect_equal(req$args$executor, "subprocess")
  expect_equal(req$args$buffer_m, 270L)
  expect_equal(req$client$name, "spacescans-r")
  expect_equal(seen$data_dir_env, normalizePath(tempdir()))
  expect_null(req$args$cache_dir)
})

test_that("keep = TRUE keeps the run directory and reports it", {
  dir <- local_fake_bridge()
  res <- link(keep = TRUE)
  rd <- attr(res, "spacescans")$run_dir
  on.exit(unlink(rd, recursive = TRUE))
  expect_true(all(file.exists(file.path(rd, c("cohort.parquet", "request.json", "result.json",
                                               "events.jsonl", "job.json", "exposures.parquet")))))
  job <- jsonlite::read_json(file.path(rd, "job.json"))
  expect_equal(job$command, "link")
  # job.json carries no cohort values
  expect_false(any(grepl("-86.0|2015-0", readLines(file.path(rd, "job.json")))))
})

test_that("a named run_dir is created 0700 and must be empty", {
  dir <- local_fake_bridge()
  rd <- file.path(withr::local_tempdir(), "run1")
  res <- link(run_dir = rd, keep = TRUE)
  expect_equal(as.character(file.info(rd)$mode), "700")
  expect_error(link(run_dir = rd), class = "spacescans_error_config")
})

test_that("columns whose names are taken get the web's _<exp>_dup suffix", {
  dir <- local_fake_bridge()
  co <- fake_cohort()
  co$ndi <- "user value"
  res <- link(co, "ndi")
  expect_equal(res$ndi, rep("user value", 6))
  expect_true("ndi_ndi_dup" %in% names(res))
})

test_that("a variable with fewer than 90% matched episodes warns", {
  dir <- local_fake_bridge(list(link = list(na_frac = list(walkability = 0.5))))
  expect_warning(res <- link(), "only 50.0% of episodes got a walkability value")
  expect_equal(sum(is.na(res$NatWalkInd)), 3L)
})

test_that("episode ids that do not match the cohort are an internal error", {
  dir <- local_fake_bridge(list(link = list(bad_ids = TRUE)))
  e <- tryCatch(link(), error = identity)
  expect_s3_class(e, "spacescans_error_internal")
  expect_match(conditionMessage(e), "episode_id 0..5")
  expect_true(dir.exists(e$run_dir))
  unlink(e$run_dir, recursive = TRUE)
})

test_that("missing data stops before the link starts, naming the ss_fetch() call", {
  dir <- local_fake_bridge(list(`data-status` = list(missing = list("ndi"))))
  e <- tryCatch(link(), error = identity)
  expect_s3_class(e, "spacescans_error_data_missing")
  expect_s3_class(e, "spacescans_error_data")
  expect_match(conditionMessage(e), "ndi: missing (needs ndi_panel)", fixed = TRUE)
  expect_equal(e$hint, 'Run ss_fetch("ndi") to download the missing data.')
  expect_length(fake_requests(dir, "link"), 0L)
  dir <- local_fake_bridge(list(`data-status` = list(missing = list("ndi"), status = "corrupt")))
  e <- tryCatch(link(variables = "ndi"), error = identity)
  expect_s3_class(e, "spacescans_error_data_corrupt")
  expect_match(e$hint, "ss_data_verify")
})

test_that("a missing Python extra stops before the link starts, naming the ss_install() call", {
  dir <- local_fake_bridge()
  e <- tryCatch(link(variables = "temis"), error = identity)
  expect_s3_class(e, "spacescans_error_missing_extra")
  expect_match(e$hint, 'ss_install(extras = "hdf4")', fixed = TRUE)
  expect_length(fake_requests(dir, "link"), 0L)
})

test_that("a failed step is a spacescans_error_step with the log tail and an OOM hint", {
  dir <- local_fake_bridge(list(link = list(error = list(
    kind = "step_failed", message = "step c3_bg failed",
    details = list(step = "c3_bg", rc = -9L, log_tail = list("line one", "Killed"))))))
  e <- tryCatch(link(), error = identity)
  expect_s3_class(e, "spacescans_error_step")
  expect_match(conditionMessage(e), "Killed")
  expect_match(e$hint, "ran out of memory")
  expect_match(conditionMessage(e), "Run directory kept at")
  expect_true(dir.exists(e$run_dir))
  unlink(e$run_dir, recursive = TRUE)
})

test_that("on_error = 'continue' returns what succeeded and warns about the rest", {
  dir <- local_fake_bridge(list(link = list(failed = list("vnl"))))
  expect_warning(res <- link(on_error = "continue"), "failed and have no columns: vnl")
  expect_equal(attr(res, "spacescans")$failed, "vnl")
  expect_equal(last_request(dir, "link")$args$on_error, "continue")
})

test_that("a crash of the bridge during link is reported as crashed", {
  dir <- local_fake_bridge(list(link = list(exit = 7L)))
  e <- tryCatch(link(), error = identity)
  expect_s3_class(e, "spacescans_error_crashed")
  expect_equal(e$exit_status, 7L)
  unlink(e$run_dir, recursive = TRUE)
})

test_that("the C3 cache lives in R_user_dir('spacescans', 'cache') with mode 0700", {
  dir <- local_fake_bridge()
  root <- withr::local_tempdir()
  withr::local_envvar(R_USER_CACHE_DIR = root)
  withr::local_options(spacescans.cache_dir = NULL)
  ss_link(fake_cohort(), "ndi", data_dir = tempdir(), progress = FALSE)
  req <- last_request(dir, "link")
  expect_equal(req$args$cache_dir, normalizePath(file.path(root, "R", "spacescans")))
  expect_equal(attr(req, "seen")$cache_dir_mode, "700")
  own <- file.path(withr::local_tempdir(), "mycache")
  ss_link(fake_cohort(), "ndi", data_dir = tempdir(), progress = FALSE, cache = own)
  expect_equal(last_request(dir, "link")$args$cache_dir, normalizePath(own))
  expect_error(ss_link(fake_cohort(), "ndi", data_dir = tempdir(), progress = FALSE, cache = 3),
               class = "spacescans_error_config")
})

test_that("data, run and cache directories in cloud-synced folders are warned about", {
  dir <- local_fake_bridge()
  local_mocked_bindings(.ss_is_cloud_synced = function(path) TRUE)
  w <- character()
  withCallingHandlers(link(), warning = function(cnd) {
    w <<- c(w, conditionMessage(cnd))
    invokeRestart("muffleWarning")
  })
  expect_true(any(grepl("data_dir .* cloud-synced", w)))
  expect_true(any(grepl("run_dir .* cloud-synced", w)))
})

test_that("arguments are checked before Python starts", {
  local_clean_state()
  withr::local_envvar(SPACESCANS_PYTHON = "/nonexistent/python")
  expect_error(ss_link(fake_cohort(), "ndi", buffer_m = 270.5), class = "spacescans_error_config")
  expect_error(ss_link(fake_cohort(), character()), class = "spacescans_error_config")
  expect_error(ss_link(fake_cohort(), "ndi", threads = 0), class = "spacescans_error_config")
  bad <- fake_cohort()
  bad$latitude[2] <- NA
  expect_error(ss_link(bad, "ndi"), class = "spacescans_error_cohort")
})

test_that("without a data directory ss_link() says how to set one", {
  dir <- local_fake_bridge()
  withr::local_envvar(SPACESCANS_DATA_DIR = NA)
  withr::local_options(spacescans.data_dir = NULL)
  e <- tryCatch(ss_link(fake_cohort(), "ndi", progress = FALSE), error = identity)
  expect_s3_class(e, "spacescans_error_config")
  expect_match(e$hint, "ss_data_dir")
})

test_that("custom datasets are linked under their variable key", {
  dir <- local_fake_bridge()
  d <- withr::local_tempdir()
  csv <- file.path(d, "svi.csv")
  writeLines(c("FIPS,RPL", "18097000100,0.5"), csv)
  svi <- ss_custom_polygon(csv, boundary = "Tract", key_col = "FIPS", value_cols = "RPL", name = "SVI")
  expect_s3_class(svi, "ss_custom")
  expect_output(print(svi), "custom_svicsv")
  creq <- last_request(dir, "custom-validate")
  expect_equal(creq$args$kind, "polygon")
  expect_equal(unlist(creq$args$files), normalizePath(csv))
  expect_equal(unlist(creq$args$options$value_cols), "RPL")
  expect_equal(creq$args$options$boundary, "Tract")
  res <- link(variables = "ndi", custom = svi)
  req <- last_request(dir, "link")
  expect_equal(unlist(req$args$variables), c("ndi", "custom_svicsv"))
  expect_equal(names(req$args$custom), "custom_svicsv")
  expect_equal(unlist(req$args$custom$custom_svicsv$value_cols), "RPL")
  expect_true("RPL" %in% names(res))
  # the data preflight only asks about catalog variables
  expect_equal(unlist(last_request(dir, "data-status")$args$variables), "ndi")
})

test_that("Ctrl-C stops the link, kills the bridge and keeps the run directory", {
  skip_on_os("windows")
  d <- withr::local_tempdir()
  pid_file <- file.path(d, "pid")
  dir <- local_fake_bridge(list(link = list(sleep_s = 60, pid_file = pid_file)))
  spacescans:::.ss_hello()
  killer <- processx::process$new("/bin/sh", c("-c", sprintf(
    "while [ ! -s '%s' ]; do sleep 0.1; done; sleep 0.5; kill -INT %d", pid_file, Sys.getpid())))
  t0 <- Sys.time()
  e <- tryCatch(link(), error = identity)
  elapsed <- as.numeric(difftime(Sys.time(), t0, units = "secs"))
  killer$wait(5000)
  expect_s3_class(e, "spacescans_error_interrupted")
  expect_lt(elapsed, 20)
  pid <- as.integer(readLines(pid_file))
  Sys.sleep(0.2)
  expect_false(process_alive(pid))
  expect_true(dir.exists(e$run_dir))
  unlink(e$run_dir, recursive = TRUE)
})

test_that("ss_coverage() sends only the five columns and returns one row per variable", {
  dir <- local_fake_bridge()
  cov <- ss_coverage(fake_cohort(), c("ndi", "temis"))
  expect_equal(cov$variable, c("ndi", "temis"))
  expect_equal(cov$coverage_pct, c(100, 100))
  expect_equal(attr(cov, "episodes"), 6L)
  seen <- attr(last_request(dir, "coverage"), "seen")
  expect_equal(seen$cohort_columns, c("pid", "startDate", "endDate", "longitude", "latitude"))
})
