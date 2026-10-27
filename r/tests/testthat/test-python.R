test_that("no configured interpreter is a spacescans_error_python", {
  local_clean_state()
  withr::local_envvar(SPACESCANS_PYTHON = NA)
  withr::local_options(spacescans.envdir = file.path(tempdir(), "no-such-env"))
  expect_error(ss_python(), class = "spacescans_error_python")
  expect_error(ss_python(), "ss_install")
})

test_that("SPACESCANS_PYTHON must exist", {
  local_clean_state()
  withr::local_envvar(SPACESCANS_PYTHON = file.path(tempdir(), "nope", "python"))
  expect_error(ss_python(), class = "spacescans_error_python")
})

test_that("ss_use_python() wins over SPACESCANS_PYTHON and is per session", {
  local_clean_state()
  d <- withr::local_tempdir()
  fake <- file.path(d, "python")
  file.create(fake)
  # The variable names a second file here, not /bin/sh: the interpreter's directory is
  # canonicalised, and on a Linux with a merged /usr, /bin/sh comes back as /usr/bin/sh.
  other <- file.path(d, "other-python")
  file.create(other)
  withr::local_envvar(SPACESCANS_PYTHON = other)
  ss_use_python(fake, check = FALSE)
  expect_equal(ss_python()$source, "ss_use_python()")
  expect_equal(ss_python()$path, file.path(normalizePath(d), "python"))
  reset_spacescans_state()
  expect_equal(ss_python(), list(path = file.path(normalizePath(d), "other-python"), source = "SPACESCANS_PYTHON"))
})

test_that("a virtual environment directory resolves to its bin/python", {
  local_clean_state()
  d <- withr::local_tempdir()
  dir.create(file.path(d, "bin"))
  file.create(file.path(d, "bin", "python"))
  withr::local_envvar(SPACESCANS_PYTHON = d)
  expect_equal(ss_python()$path, file.path(normalizePath(file.path(d, "bin")), "python"))
})

test_that("the interpreter symlink is not resolved (that would lose the venv)", {
  skip_if_windows()
  d <- withr::local_tempdir()
  target <- file.path(d, "real-python")
  file.create(target)
  link <- file.path(d, "python")
  file.symlink(target, link)
  expect_equal(spacescans:::.ss_python_exe(link), file.path(normalizePath(d), "python"))
})

test_that("ss_install() environment is found through the envdir option", {
  skip_if_windows()     # .ss_absolute() knows POSIX paths only
  local_clean_state()
  d <- withr::local_tempdir()
  dir.create(file.path(d, "bin"))
  file.create(file.path(d, "bin", "python"))
  withr::local_envvar(SPACESCANS_PYTHON = NA)
  withr::local_options(spacescans.envdir = d)
  expect_equal(ss_python(), list(path = file.path(normalizePath(d), "bin", "python"), source = "ss_install()"))
})

test_that("relative envdir options become absolute paths", {
  withr::local_options(spacescans.envdir = "rel-env")
  expect_equal(spacescans:::.ss_envdir(), file.path(normalizePath(getwd()), "rel-env"))
})

test_that("the default environment lives under R_user_dir and is named r-spacescans", {
  withr::local_options(spacescans.envdir = NULL)
  withr::local_envvar(SPACESCANS_ENVDIR = NA, R_USER_DATA_DIR = file.path(tempdir(), "udata"))
  expect_equal(spacescans:::.ss_envdir(), file.path(tempdir(), "udata", "R", "spacescans", "r-spacescans"))
})

test_that("cloud-synced folders are recognised", {
  home <- normalizePath("~")
  expect_true(spacescans:::.ss_is_cloud_synced(file.path(home, "Library", "CloudStorage", "OneDrive-IU", "x")))
  expect_true(spacescans:::.ss_is_cloud_synced(file.path(home, "Library", "Mobile Documents", "com~apple~CloudDocs")))
  expect_true(spacescans:::.ss_is_cloud_synced("/Users/someone/OneDrive - Indiana University/data"))
  expect_true(spacescans:::.ss_is_cloud_synced("/Users/someone/Dropbox/data"))
  expect_false(spacescans:::.ss_is_cloud_synced(tempdir()))
  expect_false(spacescans:::.ss_is_cloud_synced("/Users/someone/Boxes/data"))
})

test_that("the child environment drops Python and GDAL/PROJ redirections", {
  withr::local_envvar(PYTHONPATH = "/x", PROJ_LIB = "/y", GDAL_DATA = "/z", SPACESCANS_DATA_DIR = "/d")
  env <- spacescans:::.ss_child_env()
  expect_false(any(c("PYTHONPATH", "PROJ_LIB", "GDAL_DATA", "DYLD_FALLBACK_LIBRARY_PATH") %in% names(env)))
  expect_equal(env[["SPACESCANS_DATA_DIR"]], "/d")
})
