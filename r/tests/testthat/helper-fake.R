# The fake bridge (fixtures/fake_bridge.R) answers every protocol-1 command
# from canned values, so the R side is tested without Python.

fake_bridge_script <- function() {
  path <- test_path("fixtures", "fake_bridge.R")
  normalizePath(path)
}

# Use the fake bridge for the rest of the calling test. `scenario` is a list
# keyed by command (see fake_bridge.R). Returns the fake's directory; its
# "log" subdirectory receives each request and what the fake saw.
local_fake_bridge <- function(scenario = list(), env = parent.frame()) {
  skip_if_windows()     # the wrapper below is a POSIX shell script
  dir <- withr::local_tempdir(.local_envir = env)
  wrapper <- file.path(dir, "python")
  libs <- paste(.libPaths(), collapse = ":")
  writeLines(c(
    "#!/bin/sh",
    "# The client calls: <python> -I -u -X utf8 -m spacescans.bridge <args>; drop the first six.",
    "shift 6",
    "unset R_TESTS R_PROFILE_USER R_ENVIRON_USER",
    sprintf("R_LIBS='%s'; export R_LIBS", libs),
    sprintf("exec '%s' --vanilla '%s' \"$@\"", file.path(R.home("bin"), "Rscript"), fake_bridge_script())
  ), wrapper)
  Sys.chmod(wrapper, "0755")
  set_fake_scenario(dir, scenario)
  withr::local_envvar(
    SS_FAKE_SCENARIO = file.path(dir, "scenario.json"),
    SS_FAKE_LOG = file.path(dir, "log"),
    SS_FAKE_REGISTRY = file.path(dir, "registry", "registry.json"),
    SS_FAKE_VERSION = spacescans:::.ss_expected()$version,
    SPACESCANS_DATASETS_DIR = NA,
    .local_envir = env
  )
  withr::local_options(spacescans.datasets_dir = NULL, .local_envir = env)
  reset_spacescans_state()
  st <- spacescans:::.ss_state
  st$python <- list(path = wrapper, source = "ss_use_python()")
  withr::defer(reset_spacescans_state(), envir = env)
  invisible(dir)
}

set_fake_scenario <- function(dir, scenario) {
  json <- jsonlite::toJSON(scenario, auto_unbox = TRUE, null = "null", digits = NA)
  writeLines(if (length(scenario)) json else "{}", file.path(dir, "scenario.json"))
}

# The requests the fake received for `command`, oldest first, each with the
# fake's "seen" record as attribute.
fake_requests <- function(dir, command) {
  log <- file.path(dir, "log")
  files <- sort(list.files(log, pattern = sprintf("^[0-9]+-%s[.]json$", command), full.names = TRUE))
  lapply(files, function(f) {
    req <- jsonlite::read_json(f, simplifyVector = FALSE)
    seen <- sub("[.]json$", ".seen.json", f)
    attr(req, "seen") <- if (file.exists(seen)) jsonlite::read_json(seen, simplifyVector = TRUE)
    req
  })
}

last_request <- function(dir, command) {
  reqs <- fake_requests(dir, command)
  if (!length(reqs)) return(NULL)
  reqs[[length(reqs)]]
}

# A small cohort with extra columns of several types and an attribute.
fake_cohort <- function(n = 6) {
  df <- data.frame(
    pid = c("A", "A", "B", "C", "C", "D")[seq_len(n)],
    startDate = as.Date("2015-01-01") + 0:(n - 1) * 30,
    endDate = as.Date("2016-01-01") + 0:(n - 1) * 30,
    longitude = -86 - seq_len(n) / 100,
    latitude = 39 + seq_len(n) / 100,
    age = c(30L, 31L, 45L, 60L, 61L, 22L)[seq_len(n)],
    sex = factor(c("F", "F", "M", "F", "F", "M")[seq_len(n)]),
    stringsAsFactors = FALSE
  )
  attr(df, "study") <- "fake-study"
  df
}
