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
})
