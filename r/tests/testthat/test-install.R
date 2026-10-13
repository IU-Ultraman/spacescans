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

test_that("a GitHub install points ss_install() at the same commit's source archive", {
  sha <- strrep("a1", 20)
  src <- spacescans:::.ss_github_source(list(
    RemoteType = "github", RemoteHost = "api.github.com", RemoteUsername = "IU-Ultraman",
    RemoteRepo = "spacescans", RemoteSha = sha, RemoteSubdir = "r"))
  url <- sprintf("https://github.com/IU-Ultraman/spacescans/archive/%s.tar.gz", sha)
  expect_equal(src$url, url)
  expect_equal(src$commit, sha)
  expect_equal(spacescans:::.ss_requirement(src, c("geo", "nc")), paste0("spacescans-pipeline[geo,nc] @ ", url))
  expect_equal(spacescans:::.ss_requirement(src), paste0("spacescans-pipeline @ ", url))
  # older versions of remotes wrote only the Github* fields
  old <- spacescans:::.ss_github_source(list(GithubUsername = "IU-Ultraman", GithubRepo = "spacescans",
                                             GithubSHA1 = sha))
  expect_equal(old$url, url)
})

test_that("installs that did not come from GitHub give no source archive", {
  sha <- strrep("b2", 20)
  gh <- function(...) spacescans:::.ss_github_source(list(...))
  expect_null(spacescans:::.ss_github_source(NA))                  # package not installed
  expect_null(gh(Package = "spacescans"))                         # R CMD INSTALL from a checkout
  expect_null(gh(RemoteType = "local", RemoteSha = sha))
  expect_null(gh(RemoteType = "github", RemoteUsername = "u", RemoteRepo = "r", RemoteSha = "abc123"))
  expect_null(gh(RemoteType = "github", RemoteHost = "github.example.org",
                 RemoteUsername = "u", RemoteRepo = "r", RemoteSha = sha))
})

test_that("without a wheel, ss_install() uses the GitHub source unless offline", {
  gh <- spacescans:::.ss_github_source(list(RemoteType = "github", RemoteUsername = "IU-Ultraman",
                                            RemoteRepo = "spacescans", RemoteSha = strrep("c3", 20)))
  local_mocked_bindings(.ss_pin = function() list())               # no bundled wheel, no pin URL
  expect_identical(spacescans:::.ss_find_wheel(github = gh), gh)
  expect_error(spacescans:::.ss_find_wheel(offline = TRUE, github = gh), class = "spacescans_error_install")
  expect_error(spacescans:::.ss_find_wheel(github = NULL), "install_github", class = "spacescans_error_install")
  d <- withr::local_tempdir()
  whl <- file.path(d, "spacescans_pipeline-0.0.0-py3-none-any.whl")
  file.create(whl)
  expect_equal(spacescans:::.ss_find_wheel(wheel = whl, github = gh)$kind, "wheel")   # a wheel always wins
})

# Builds a real environment from GitHub: needs uv and the network, so it runs
# only when SPACESCANS_TEST_INSTALL=true and SPACESCANS_TEST_GITHUB_SHA names a
# pushed commit whose Python package has this R package's version.
test_that("ss_install() builds a working environment from the GitHub source archive", {
  skip_if_not(identical(Sys.getenv("SPACESCANS_TEST_INSTALL"), "true"), "SPACESCANS_TEST_INSTALL is not true")
  sha <- Sys.getenv("SPACESCANS_TEST_GITHUB_SHA", "")
  skip_if_not(grepl("^[0-9a-f]{40}$", sha), "SPACESCANS_TEST_GITHUB_SHA is not set")
  local_clean_state()
  gh <- spacescans:::.ss_github_source(list(RemoteType = "github", RemoteUsername = "IU-Ultraman",
                                            RemoteRepo = "spacescans", RemoteSha = sha))
  local_mocked_bindings(.ss_pin = function() list(), .ss_github_source = function(...) gh)
  envdir <- file.path(withr::local_tempdir(), "r-spacescans")
  caps <- ss_install(envdir = envdir, quiet = TRUE)
  expect_equal(caps$spacescans$version, spacescans:::.ss_expected()$version)
  expect_true(isTRUE(caps$extras$geo$available))
  info <- jsonlite::read_json(file.path(envdir, "env.json"))
  expect_equal(info$source_commit, sha)
  expect_null(info$wheel)
})
