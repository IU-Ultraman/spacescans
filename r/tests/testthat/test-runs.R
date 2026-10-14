# Run directories under the per-user root and the sweep of dead owners' directories
# (phase-C decision 2). The suite's setup points R_USER_CACHE_DIR at a temp dir.

runs_root <- function() spacescans:::.ss_runs_root()

# The pid (and start time while it lived) of a process that has exited.
dead_process <- function() {
  p <- processx::process$new("/bin/sh", c("-c", "sleep 0.3"))
  pid <- p$get_pid()
  start <- spacescans:::.ss_process_start(pid)
  p$wait(10000)
  list(pid = pid, start = start)
}

make_run_dir <- function(name, owner, payload = strrep("x", 1000)) {
  d <- file.path(runs_root(), name)
  dir.create(d, recursive = TRUE, showWarnings = FALSE)
  writeLines(payload, file.path(d, "cohort.parquet"))
  if (!is.null(owner)) {
    writeLines(jsonlite::toJSON(owner, auto_unbox = TRUE, null = "null", digits = NA),
               file.path(d, "owner.json"))
  }
  d
}

own_owner <- function(...) {
  base <- list(schema = 1L, pid = Sys.getpid(), start_time = spacescans:::.ss_process_start(),
               host = spacescans:::.ss_hostname(), client = "test", created_at = "2026-10-13T00:00:00+0000")
  utils::modifyList(base, list(...))
}

test_that("the run root is R_user_dir('spacescans', 'cache')/runs and a new run dir is owned", {
  root <- withr::local_tempdir()
  withr::local_envvar(R_USER_CACHE_DIR = root)
  expect_equal(runs_root(), file.path(root, "R", "spacescans", "runs"))
  d <- spacescans:::.ss_new_run_dir()
  on.exit(unlink(d, recursive = TRUE), add = TRUE)
  expect_equal(dirname(d), normalizePath(runs_root()))
  expect_equal(as.character(file.info(d)$mode), "700")
  expect_equal(as.character(file.info(runs_root())$mode), "700")
  owner <- jsonlite::read_json(file.path(d, "owner.json"), simplifyVector = TRUE)
  expect_equal(owner$pid, Sys.getpid())
  expect_equal(owner$host, spacescans:::.ss_hostname())
  expect_equal(owner$schema, 1L)
  expect_match(owner$client, "^spacescans-r ")
  expect_lt(abs(owner$start_time - spacescans:::.ss_process_start()), 1e-3)
  expect_equal(as.character(file.info(file.path(d, "owner.json"))$mode), "600")
  expect_setequal(names(owner), c("schema", "pid", "start_time", "host", "client", "created_at"))
  # with a parent (output directories): an ordinary temp dir without a record
  o <- spacescans:::.ss_new_run_dir(file.path(tempdir(), "spacescans-output"))
  on.exit(unlink(o, recursive = TRUE), add = TRUE)
  expect_false(file.exists(file.path(o, "owner.json")))
})

test_that("without a usable run root the session falls back to tempdir() with one warning", {
  base <- withr::local_tempdir()
  blocker <- file.path(base, "not-a-dir")
  writeLines("x", blocker)
  withr::local_envvar(R_USER_CACHE_DIR = blocker)     # the root cannot be created under a file
  st <- spacescans:::.ss_state
  old <- st$runs_root_warned
  st$runs_root_warned <- NULL
  withr::defer(st$runs_root_warned <- old)
  d <- NULL
  expect_warning(d <- spacescans:::.ss_new_run_dir(), "cannot create the run directory root")
  on.exit(unlink(d, recursive = TRUE), add = TRUE)
  expect_equal(dirname(d), normalizePath(tempdir()))
  expect_equal(as.character(file.info(d)$mode), "700")
  expect_false(file.exists(file.path(d, "owner.json")))
  d2 <- NULL
  expect_no_warning(d2 <- spacescans:::.ss_new_run_dir())    # warned once per session
  on.exit(unlink(d2, recursive = TRUE), add = TRUE)
})

test_that("owner liveness: dead pid, reused pid, live pid, other host", {
  alive <- spacescans:::.ss_owner_alive
  expect_true(alive(own_owner()))
  expect_false(alive(own_owner(start_time = spacescans:::.ss_process_start() - 3600)))
  expect_true(alive(own_owner(start_time = NULL)))
  dead <- dead_process()
  expect_false(alive(own_owner(pid = dead$pid, start_time = dead$start)))
  expect_true(is.na(alive(own_owner(host = "some-other-host.example"))))
  expect_true(alive(own_owner(host = "")))            # no host recorded: judged here, as Python does
  expect_true(is.na(alive(own_owner(pid = NULL))))
  expect_true(is.na(alive(list())))
})

test_that("the sweep removes dead owners' run dirs, keeps live ones and counts", {
  root <- withr::local_tempdir()
  withr::local_envvar(R_USER_CACHE_DIR = root)
  dead <- dead_process()
  gone <- make_run_dir("ss-gone", own_owner(pid = dead$pid, start_time = dead$start), strrep("y", 2048))
  reused <- make_run_dir("ss-reused", own_owner(start_time = spacescans:::.ss_process_start() - 7200))
  live <- make_run_dir("ss-live", own_owner())
  foreign <- make_run_dir("ss-foreign", own_owner(pid = dead$pid, host = "elsewhere.example"))
  bare <- make_run_dir("ss-bare", NULL)
  writeLines("x", file.path(runs_root(), "not-a-dir.txt"))

  dry <- spacescans:::.ss_sweep_runs(dry_run = TRUE)
  expect_equal(dry$removed, c("ss-gone", "ss-reused"))
  expect_true(dir.exists(gone) && dir.exists(reused))
  expect_match(dry$message, "would remove 2 run directories")

  sw <- spacescans:::.ss_sweep_runs()
  expect_equal(sw$removed, c("ss-gone", "ss-reused"))
  expect_equal(sw$kept, "ss-live")
  expect_equal(sw$unknown, c("ss-bare", "ss-foreign"))
  expect_gt(sw$bytes, 3000)
  expect_equal(sw$errors, character())
  expect_false(dir.exists(gone) || dir.exists(reused))
  expect_true(dir.exists(live) && dir.exists(foreign) && dir.exists(bare))
  expect_match(sw$message, "removed 2 run directories left by processes that no longer exist")
  expect_false(grepl("cohort", sw$message))
  # a second sweep finds nothing and says nothing
  again <- spacescans:::.ss_sweep_runs()
  expect_equal(again$removed, character())
  expect_null(again$message)
  expect_equal(again$kept, "ss-live")
  # a missing root is an empty result and creates nothing
  withr::local_envvar(R_USER_CACHE_DIR = file.path(root, "never"))
  expect_equal(spacescans:::.ss_sweep_runs()$removed, character())
  expect_false(dir.exists(file.path(root, "never")))
})

test_that("ss_cache_clear(), ss_cache_info() and ss_doctor() sweep and report", {
  root <- withr::local_tempdir()
  withr::local_envvar(R_USER_CACHE_DIR = root)
  dir <- local_fake_bridge()
  dead <- dead_process()
  make_run_dir("ss-stale", own_owner(pid = dead$pid, start_time = dead$start))
  expect_message(ss_cache_clear(), "removed 1 run directory")
  expect_false(dir.exists(file.path(runs_root(), "ss-stale")))
  make_run_dir("ss-stale-2", own_owner(pid = dead$pid, start_time = dead$start))
  live <- make_run_dir("ss-live", own_owner())
  info <- NULL
  expect_message(info <- ss_cache_info(), "removed 1 run directory")
  expect_equal(info$runs$removed, "ss-stale-2")
  expect_equal(info$runs$kept, 1L)
  expect_output(print(info), "run dirs 1 in use, 1 removed")
  expect_true(dir.exists(live))
  make_run_dir("ss-stale-3", own_owner(pid = dead$pid, start_time = dead$start))
  out <- NULL
  expect_output(out <- ss_doctor())
  row <- out[out$check == "run directories", ]
  expect_equal(row$status, "ok")
  expect_match(row$message, "1 in use, 1 removed")
  expect_false(dir.exists(file.path(runs_root(), "ss-stale-3")))
  # the bridge was asked to sweep the same root
  req <- last_request(dir, "cache-clear")
  expect_equal(req$args$cache_root, tools::R_user_dir("spacescans", "cache"))
})

test_that("loading the package sweeps the run root and says so", {
  skip_on_cran()
  installed <- find.package("spacescans", lib.loc = .libPaths(), quiet = TRUE)
  skip_if(!length(installed), "the package is not installed (load_all); R CMD check runs this")
  root <- withr::local_tempdir()
  withr::local_envvar(R_USER_CACHE_DIR = root)
  dead <- dead_process()
  stale <- make_run_dir("ss-stale", own_owner(pid = dead$pid, start_time = dead$start))
  live <- make_run_dir("ss-live", own_owner())
  script <- withr::local_tempfile(lines = c(
    "withCallingHandlers(loadNamespace('spacescans'),",
    "  packageStartupMessage = function(m) { cat('MSG:', conditionMessage(m)); invokeRestart('muffleMessage') })"
  ), fileext = ".R")
  # a fresh R session (processx, as the package itself uses) with this library path
  out <- processx::run(file.path(R.home("bin"), "Rscript"), c("--vanilla", script), error_on_status = FALSE,
                       env = c("current", R_USER_CACHE_DIR = root,
                               R_LIBS = paste(.libPaths(), collapse = .Platform$path.sep)))
  expect_equal(out$status, 0L, info = paste(out$stdout, out$stderr))
  expect_match(out$stdout, "removed 1 run directory left by processes that no longer exist")
  expect_false(dir.exists(stale))
  expect_true(dir.exists(live))
})

test_that("ss_link() runs in a directory under the run root that is removed afterwards", {
  root <- withr::local_tempdir()
  withr::local_envvar(R_USER_CACHE_DIR = root)
  dir <- local_fake_bridge()
  ss_link(fake_cohort(), "ndi", data_dir = tempdir(), progress = FALSE, cache = FALSE)
  req <- last_request(dir, "link")
  expect_equal(dirname(req$args$run_dir), normalizePath(runs_root()))
  expect_false(dir.exists(req$args$run_dir))
  res <- ss_link(fake_cohort(), "ndi", data_dir = tempdir(), progress = FALSE, cache = FALSE, keep = TRUE)
  rd <- attr(res, "spacescans")$run_dir
  on.exit(unlink(rd, recursive = TRUE), add = TRUE)
  owner <- jsonlite::read_json(file.path(rd, "owner.json"), simplifyVector = TRUE)
  expect_equal(owner$pid, Sys.getpid())
  expect_false(any(grepl("-86.0|2015-0", readLines(file.path(rd, "owner.json")))))
})
