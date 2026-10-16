# Opt-in tests against the real Python bridge: SPACESCANS_LIVE=1 and
# SPACESCANS_PYTHON=<python with spacescans>. A test whose bridge command the
# installed spacescans does not list (hello's `commands`) skips, so the same
# file works before and after each phase-B bridge command lands.
#
# Further opt-ins:
#   SPACESCANS_LIVE_DATA_DIR  a data directory to read (status checks only;
#                             the tests verify that nothing in it changed)
#   SPACESCANS_LIVE_LINK=1    also run small real links: the registered-dataset
#                             example (no catalog data needed) and, with the data
#                             directory (walkability and noise), the minimal
#                             example and links at both output levels
#
# Registry tests skip while the installed bridge has no datasets-* commands.

skip_if_not_live <- function() {
  if (!identical(Sys.getenv("SPACESCANS_LIVE"), "1")) skip("live bridge tests need SPACESCANS_LIVE=1")
  skip_if_no_python()
}

live_caps <- function() {
  reset_spacescans_state()
  spacescans:::.ss_hello()
}

skip_if_no_command <- function(command) {
  cmds <- unlist(live_caps()$commands)
  if (!command %in% cmds) skip(sprintf("the installed bridge has no '%s' command yet", command))
}

skip_if_not_live_link <- function() {
  if (!identical(Sys.getenv("SPACESCANS_LIVE_LINK"), "1")) skip("set SPACESCANS_LIVE_LINK=1 to run a real link")
}

# A registry in a temporary directory for the calling test.
local_live_registry <- function(env = parent.frame()) {
  root <- withr::local_tempdir(.local_envir = env)
  withr::local_options(spacescans.datasets_dir = root, .local_envir = env)
  root
}

example_script <- function(name) {
  path <- system.file("examples", name, package = "spacescans")
  if (!nzchar(path)) path <- test_path("..", "..", "inst", "examples", name)
  normalizePath(path)
}

# Run an example script in a fresh environment, its printing captured.
run_example <- function(name) {
  env <- new.env(parent = globalenv())
  utils::capture.output(sys.source(example_script(name), envir = env))
  env
}

live_data_dir <- function() {
  d <- Sys.getenv("SPACESCANS_LIVE_DATA_DIR", "")
  if (!nzchar(d) || !dir.exists(d)) skip("SPACESCANS_LIVE_DATA_DIR is not set")
  normalizePath(d)
}

# Fail the test if anything under `dir` is newer than a stamp taken first.
local_untouched <- function(dir, env = parent.frame()) {
  stamp <- withr::local_tempfile(.local_envir = env)
  file.create(stamp)
  Sys.sleep(1.1)
  withr::defer({
    newer <- system2("find", c(shQuote(dir), "-newer", shQuote(stamp)), stdout = TRUE, stderr = FALSE)
    expect_equal(newer, character(), label = "files changed in the data directory")
  }, envir = env)
}

live_cohort <- function(n = 12) {
  data.frame(
    pid = sprintf("L%03d", rep(seq_len(n / 2), each = 2)),
    startDate = as.Date("2016-01-01") + rep(c(0, 400), n / 2),
    endDate = as.Date("2016-12-31") + rep(c(0, 400), n / 2),
    longitude = -86.16 + seq_len(n) / 1000,
    latitude = 39.77 + seq_len(n) / 1000,
    note = "kept in R"
  )
}

test_that("live: hello reports protocol 1 and the commands", {
  skip_if_not_live()
  local_clean_state()
  caps <- live_caps()
  expect_equal(as.integer(caps$protocol), 1L)
  expect_true(all(c("capabilities", "hello", "run", "schema", "templates") %in% unlist(caps$commands)))
  v <- ss_version()
  expect_equal(v$protocol, 1L)
  expect_equal(v$python_package, spacescans:::.ss_expected()$version)
  skip_if_no_command("datasets-register")
  expect_true(all(c("datasets-register", "datasets-list", "datasets-get", "datasets-update",
                    "datasets-remove") %in% unlist(caps$commands)))
})

test_that("live: ss_doctor() runs every check", {
  skip_if_not_live()
  local_clean_state()
  withr::local_options(spacescans.data_dir = withr::local_tempdir())
  out <- NULL
  expect_output(out <- ss_doctor())
  expect_equal(out$status[out$check == "bridge"], "ok")
  expect_true("cache directory" %in% out$check)
})

test_that("live: init templates are listed with kind = 'init'", {
  skip_if_not_live()
  local_clean_state()
  tpl <- ss_templates(kind = "init")
  expect_true("c4/zbp" %in% tpl$name)
  expect_equal(ss_templates("c4/zbp", kind = "init")$linkage_pattern, "yearly_areal")
})

test_that("live: catalog templates are the 25 shipped ones", {
  skip_if_not_live()
  skip_if_no_command("catalog")
  local_clean_state()
  tpl <- ss_templates()
  expect_equal(nrow(tpl), 25L)
})

test_that("live: ss_catalog() and ss_features() describe the 11 variables", {
  skip_if_not_live()
  skip_if_no_command("catalog")
  local_clean_state()
  cat <- ss_catalog()
  expect_true(all(c("ndi", "walkability", "temis", "faqsd") %in% cat$variable))
  expect_equal(nrow(cat), 11L)
  expect_false(anyNA(cat$label))
  f <- ss_features()
  expect_true(all(c("feature_name", "short_description") %in% names(f)))
  expect_gt(nrow(ss_features("ndi")), 0L)
})

test_that("live: ss_data_status() on an empty directory reports missing data and next steps", {
  skip_if_not_live()
  skip_if_no_command("data-status")
  local_clean_state()
  d <- withr::local_tempdir()
  st <- ss_data_status(c("ndi", "walkability"), data_dir = d)
  expect_equal(st$variable, c("ndi", "walkability"))
  expect_false(any(st$usable))
  expect_false(attr(st, "ready"))
  expect_equal(list.files(d, all.files = TRUE, no.. = TRUE), character())
})

test_that("live: ss_data_status() reads a real data directory without changing it", {
  skip_if_not_live()
  skip_if_no_command("data-status")
  local_clean_state()
  d <- live_data_dir()
  local_untouched(d)
  st <- ss_data_status(data_dir = d)
  expect_equal(nrow(st), 11L)
  expect_true(all(st$status %in% c("ready", "differs_from_reference", "unverified", "incomplete",
                                   "missing", "action_required", "corrupt")))
})

test_that("live: ss_fetch(dry_run = TRUE) plans without downloading", {
  skip_if_not_live()
  skip_if_no_command("data-plan")
  local_clean_state()
  d <- withr::local_tempdir()
  expect_output(plan <- ss_fetch("ndi", data_dir = d, dry_run = TRUE))
  expect_s3_class(plan, "ss_fetch_plan")
  expect_equal(list.files(d, all.files = TRUE, no.. = TRUE, recursive = TRUE), character())
})

test_that("live: ss_data_terms() shows a provider's terms", {
  skip_if_not_live()
  skip_if_no_command("data-terms")
  local_clean_state()
  t <- ss_data_terms("faqsd")
  expect_equal(t$dataset, "faqsd")
  expect_output(print(t), "faqsd")
})

test_that("live: ss_coverage() estimates coverage from five columns", {
  skip_if_not_live()
  skip_if_no_command("coverage")
  local_clean_state()
  cov <- ss_coverage(live_cohort(), c("ndi", "walkability"))
  expect_equal(cov$variable, c("ndi", "walkability"))
  expect_true(all(cov$pct >= 0 & cov$pct <= 100))
})

test_that("live: ss_custom_polygon() validates a CSV through custom-validate", {
  skip_if_not_live()
  skip_if_no_command("custom-validate")
  local_clean_state()
  d <- withr::local_tempdir()
  csv <- file.path(d, "tract_values.csv")
  writeLines(c("GEOID,score", "18097310100,0.25", "18097310200,0.75"), csv)
  def <- ss_custom_polygon(csv, boundary = "Tract", key_col = "GEOID", value_cols = "score", name = "Live test")
  expect_s3_class(def, "ss_custom")
  expect_match(def$variable_key, "^custom_")
  expect_equal(unlist(def$value_cols), "score")
})

test_that("live: ss_cache_info() and ss_cache_clear() work on an empty cache", {
  skip_if_not_live()
  skip_if_no_command("cache-info")
  local_clean_state()
  root <- withr::local_tempdir()
  info <- ss_cache_info(cache_dir = root)
  expect_equal(as.integer(info$entries %||% 0L), 0L)
  skip_if_no_command("cache-clear")
  ss_cache_clear(cache_dir = root)
})

test_that("live: R and the bridge keep run directories under one root and sweep each other's", {
  skip_if_not_live()
  skip_if_no_command("runs-sweep")
  local_clean_state()
  root <- withr::local_tempdir()
  withr::local_envvar(R_USER_CACHE_DIR = root)
  runs <- spacescans:::.ss_runs_root()
  expect_equal(runs, file.path(tools::R_user_dir("spacescans", "cache"), "runs"))
  # Python computes the same root as tools::R_user_dir() (no cache_root sent)
  v <- spacescans:::.ss_bridge("runs-sweep", list(dry_run = TRUE))
  expect_equal(normalizePath(v$root), normalizePath(runs))
  # a directory R made for a session that is gone: the bridge's sweep removes it
  p <- processx::process$new("/bin/sh", c("-c", "sleep 0.3"))
  pid <- p$get_pid()
  p$wait(10000)
  stale <- file.path(runs, "ss-gone")
  dir.create(stale, recursive = TRUE)
  writeLines("cohort", file.path(stale, "cohort.parquet"))
  spacescans:::.ss_write_owner(stale, pid = pid)
  mine <- spacescans:::.ss_new_run_dir()
  v <- spacescans:::.ss_bridge("runs-sweep", list())
  expect_equal(unlist(v$removed), "ss-gone")
  expect_false(dir.exists(stale))
  expect_true(dir.exists(mine))            # owned by this live session
  # ss_cache_info() counts a directory in use once, though R and the bridge both sweep
  info <- ss_cache_info(cache_dir = withr::local_tempdir())
  expect_equal(info$runs$kept, 1L)
  expect_equal(info$runs$removed, character())
  unlink(mine, recursive = TRUE)
})

test_that("live: a custom definition carries the label, units and description R gave", {
  skip_if_not_live()
  skip_if_no_command("custom-validate")
  local_clean_state()
  d <- withr::local_tempdir()
  csv <- file.path(d, "tract_values.csv")
  writeLines(c("GEOID,score,heat", "18097310100,0.25,1.5", "18097310200,0.75,2.5"), csv)
  def <- ss_custom_polygon(csv, boundary = "Tract", key_col = "GEOID", value_cols = c("score", "heat"),
                           name = "Live survey", description = "Scores from a live test.",
                           labels = c(score = "Survey score"), units = c(heat = "degC"))
  expect_equal(def$label, "Live survey")
  expect_equal(def$description, "Scores from a live test.")
  expect_equal(def$value_labels$score, "Survey score")
  expect_equal(def$value_units$heat, "degC")
})

test_that("live: ss_link() links a small cohort and leaves the data directory untouched", {
  skip_if_not_live()
  if (!identical(Sys.getenv("SPACESCANS_LIVE_LINK"), "1")) skip("set SPACESCANS_LIVE_LINK=1 to run a real link")
  skip_if_no_command("link")
  local_clean_state()
  d <- live_data_dir()
  local_untouched(d)
  withr::local_envvar(R_USER_CACHE_DIR = withr::local_tempdir())
  co <- live_cohort()
  res <- ss_link(co, "walkability", output_level = "episode", data_dir = d, progress = FALSE, threads = 2)
  expect_equal(nrow(res), nrow(co))
  expect_equal(res$note, co$note)
  expect_true(ncol(res) > ncol(co))
  expect_s3_class(attr(res, "coverage"), "data.frame")
  prov <- attr(res, "provenance")
  expect_false(any(grepl("L001", unlist(prov), fixed = TRUE)))
  # decision 2: the run directory was under the per-user root and is gone after success
  runs <- spacescans:::.ss_runs_root()
  expect_equal(list.files(runs, all.files = TRUE, no.. = TRUE), character())
  # kept on request: there, private, owned by this session; the C3 step is a cache hit
  res2 <- ss_link(co, "walkability", output_level = "episode", data_dir = d, progress = FALSE,
                  threads = 2, keep = TRUE)
  rd <- attr(res2, "spacescans")$run_dir
  on.exit(unlink(rd, recursive = TRUE), add = TRUE)
  expect_equal(dirname(rd), normalizePath(runs))
  expect_equal(as.character(file.info(rd)$mode), "700")
  owner <- jsonlite::read_json(file.path(rd, "owner.json"))
  expect_equal(owner$pid, Sys.getpid())
  expect_false(any(grepl("L001|-86\\.1|2016-", readLines(file.path(rd, "owner.json")))))
  expect_equal(res2$NatWalkInd, res$NatWalkInd)
  expect_true("cache_hit" %in% attr(res2, "spacescans")$steps$status)
})

test_that("live: a real link describes a custom dataset's column by its definition (decision 3)", {
  skip_if_not_live()
  if (!identical(Sys.getenv("SPACESCANS_LIVE_LINK"), "1")) skip("set SPACESCANS_LIVE_LINK=1 to run a real link")
  skip_if_no_command("link")
  local_clean_state()
  d <- live_data_dir()
  local_untouched(d)
  withr::local_envvar(R_USER_CACHE_DIR = withr::local_tempdir())
  vals <- file.path(withr::local_tempdir(), "tract_scores.csv")
  writeLines(c("GEOID,score", "18097310100,0.25", "18097310200,0.75"), vals)
  def <- ss_custom_polygon(vals, boundary = "Tract", key_col = "GEOID", value_cols = "score",
                           name = "Live survey", description = "Scores from a live test.",
                           units = c(score = "points"))
  res <- suppressWarnings(ss_link(live_cohort(), character(), custom = def, data_dir = d,
                                  progress = FALSE, threads = 2))
  f <- attr(res, "features")
  expect_equal(f$feature_name, "score")
  expect_equal(f$short_description, "Live survey")
  expect_equal(f$detailed_description, "Scores from a live test. Unit: points.")
  expect_equal(list.files(spacescans:::.ss_runs_root(), all.files = TRUE, no.. = TRUE), character())
})

# --- phase D: output levels and registered datasets -----------------------------

test_that("live: participant level is the day-weighted mean of the episode level (walkability)", {
  skip_if_not_live()
  skip_if_not_live_link()
  skip_if_no_command("link")
  local_clean_state()
  d <- live_data_dir()
  local_untouched(d)
  withr::local_envvar(R_USER_CACHE_DIR = withr::local_tempdir())
  co <- live_cohort()
  ep <- ss_link(co, "walkability", output_level = "episode", data_dir = d, progress = FALSE, threads = 2)
  res <- ss_link(co, "walkability", data_dir = d, progress = FALSE, threads = 2)
  expect_equal(attr(res, "output_level"), "participant")
  expect_equal(names(res), c("pid", "NatWalkInd"))
  expect_equal(res$pid, unique(co$pid))
  # walkability is time-invariant: every day of an episode with a value contributes
  days <- as.numeric(co$endDate - co$startDate) + 1
  want <- vapply(unique(co$pid), function(p) {
    k <- co$pid == p & !is.na(ep$NatWalkInd)
    if (!any(k)) NA_real_ else stats::weighted.mean(ep$NatWalkInd[k], days[k])
  }, numeric(1), USE.NAMES = FALSE)
  expect_equal(res$NatWalkInd, want, tolerance = 1e-9)
  cov <- attr(res, "coverage")
  expect_equal(cov$participants, length(unique(co$pid)))
  expect_equal(cov$with_value, sum(!is.na(res$NatWalkInd)))
  expect_equal(attr(res, "provenance")$run$output_level, "participant")
})

test_that("live: the minimal example (three participants, NPS noise) runs and checks itself", {
  skip_if_not_live()
  skip_if_not_live_link()
  skip_if_no_command("link")
  local_clean_state()
  d <- live_data_dir()
  if (!isTRUE(attr(ss_data_status("noise", data_dir = d), "ready"))) skip("the noise data is not in the data directory")
  local_untouched(d)
  withr::local_envvar(R_USER_CACHE_DIR = withr::local_tempdir())
  withr::local_options(spacescans.data_dir = d)
  env <- run_example("minimal-example.R")            # stops if P2's value is not the weighted mean
  expect_equal(env$res$pid, c("P1", "P2", "P3"))
  expect_equal(names(env$res), c("pid", "l50dba_exi", "l50dba_imp", "l50dba_nat"))
  expect_false(anyNA(env$res$l50dba_exi))
  expect_equal(nrow(env$episodes), 4L)
  expect_equal(attr(env$res, "coverage")$pct, 100)
})

test_that("live: registered datasets: register, list, get, update and remove (polygon table)", {
  skip_if_not_live()
  skip_if_no_command("datasets-register")
  local_clean_state()
  root <- local_live_registry()
  csv <- file.path(withr::local_tempdir(), "county_scores.csv")
  writeLines(c("GEOID,score", "18097,0.25", "18057,0.75"), csv)
  rec <- ss_register_dataset(csv, name = "County scores", kind = "polygon_table", temporal = "static",
                             key = list(boundary = "county", column = "GEOID"),
                             variables = data.frame(name = "score", unit = "points"),
                             source = "live test", version = "1")
  expect_s3_class(rec, "ss_dataset")
  expect_equal(rec$name, "County scores")
  expect_match(rec$variable_key, "^custom_[0-9a-f]{8}$")
  expect_equal(rec$variables$name, "score")
  expect_equal(rec$variables$unit, "points")
  expect_equal(rec$visibility, "private")
  expect_true(length(rec$files) >= 1L)
  expect_true(file.exists(csv))                                   # the original stays
  ls <- ss_datasets()
  expect_equal(ls$name, "County scores")
  expect_equal(normalizePath(attr(ls, "root")), normalizePath(root))
  expect_equal(ss_dataset(rec$id)$name, "County scores")
  expect_equal(ss_dataset("county SCORES")$id, rec$id)
  expect_equal(ss_dataset(rec$variable_key)$id, rec$id)
  expect_error(ss_register_dataset(csv, name = "County scores", kind = "polygon_table",
                                   temporal = "static", key = "county"),
               class = "spacescans_error_config")
  up <- ss_update_dataset(rec, visibility = "deployment", description = "Two counties.")
  expect_equal(up$visibility, "deployment")
  expect_equal(up$source, "live test")
  expect_equal(up$id, rec$id)
  expect_message(ss_remove_dataset("County scores"), "Removed")
  expect_equal(nrow(ss_datasets()), 0L)
  expect_error(ss_dataset("County scores"), class = "spacescans_error_config")
})

test_that("live: a registered county table links by name at participant level", {
  skip_if_not_live()
  skip_if_not_live_link()
  skip_if_no_command("datasets-register")
  local_clean_state()
  d <- live_data_dir()
  local_untouched(d)
  withr::local_envvar(R_USER_CACHE_DIR = withr::local_tempdir())
  local_live_registry()
  csv <- file.path(withr::local_tempdir(), "county_scores.csv")
  writeLines(c("GEOID,score", "18097,0.25", "18057,0.75"), csv)      # the cohort is in 18097
  rec <- ss_register_dataset(csv, name = "county scores", kind = "polygon_table", temporal = "static",
                             key = "county")
  co <- live_cohort()
  res <- ss_link(co, character(), custom = "county scores", data_dir = d, progress = FALSE, threads = 2)
  expect_equal(names(res), c("pid", "score"))
  expect_equal(res$pid, unique(co$pid))
  expect_equal(res$score, rep(0.25, length(unique(co$pid))))
  expect_equal(attr(res, "coverage")$pct, 100)
  ep <- ss_link(co, character(), custom = rec, output_level = "episode", data_dir = d, progress = FALSE,
                threads = 2)
  expect_equal(ep$score, rep(0.25, nrow(co)))
  expect_equal(ep$note, co$note)
})

test_that("live: the registered-dataset example links a features dataset by name", {
  skip_if_not_live()
  skip_if_not_live_link()
  skip_if_no_command("datasets-register")
  skip_if_no_geo()
  local_clean_state()
  local_live_registry()
  withr::local_envvar(R_USER_CACHE_DIR = withr::local_tempdir())
  withr::local_options(spacescans.data_dir = withr::local_tempdir())   # no catalog data needed
  env <- run_example("registered-dataset.R")
  expect_equal(env$rec$variables$name, "example_parks_dist_m")
  expect_equal(names(env$res), c("pid", "example_parks_dist_m"))
  expect_equal(env$res$pid, c("P1", "P2", "P3"))
  expect_true(env$res$example_parks_dist_m[1] > 0 && env$res$example_parks_dist_m[1] < 10000)
  expect_equal(nrow(ss_datasets()), 0L)                           # the example removed it
})

test_that("live: a features dataset at participant level is the day-weighted mean of its episodes", {
  skip_if_not_live()
  skip_if_not_live_link()
  skip_if_no_command("datasets-register")
  skip_if_no_geo()
  local_clean_state()
  local_live_registry()
  withr::local_envvar(R_USER_CACHE_DIR = withr::local_tempdir())
  d <- withr::local_tempdir()
  # the example's cohort (P2 has two location episodes) and three points
  co <- data.frame(
    pid = c("P1", "P2", "P2", "P3"),
    startDate = as.Date(c("2015-01-01", "2014-03-01", "2016-07-01", "2017-01-01")),
    endDate = as.Date(c("2017-12-31", "2016-06-30", "2019-12-31", "2018-06-30")),
    longitude = c(-86.1581, -87.6298, -84.3880, -122.4194),
    latitude = c(39.7684, 41.8781, 33.7490, 37.7749)
  )
  parks <- file.path(withr::local_tempdir(), "parks.geojson")
  pt <- function(x, y) sprintf('{"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [%s, %s]}}', x, y)
  writeLines(sprintf('{"type": "FeatureCollection", "features": [%s]}',
                     paste(pt(-86.17, 39.78), pt(-87.62, 41.88), pt(-84.37, 33.76), sep = ", ")), parks)
  ss_register_dataset(parks, name = "parks", kind = "features", temporal = "static")
  ep <- ss_link(co, character(), custom = "parks", output_level = "episode", data_dir = d, progress = FALSE)
  res <- ss_link(co, character(), custom = "parks", data_dir = d, progress = FALSE)
  expect_equal(res$pid, c("P1", "P2", "P3"))
  days <- as.numeric(co$endDate - co$startDate) + 1
  want <- vapply(unique(co$pid), function(p) {
    k <- co$pid == p & !is.na(ep$parks_dist_m)
    if (!any(k)) NA_real_ else stats::weighted.mean(ep$parks_dist_m[k], days[k])
  }, numeric(1), USE.NAMES = FALSE)
  expect_equal(res$parks_dist_m, want, tolerance = 1e-9)
})
