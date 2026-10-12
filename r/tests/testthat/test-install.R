# ss_install() builds a real environment: slow and needs uv plus a wheel, so it
# runs only when SPACESCANS_TEST_INSTALL=true and SPACESCANS_TEST_WHEEL is set.
test_that("ss_install() builds a working environment from a wheel", {
  skip_if_not(identical(Sys.getenv("SPACESCANS_TEST_INSTALL"), "true"), "SPACESCANS_TEST_INSTALL is not true")
  wheel <- Sys.getenv("SPACESCANS_TEST_WHEEL", "")
  skip_if_not(nzchar(wheel) && file.exists(wheel), "SPACESCANS_TEST_WHEEL is not set")
  local_clean_state()
  envdir <- file.path(withr::local_tempdir(), "r-spacescans")
  caps <- ss_install(extras = character(), envdir = envdir, wheel = wheel, quiet = TRUE)
  expect_equal(caps$spacescans$version, spacescans:::.ss_expected()$version)
  expect_false(isTRUE(caps$extras$geo$available))
  info <- jsonlite::read_json(file.path(envdir, "env.json"))
  expect_equal(info$wheel, basename(wheel))
  withr::local_envvar(SPACESCANS_PYTHON = NA)
  withr::local_options(spacescans.envdir = envdir)
  expect_equal(ss_python()$source, "ss_install()")
  expect_equal(ss_version()$python_package, caps$spacescans$version)
})

test_that("ss_install() refuses unknown extras and cloud-synced folders", {
  expect_error(ss_install(extras = "gpu"), class = "spacescans_error_config")
  home <- normalizePath("~")
  expect_error(ss_install(envdir = file.path(home, "Library", "CloudStorage", "OneDrive-X", "env")),
               class = "spacescans_error_install")
})

test_that("uv is looked up in the documented places", {
  withr::local_envvar(SPACESCANS_UV = NA, PATH = "/nonexistent")
  d <- withr::local_tempdir()
  fake <- file.path(d, "uv")
  file.create(fake)
  expect_equal(spacescans:::.ss_find_uv(fake), normalizePath(fake))
  withr::local_envvar(SPACESCANS_UV = fake)
  expect_equal(spacescans:::.ss_find_uv(), normalizePath(fake))
})
