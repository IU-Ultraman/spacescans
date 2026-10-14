# Opt-in tests against the real Python bridge: SPACESCANS_LIVE=1 and
# SPACESCANS_PYTHON=<python with spacescans>. A test whose bridge command the
# installed spacescans does not list (hello's `commands`) skips, so the same
# file works before and after each phase-B bridge command lands.
#
# Further opt-ins:
#   SPACESCANS_LIVE_DATA_DIR  a data directory to read (status checks only;
#                             the tests verify that nothing in it changed)
#   SPACESCANS_LIVE_LINK=1    also run a small real ss_link() (needs the data)

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
  res <- ss_link(co, "walkability", data_dir = d, progress = FALSE, threads = 2)
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
  res2 <- ss_link(co, "walkability", data_dir = d, progress = FALSE, threads = 2, keep = TRUE)
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
