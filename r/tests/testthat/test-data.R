test_that("ss_data_dir() reads and sets the data directory, with no implicit default", {
  skip_if_windows()     # .ss_absolute() knows POSIX paths only
  withr::local_options(spacescans.data_dir = NULL)
  withr::local_envvar(SPACESCANS_DATA_DIR = NA)
  expect_error(ss_data_dir(), class = "spacescans_error_config")
  withr::local_envvar(SPACESCANS_DATA_DIR = tempdir())
  expect_equal(ss_data_dir(), normalizePath(tempdir()))
  d <- file.path(withr::local_tempdir(), "not-yet")
  expect_invisible(ss_data_dir(d))
  expect_equal(ss_data_dir(), file.path(normalizePath(dirname(d)), "not-yet"))
  expect_error(ss_data_dir(c("a", "b")), class = "spacescans_error_config")
})

test_that("ss_data_status() returns a table with R next steps", {
  dir <- local_fake_bridge(list(`data-status` = list(missing = list("ndi"))))
  st <- ss_data_status(c("ndi", "walkability"), data_dir = tempdir())
  expect_s3_class(st, "ss_data_status")
  expect_equal(st$variable, c("ndi", "walkability"))
  expect_equal(st$status, c("missing", "ready"))
  expect_equal(st$usable, c(FALSE, TRUE))
  expect_equal(st[["next"]][1], 'ss_fetch(datasets = "ndi_panel")')
  expect_false(attr(st, "ready"))
  ds <- attr(st, "datasets")
  expect_true("ndi_panel" %in% ds$dataset)
  expect_output(print(st), "Some data is not ready")
  req <- last_request(dir, "data-status")
  expect_equal(unlist(req$args$variables), c("ndi", "walkability"))
  expect_false(req$args$deep)
})

test_that("ss_fetch(dry_run = TRUE) shows the plan without downloading", {
  dir <- local_fake_bridge()
  expect_output(plan <- ss_fetch("ndi", data_dir = tempdir(), dry_run = TRUE), "ndi_panel.tar.gz")
  expect_s3_class(plan, "ss_fetch_plan")
  expect_length(fake_requests(dir, "data-fetch"), 0L)
})

test_that("ss_fetch() downloads, and refuses unaccepted terms outside interactive sessions", {
  terms <- list(list(id = "epa-faqsd", dataset = "faqsd", title = "EPA FAQSD terms", url = "https://x"))
  dir <- local_fake_bridge(list(`data-plan` = list(terms_required = terms)))
  e <- tryCatch(ss_fetch("faqsd", data_dir = tempdir(), progress = FALSE), error = identity)
  expect_s3_class(e, "spacescans_error_terms")
  expect_match(e$hint, 'ss_fetch(datasets = "faqsd", accept_terms = "epa-faqsd")', fixed = TRUE)
  rep <- ss_fetch("faqsd", data_dir = tempdir(), accept_terms = "epa-faqsd", progress = FALSE)
  expect_s3_class(rep, "ss_fetch_report")
  expect_equal(rep$items[[1]]$outcome, "downloaded")
  req <- last_request(dir, "data-fetch")
  expect_equal(unlist(req$args$accept_terms), "epa-faqsd")
  expect_false(req$args$keep_archives)
  expect_output(print(rep), "downloaded")
})

test_that("failed downloads warn and manual datasets are listed", {
  dir <- local_fake_bridge(list(`data-fetch` = list(outcome = "failed"),
                                `data-plan` = list(manual = list(list(dataset = "vnl", reason = "provider download")))))
  expect_warning(rep <- ss_fetch("vnl", data_dir = tempdir(), progress = FALSE), "1 download\\(s\\) failed")
  expect_output(print(ss_fetch("vnl", data_dir = tempdir(), dry_run = TRUE)), "vnl: provider download")
})

test_that("verify, import and terms call their commands", {
  dir <- local_fake_bridge()
  v <- ss_data_verify(datasets = "ndi_panel", data_dir = tempdir(), deep = TRUE)
  expect_true(last_request(dir, "data-verify")$args$deep)
  expect_equal(v$datasets[[1]]$status, "ready")
  f <- withr::local_tempfile(lines = "x")
  imp <- ss_data_import("vnl", f, data_dir = tempdir())
  expect_equal(imp$imported, 1L)
  expect_equal(unlist(last_request(dir, "data-import")$args$paths), normalizePath(f))
  expect_error(ss_data_import("vnl", "/no/such/file", data_dir = tempdir()), class = "spacescans_error_config")
  t <- ss_data_terms("faqsd")
  expect_s3_class(t, "ss_terms")
  expect_output(print(t), "epa-faqsd")
})

test_that("bridge next commands are translated to R calls", {
  expect_equal(spacescans:::.ss_r_command("spacescans data fetch --dataset bg_boundaries"),
               'ss_fetch(datasets = "bg_boundaries")')
  expect_equal(spacescans:::.ss_r_command("optional: spacescans data verify --dataset vnl"),
               'optional: ss_data_verify(datasets = "vnl")')
  expect_equal(spacescans:::.ss_r_command(""), "")
  # --accept-terms becomes the accept_terms argument (review rux F3)
  expect_equal(spacescans:::.ss_r_command("spacescans data fetch --dataset faqsd --accept-terms epa-faqsd"),
               'ss_fetch(datasets = "faqsd", accept_terms = "epa-faqsd")')
  expect_equal(spacescans:::.ss_r_command("spacescans data fetch faqsd temis --accept-terms a-1 --accept-terms b"),
               'ss_fetch(c("faqsd", "temis"), accept_terms = c("a-1", "b"))')
})

test_that("ss_custom_polygon() gives every data frame a file of its own (review rux F2)", {
  dir <- local_fake_bridge()
  a <- data.frame(FIPS = c("18097320100", "18097320200"), greenness = c(0.2, 0.4))
  b <- transform(a, greenness = 999)
  da <- ss_custom_polygon(a, boundary = "Tract", key_col = "FIPS", value_cols = "greenness")
  db <- ss_custom_polygon(b, boundary = "Tract", key_col = "FIPS", value_cols = "greenness")
  expect_false(identical(da$values_path, db$values_path))
  expect_equal(utils::read.csv(da$values_path)$greenness, c(0.2, 0.4))
  expect_equal(utils::read.csv(db$values_path)$greenness, c(999, 999))
  dn <- ss_custom_polygon(a, boundary = "Tract", key_col = "FIPS", value_cols = "greenness", name = "SVI 2018")
  dm <- ss_custom_polygon(b, boundary = "Tract", key_col = "FIPS", value_cols = "greenness", name = "SVI/2018")
  expect_false(identical(dn$values_path, dm$values_path))
  e <- tryCatch(ss_custom_polygon(a, boundary = "Block", key_col = "FIPS", value_cols = "greenness"),
                error = identity)
  expect_s3_class(e, "spacescans_error_config")
})
