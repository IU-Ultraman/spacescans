test_that("every error kind gets spacescans_error and its own class", {
  e <- tryCatch(spacescans:::.ss_abort("config", "bad"), error = identity)
  expect_s3_class(e, "spacescans_error_config")
  expect_s3_class(e, "spacescans_error")
  expect_s3_class(e, "error")
  expect_equal(e$kind, "config")
})

test_that("design class names are used for renamed kinds", {
  expect_equal(spacescans:::.ss_error_class("terms_required")[1], "spacescans_error_terms")
  expect_equal(spacescans:::.ss_error_class("disk_space")[1], "spacescans_error_disk")
  expect_equal(spacescans:::.ss_error_class("step_failed")[1], "spacescans_error_step")
  expect_equal(spacescans:::.ss_error_class("brand_new_kind")[1], "spacescans_error_brand_new_kind")
})

test_that("data_* kinds are also spacescans_error_data", {
  cls <- spacescans:::.ss_error_class("data_missing")
  expect_equal(cls[1:2], c("spacescans_error_data_missing", "spacescans_error_data"))
})

test_that("hints are appended to the message and fields are kept", {
  e <- tryCatch(spacescans:::.ss_abort("data", "missing file", hint = "check it", details = list(path = "/x")),
                error = identity)
  expect_match(conditionMessage(e), "missing file\nHint: check it", fixed = TRUE)
  expect_equal(e$details$path, "/x")
})

test_that("R hints are built from bridge details", {
  expect_equal(spacescans:::.ss_r_hint("missing_extra", list(extra = "geo"), NULL),
               'ss_install(extras = "geo") adds the missing Python extra.')
  expect_match(spacescans:::.ss_r_hint("data", list(path = "/d/x.parquet"), NULL), "/d/x.parquet")
  expect_equal(spacescans:::.ss_r_hint("runtime", list(), "py hint"), "py hint")
})

test_that("phase B functions raise not_implemented", {
  for (f in list(ss_link, ss_catalog, ss_data_status, ss_fetch)) {
    expect_error(f(), class = "spacescans_error_not_implemented")
  }
  expect_error(ss_link(data.frame(), "ndi"), "available in phase B")
})
