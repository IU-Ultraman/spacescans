link <- function(cohort = fake_cohort(), variables = c("ndi", "walkability"), ...) {
  ss_link(cohort, variables, data_dir = tempdir(), progress = FALSE, cache = FALSE, ...)
}

test_that("ss_link() joins the exposures onto the cohort, keeping its types and attributes", {
  dir <- local_fake_bridge()
  co <- fake_cohort()
  res <- link(co, output_level = "episode")
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
  expect_equal(attr(res, "output_level"), "episode")
  expect_equal(info$output_level, "episode")
  expect_equal(last_request(dir, "link")$args$output_level, "episode")
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
  expect_length(unlist(req$args$reserved_names), 0L)     # a participant table takes no user names
  expect_equal(req$args$output_level, "participant")      # the default, always sent
  expect_null(req$args$datasets)
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
  res <- link(co, "ndi", output_level = "episode")
  expect_equal(unlist(last_request(dir, "link")$args$reserved_names), names(co))
  expect_equal(res$ndi, rep("user value", 6))
  expect_true("ndi_ndi_dup" %in% names(res))
})

test_that("a variable with fewer than 90% matched episodes warns", {
  dir <- local_fake_bridge(list(link = list(na_frac = list(walkability = 0.5))))
  expect_warning(res <- link(output_level = "episode"), "only 50.0% of episodes got a walkability value")
  expect_equal(sum(is.na(res$NatWalkInd)), 3L)
})

test_that("episode ids that do not match the cohort are an internal error", {
  dir <- local_fake_bridge(list(link = list(bad_ids = TRUE)))
  e <- tryCatch(link(output_level = "episode"), error = identity)
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

test_that("the missing-extra preflight reads the bridge catalog's missing_extras", {
  # The Python bridge's catalog rows name the extras this install lacks
  # (`missing_extras`), not every extra a variable needs (`extras`).
  row <- function(key, experiment, missing) {
    list(key = key, label = key, experiment = experiment, value_cols = list(key),
         datasets = list(), missing_extras = missing, runnable_here = !length(missing))
  }
  cat <- list(variables = list(row("ndi", "bg_ndi_wi", list()), row("walkability", "bg_ndi_wi", list()),
                               row("temis", "temis", list("hdf4"))),
              features = list(), data_release = "2026.07", catalog_hash = "x")
  dir <- local_fake_bridge(list(catalog = list(value = cat)))
  e <- tryCatch(link(variables = c("ndi", "temis")), error = identity)
  expect_s3_class(e, "spacescans_error_missing_extra")
  expect_match(conditionMessage(e), "temis need(s) the Python extra(s) hdf4", fixed = TRUE)
  expect_match(e$hint, 'ss_install(extras = "hdf4")', fixed = TRUE)
  expect_length(fake_requests(dir, "link"), 0L)
  dir <- local_fake_bridge(list(catalog = list(value = cat)))
  res <- link(variables = c("ndi", "walkability"))
  expect_length(fake_requests(dir, "link"), 1L)
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
  expect_error(ss_link(fake_cohort(), "ndi", buffer_m = 0), "`buffer_m` must be .* at least 1", class = "spacescans_error_config")
  expect_error(ss_link(fake_cohort(), character()), class = "spacescans_error_config")
  expect_error(ss_link(fake_cohort(), "ndi", threads = 0), class = "spacescans_error_config")
  expect_error(ss_link(fake_cohort(), "ndi", output_level = "patient"), '"participant", "episode"',
               class = "spacescans_error_config")
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

test_that("a custom dataset's columns are described by its definition, not the catalog (decision 3)", {
  dir <- local_fake_bridge()
  d <- withr::local_tempdir()
  csv <- file.path(d, "survey.csv")
  writeLines(c("FIPS,score,heat", "18097000100,0.5,1.5"), csv)
  def <- ss_custom_polygon(csv, boundary = "Tract", key_col = "FIPS", value_cols = c("score", "heat"),
                           name = "Our survey", description = "Scores from our own survey.",
                           labels = c(score = "Survey score"), units = c(heat = "degC"))
  opts <- last_request(dir, "custom-validate")$args$options
  expect_equal(opts$label, "Our survey")
  expect_equal(opts$description, "Scores from our own survey.")
  expect_equal(opts$labels, list(score = "Survey score"))
  expect_equal(opts$units, list(heat = "degC"))
  res <- link(variables = "ndi", custom = def)
  # R sends the definition back unchanged, and keeps the rows the bridge wrote for it
  sent <- last_request(dir, "link")$args$custom[[def$variable_key]]
  expect_equal(sent$value_labels, list(score = "Survey score"))
  expect_equal(sent$value_units, list(heat = "degC"))
  expect_equal(sent$description, "Scores from our own survey.")
  f <- attr(res, "features")
  expect_equal(f$feature_name, c("ndi", "score", "heat"))
  expect_equal(f$short_description, c("fake ndi", "Survey score", "Our survey (heat)"))
  expect_equal(f$detailed_description[2:3],
               c("Scores from our own survey.", "Scores from our own survey. Unit: degC."))
  expect_error(ss_custom_polygon(csv, boundary = "Tract", key_col = "FIPS", value_cols = "score",
                                 description = c("a", "b")),
               class = "spacescans_error_config")
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


test_that("an unknown on_error is a spacescans_error_config (review F11)", {
  dir <- local_fake_bridge()
  e <- tryCatch(link(on_error = "ignore"), error = identity)
  expect_s3_class(e, "spacescans_error_config")
  expect_match(conditionMessage(e), '"stop", "continue"', fixed = TRUE)
  expect_length(fake_requests(dir, "link"), 0L)
})

test_that("each link warning is shown once (review F4)", {
  ws <- list(list(code = "cohort_date_missing", message = "Blank endDate in 2 rows (rows 3, 5)"),
             list(code = "renamed", message = "noise: column x renamed to x_noise_dup"),
             list(code = "low_match", message = "walkability: 50% of episodes got a value"))
  dir <- local_fake_bridge(list(link = list(warnings = ws, result_warnings = ws,
                                            na_frac = list(walkability = 0.5))))
  co <- fake_cohort()
  co$endDate[c(3, 5)] <- NA
  got <- character()
  res <- withCallingHandlers(link(co), warning = function(w) {
    got <<- c(got, conditionMessage(w))
    invokeRestart("muffleWarning")
  })
  expect_equal(sum(grepl("blank in rows 3, 5", got, ignore.case = TRUE)), 1L)  # R's own check
  expect_equal(sum(grepl("Blank endDate", got)), 0L)
  expect_equal(sum(grepl("x_noise_dup", got)), 1L)
  expect_equal(sum(grepl("walkability", got)), 1L)                             # R's coverage warning
  expect_match(got[grepl("blank", got)], "no exposure values (static or time-varying)", fixed = TRUE)
})

test_that("an error while joining keeps the run directory and says how to remove it (review P4)", {
  dir <- local_fake_bridge(list(link = list(bad_ids = TRUE)))
  e <- tryCatch(link(output_level = "episode"), error = identity)
  expect_s3_class(e, "spacescans_error_internal")
  expect_match(conditionMessage(e), paste0("Run directory kept at ", e$run_dir), fixed = TRUE)
  expect_match(conditionMessage(e), "unlink(", fixed = TRUE)
  expect_true(dir.exists(e$run_dir))
  unlink(e$run_dir, recursive = TRUE)
})

test_that("terms-only data gets an ss_fetch(accept_terms = ) hint, not 'by hand' (review F3)", {
  st <- list(data_dir = "/x", data_release = "2026.07", deep = FALSE, ready = FALSE,
             variables = list(list(variable = "faqsd", status = "action_required", usable = FALSE,
                                   datasets = list("faqsd"), chosen = list("faqsd"),
                                   not_usable = list("faqsd"), download_bytes = 0, disk_bytes = 0,
                                   `next` = "spacescans data fetch faqsd --accept-terms epa-faqsd")),
             datasets = list(list(dataset = "faqsd", status = "action_required", usable = FALSE,
                                  title = "EPA FAQSD", acquire = "provider", scripted = TRUE,
                                  terms = list(id = "epa-faqsd", url = "https://www.epa.gov/"),
                                  download_bytes = 0, disk_bytes = 0, present = 0L, expected = 7L,
                                  `next` = "spacescans data fetch --dataset faqsd --accept-terms epa-faqsd")),
             download_bytes = 0, disk_bytes = 0, free_bytes = 1e12, next_steps = list(),
             warnings = list())
  dir <- local_fake_bridge(list(`data-status` = list(value = st)))
  e <- tryCatch(link(variables = "faqsd"), error = identity)
  expect_s3_class(e, "spacescans_error_data_missing")
  expect_equal(e$hint, 'Run ss_fetch("faqsd", accept_terms = "epa-faqsd") to download the missing data.')
  expect_no_match(conditionMessage(e), "by hand")
  expect_match(conditionMessage(e), 'ss_data_terms("faqsd")', fixed = TRUE)
  stt <- ss_data_status("faqsd", data_dir = tempdir())
  expect_equal(stt[["next"]], 'ss_fetch("faqsd", accept_terms = "epa-faqsd")')
  expect_equal(attr(stt, "datasets")[["next"]], 'ss_fetch(datasets = "faqsd", accept_terms = "epa-faqsd")')
})

test_that("a data.table comes back extendable by reference, the input untouched (review F7)", {
  pkg <- "data.table"                      # optional: spacescans does not depend on it
  skip_if_not_installed(pkg)
  dt_fun <- function(name) getExportedValue(pkg, name)
  dir <- local_fake_bridge()
  co <- dt_fun("as.data.table")(fake_cohort())
  before <- names(co)
  res <- link(co, output_level = "episode")
  expect_s3_class(res, "data.table")
  expect_equal(names(co), before)
  expect_null(attr(co, "features"))
  expect_equal(attr(res, "features")$feature_name, c("ndi", "NatWalkInd"))
  expect_no_warning(dt_fun("set")(res, j = "flag", value = 1L))
  expect_equal(res$flag, rep(1L, 6))
})

test_that("bridges that must not outlive R are told R's pid; a persistent job is not (review P2)", {
  dir <- local_fake_bridge()
  link()
  expect_equal(attr(last_request(dir, "link"), "seen")$bridge_parent, as.character(Sys.getpid()))
  expect_equal(attr(last_request(dir, "data-status"), "seen")$bridge_parent, as.character(Sys.getpid()))
  rd <- file.path(withr::local_tempdir(), "persistent")
  job <- link(run_dir = rd, wait = FALSE)
  ss_job_wait(job, progress = FALSE)
  expect_equal(attr(last_request(dir, "link"), "seen")$bridge_parent, "")
})

# --- participant level (phase D: the default) ----------------------------------

test_that("by default ss_link() returns one row per participant, in order of first appearance", {
  dir <- local_fake_bridge()
  co <- fake_cohort()
  res <- link(co)
  expect_s3_class(res, "data.frame")
  expect_equal(names(res), c("pid", "ndi", "NatWalkInd"))      # no dates, coordinates or episode ids
  expect_equal(res$pid, c("A", "B", "C", "D"))
  expect_equal(nrow(res), 4L)
  # the fake bridge weights each episode by its days (366 for every fake episode)
  ep <- round(seq_len(6) / 7 + 1, 4)
  expect_equal(res$ndi, c(mean(ep[1:2]), ep[3], mean(ep[4:5]), ep[6]))
  expect_equal(attr(res, "output_level"), "participant")
  cov <- attr(res, "coverage")
  expect_equal(names(cov)[1:4], c("variable", "participants", "with_value", "pct"))
  expect_equal(cov$participants, c(4L, 4L))
  expect_equal(cov$pct, c(100, 100))
  expect_equal(attr(res, "features")$feature_name, c("ndi", "NatWalkInd"))
  expect_equal(attr(res, "provenance")$output_level, "participant")
  info <- attr(res, "spacescans")
  expect_equal(info$output_level, "participant")
  expect_equal(info$participants, 4L)
  expect_equal(info$episodes, 6L)
  expect_null(attr(res, "study"))                 # a new table, not the user's data frame
  expect_equal(last_request(dir, "link")$args$output_level, "participant")
})

test_that("pid keeps the type of the user's id column, whatever it is called", {
  dir <- local_fake_bridge()
  co <- fake_cohort()
  co$pid <- c(101L, 101L, 7L, 55L, 55L, 9L)
  res <- link(co, "ndi")
  expect_identical(res$pid, c(101L, 7L, 55L, 9L))
  co$pid <- factor(c("x", "x", "y", "z", "z", "w"), levels = c("w", "x", "y", "z"))
  res <- link(co, "ndi")
  expect_identical(res$pid, factor(c("x", "y", "z", "w"), levels = c("w", "x", "y", "z")))
  names(co)[names(co) == "pid"] <- "participant_id"
  res <- link(co, "ndi", cols = c(pid = "participant_id"))
  expect_equal(names(res), c("pid", "ndi"))
  expect_equal(as.character(res$pid), c("x", "y", "z", "w"))
})

test_that("participants without a value are kept with NA, and a low linkage rate counts participants", {
  dir <- local_fake_bridge(list(link = list(na_frac = list(walkability = 0.5))))
  expect_warning(res <- link(), "only 50.0% of participants got a walkability value")
  expect_equal(res$pid, c("A", "B", "C", "D"))
  expect_equal(is.na(res$NatWalkInd), c(TRUE, TRUE, FALSE, FALSE))
  cov <- attr(res, "coverage")
  expect_equal(cov$with_value[cov$variable == "walkability"], 2L)
})

test_that("a participant table that does not match the cohort is an internal error", {
  dir <- local_fake_bridge(list(link = list(bad_pids = TRUE)))
  e <- tryCatch(link(), error = identity)
  expect_s3_class(e, "spacescans_error_internal")
  expect_match(conditionMessage(e), "expected 4 participants in order of first appearance")
  expect_true(dir.exists(e$run_dir))
  unlink(e$run_dir, recursive = TRUE)
})

test_that("a bridge that returns episodes for a participant request is refused with the ss_install() hint", {
  dir <- local_fake_bridge(list(link = list(ignore_level = TRUE)))
  e <- tryCatch(link(), error = identity)
  expect_s3_class(e, "spacescans_error_protocol")
  expect_match(conditionMessage(e), "returned one row per location episode")
  expect_match(e$hint, "ss_install()", fixed = TRUE)
  unlink(e$run_dir, recursive = TRUE)
  # the same bridge still gives what an episode-level request asks for
  res <- link(output_level = "episode")
  expect_equal(nrow(res), 6L)
})

test_that("a participant table is recognised by its shape when the bridge does not echo the level", {
  dir <- local_fake_bridge(list(link = list(no_level_echo = TRUE)))
  res <- link()
  expect_equal(attr(res, "output_level"), "participant")
  expect_equal(res$pid, c("A", "B", "C", "D"))
})

test_that("linkage rates a bridge reports as patients are shown per participant", {
  dir <- local_fake_bridge(list(link = list(coverage_shape = "patients",
                                            na_frac = list(walkability = 0.5))))
  expect_warning(res <- link(), "only 50.0% of participants")
  cov <- attr(res, "coverage")
  expect_equal(names(cov)[1:4], c("variable", "participants", "with_value", "pct"))
  expect_equal(cov$participants, c(4L, 4L))
  expect_equal(cov$with_value, c(4L, 2L))
  expect_equal(cov$pct, c(100, 50))
  expect_false(any(c("patients", "patients_with_value") %in% names(cov)))
})

test_that("a data.table cohort gives a data.table participant table (review F7)", {
  pkg <- "data.table"
  skip_if_not_installed(pkg)
  dt_fun <- function(name) getExportedValue(pkg, name)
  dir <- local_fake_bridge()
  co <- dt_fun("as.data.table")(fake_cohort())
  res <- link(co)
  expect_s3_class(res, "data.table")
  expect_equal(names(res), c("pid", "ndi", "NatWalkInd"))
  expect_equal(attr(res, "output_level"), "participant")
  expect_no_warning(dt_fun("set")(res, j = "flag", value = 1L))
})

test_that("no participant id reaches job.json or the request", {
  dir <- local_fake_bridge()
  co <- fake_cohort()
  co$pid <- c("PIDQ1", "PIDQ1", "PIDQ2", "PIDQ3", "PIDQ3", "PIDQ4")
  res <- link(co, keep = TRUE)
  rd <- attr(res, "spacescans")$run_dir
  on.exit(unlink(rd, recursive = TRUE))
  for (f in c("job.json", "request.json", "result.json", "events.jsonl")) {
    expect_false(any(grepl("PIDQ", readLines(file.path(rd, f)), fixed = TRUE)), label = f)
  }
  job <- jsonlite::read_json(file.path(rd, "job.json"))
  expect_equal(job$meta$output_level, "participant")
  expect_equal(job$meta$participants, 4L)
})
