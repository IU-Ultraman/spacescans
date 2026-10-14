# Every run directory the package chooses itself goes under
# tools::R_user_dir("spacescans", "cache")/runs, and the load-time sweep reads
# that root. Point it at a temporary directory for the whole suite, so the
# tests never touch (or clean) the developer's real user cache.
local({
  root <- file.path(tempdir(), "spacescans-test-user-cache")
  dir.create(root, showWarnings = FALSE, recursive = TRUE)
  withr::local_envvar(R_USER_CACHE_DIR = root, .local_envir = testthat::teardown_env())
})
