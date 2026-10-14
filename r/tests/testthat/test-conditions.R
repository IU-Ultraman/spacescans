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

test_that("R hints name the R call that fixes the problem", {
  h <- spacescans:::.ss_r_hint
  expect_equal(h("data_missing", list(variables = c("ndi", "vnl")), NULL),
               'Run ss_fetch(c("ndi", "vnl")) to download the missing data.')
  expect_equal(h("data_missing", list(datasets = "bg_boundaries"), NULL),
               'Run ss_fetch(datasets = "bg_boundaries") to download the missing data.')
  expect_match(h("data_corrupt", list(dataset = "vnl"), NULL), 'ss_data_verify(datasets = "vnl", deep = TRUE)', fixed = TRUE)
  expect_match(h("terms_required", list(terms = "epa-faqsd", dataset = "faqsd"), NULL),
               'ss_fetch(datasets = "faqsd", accept_terms = "epa-faqsd")', fixed = TRUE)
  expect_match(h("step_failed", list(step = "c3_bg", rc = 137L), NULL), "ran out of memory")
  expect_false(grepl("memory", h("step_failed", list(step = "c3_bg", rc = 1L), NULL)))
  # the bridge's measured memory need (gate M) is quoted after R's own advice
  oom <- h("step_failed", list(step = "c4_temis", rc = -9L, oom = TRUE,
                               memory_note = "measured: temis exceeded a 24 GiB container on 100,000 episodes"), NULL)
  expect_match(oom, "smaller cohort. Measured: temis exceeded a 24 GiB container on 100,000 episodes.", fixed = TRUE)
  expect_false(grepl("Measured", h("step_failed", list(step = "c4_temis", rc = -9L, oom = TRUE, memory_note = NULL), NULL)))
  expect_false(grepl("Measured", h("step_failed", list(step = "c4_temis", rc = 2L, memory_note = "measured: x"), NULL)))
  expect_match(h("disk_space", list(required_bytes = 2e9, free_bytes = 1e9), NULL), "2.0 GB, 1.0 GB free")
  expect_match(h("network", list(), NULL), "resume")
  expect_equal(h("missing_extra", list(extra = c("geo", "hdf4")), NULL),
               'ss_install(extras = c("geo", "hdf4")) adds the missing Python extra.')
})

test_that("cohort, network, locked and step kinds get their design classes", {
  for (k in c("cohort", "network", "locked")) {
    expect_s3_class(tryCatch(spacescans:::.ss_abort(k, "x"), error = identity), paste0("spacescans_error_", k))
  }
  e <- tryCatch(spacescans:::.ss_abort("step_failed", "x"), error = identity)
  expect_s3_class(e, "spacescans_error_step")
  expect_s3_class(e, "spacescans_error")
})
