test_that("ss_version() reports matching versions and protocol 1", {
  skip_if_no_python()
  local_clean_state()
  v <- ss_version()
  expect_s3_class(v, "ss_version")
  expect_equal(v$python_package, spacescans:::.ss_expected()$version)
  expect_equal(v$protocol, 1L)
  expect_match(v$code_fp, "^sha256:[0-9a-f]{64}$")
  expect_equal(v$source, "SPACESCANS_PYTHON")
  expect_output(print(v), "bridge protocol")
})

test_that("the handshake is cached per session and interpreter", {
  skip_if_no_python()
  local_clean_state()
  a <- spacescans:::.ss_hello()
  t1 <- spacescans:::.ss_state$hello$time
  b <- spacescans:::.ss_hello()
  expect_identical(spacescans:::.ss_state$hello$time, t1)
  expect_equal(a$spacescans$code_fp, b$spacescans$code_fp)
})

test_that("a Python package of another version is refused (strict) or warned about", {
  skip_if_no_python()
  local_clean_state()
  local_mocked_bindings(.ss_expected = function() list(version = "9.9.9", protocol = 1L))
  expect_error(ss_version(), class = "spacescans_error_protocol")
  expect_error(ss_version(), "needs spacescans 9.9.9")
  withr::local_options(spacescans.version_check = "warn")
  expect_error(ss_version(), class = "spacescans_error_protocol")   # different minor: still refused
  withr::local_options(spacescans.version_check = "off")
  expect_s3_class(ss_version(), "ss_version")
})

test_that("same minor version passes with a warning under version_check = 'warn'", {
  skip_if_no_python()
  local_clean_state()
  real <- spacescans:::.ss_expected()$version
  other <- paste0(sub("\\.[0-9]+$", "", real), ".999")
  local_mocked_bindings(.ss_expected = function() list(version = other, protocol = 1L))
  withr::local_options(spacescans.version_check = "warn")
  expect_warning(ss_version(), "needs spacescans")
})

test_that("a bridge protocol outside the accepted range is a protocol error", {
  skip_if_no_python()
  local_clean_state()
  local_mocked_bindings(.ss_expected = function() list(version = "0.2.7", protocol = 2L))
  expect_error(ss_version(), class = "spacescans_error_protocol")
})

test_that("events stream while the bridge runs and stray output stays out of them", {
  skip_if_no_python()
  local_clean_state()
  seen <- list()
  val <- spacescans:::.ss_bridge("ping", list(sleep_s = 0.6, echo = list(a = 1)),
                                 on_event = function(ev) seen[[length(seen) + 1L]] <<- ev)
  kinds <- vapply(seen, `[[`, "", "ev")
  expect_equal(kinds[1], "start")
  expect_equal(kinds[length(kinds)], "done")
  expect_true("progress" %in% kinds)
  expect_true(val$pong)
  expect_equal(val$echo$a, 1L)
})

test_that("error kinds from Python become the matching R condition classes", {
  skip_if_no_python()
  local_clean_state()
  e <- tryCatch(spacescans:::.ss_bridge("ping", list(fail_kind = "config")), error = identity)
  expect_s3_class(e, "spacescans_error_config")
  expect_equal(e$py_type, "spacescans.bridge.errors.BridgeError")
  expect_match(e$py_traceback, "Traceback")
  expect_equal(e$exit_status, 1L)
  expect_error(spacescans:::.ss_bridge("ping", list(fail_kind = "data_missing")), class = "spacescans_error_data")
  expect_error(spacescans:::.ss_bridge("ping", list(fail_kind = "terms_required")), class = "spacescans_error_terms")
})

test_that("a killed bridge is reported as crashed, with an out-of-memory hint for SIGKILL", {
  skip_if_no_python()
  local_clean_state()
  e <- tryCatch(spacescans:::.ss_bridge("ping", list(signal = 9L)), error = identity)
  expect_s3_class(e, "spacescans_error_crashed")
  expect_equal(e$exit_status, -9L)
  expect_match(conditionMessage(e), "SIGKILL")
  expect_match(conditionMessage(e), "out of memory")
  e <- tryCatch(spacescans:::.ss_bridge("ping", list(exit_code = 7L)), error = identity)
  expect_s3_class(e, "spacescans_error_crashed")
  expect_equal(e$exit_status, 7L)
})

test_that("Ctrl-C stops the bridge quickly and leaves no process behind", {
  skip_if_no_python()
  skip_on_os("windows")
  local_clean_state()
  spacescans:::.ss_hello()
  pid <- NULL
  killer <- processx::process$new("/bin/sh", c("-c", sprintf("sleep 1.5; kill -INT %d", Sys.getpid())))
  t0 <- Sys.time()
  e <- tryCatch(
    spacescans:::.ss_bridge("ping", list(sleep_s = 60), on_event = function(ev) {
      if (identical(ev$ev, "start")) pid <<- ev$pid
    }),
    error = identity)
  elapsed <- as.numeric(difftime(Sys.time(), t0, units = "secs"))
  killer$wait(2000)
  expect_s3_class(e, "spacescans_error_interrupted")
  expect_equal(e$exit_status, 130L)
  expect_lt(elapsed, 10)
  expect_false(is.null(pid))
  expect_false(process_alive(pid))
})

test_that("run directories are private and removed after success", {
  skip_if_no_python()
  local_clean_state()
  before <- list.files(tempdir(), pattern = "^ss-")
  spacescans:::.ss_bridge("ping")
  expect_equal(list.files(tempdir(), pattern = "^ss-"), before)
  d <- spacescans:::.ss_new_run_dir()
  on.exit(unlink(d, recursive = TRUE))
  expect_equal(as.character(file.info(d)$mode), "700")
})

test_that("ss_doctor() reports the interpreter and the bridge", {
  skip_if_no_python()
  local_clean_state()
  out <- NULL
  expect_output(out <- ss_doctor())
  expect_s3_class(out, "ss_doctor")
  expect_equal(out$status[out$check == "python"], "ok")
  expect_equal(out$status[out$check == "bridge"], "ok")
})
