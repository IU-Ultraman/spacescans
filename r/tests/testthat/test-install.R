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

test_that("the lock is filtered to the base install plus the requested extras", {
  d <- withr::local_tempdir()
  lock <- file.path(d, "macos-arm64-py312.txt")
  writeLines(c(
    "numpy==2.5.4 \\", "    --hash=sha256:aa \\", "    --hash=sha256:bb",
    "PyYAML==6.0.3 \\", "    --hash=sha256:cc",
    "rasterio==1.5.1 \\", "    --hash=sha256:dd",
    "pyhdf==0.11.6 \\", "    --hash=sha256:ee",
    "typing_extensions==4.16.0 \\", "    --hash=sha256:ff"
  ), lock)
  jsonlite::write_json(list(base = c("numpy", "pyyaml", "typing-extensions"), geo = "rasterio", hdf4 = "pyhdf"),
                       file.path(d, "macos-arm64-py312.groups.json"))
  out <- readLines(spacescans:::.ss_lock_requirements(lock, "geo", file.path(d, "req.txt")))
  expect_equal(out, c("numpy==2.5.4 \\", "    --hash=sha256:aa \\", "    --hash=sha256:bb",
                      "PyYAML==6.0.3 \\", "    --hash=sha256:cc", "rasterio==1.5.1 \\", "    --hash=sha256:dd",
                      "typing_extensions==4.16.0 \\", "    --hash=sha256:ff"))
  all <- readLines(spacescans:::.ss_lock_requirements(lock, "all", file.path(d, "req2.txt")))
  expect_true(any(startsWith(all, "pyhdf==")))
  expect_error(spacescans:::.ss_lock_requirements(lock, "nc", file.path(d, "req3.txt")),
               class = "spacescans_error_install")
})
