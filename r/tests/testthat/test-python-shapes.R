# The R readers against values captured from the real Python functions
# (fixtures/python/*.json, written by spacescans.data, .orchestrate and
# .custom at this commit), so a change of shape on either side shows up here
# without Python.

py_fixture <- function(name) {
  jsonlite::read_json(test_path("fixtures", "python", paste0(name, ".json")), simplifyVector = FALSE)
}

test_that("a real data.status() report becomes the ss_data_status table", {
  st <- spacescans:::.ss_status_df(py_fixture("data_status"))
  expect_equal(st$variable, c("ndi", "walkability", "temis"))
  expect_true(all(st$status == "action_required"))
  expect_false(any(st$usable))
  expect_equal(st$datasets[1], "bg_boundaries, ndi_panel")
  expect_match(st[["next"]][1], '^ss_fetch\\("ndi"\\); then manual step')
  ds <- attr(st, "datasets")
  expect_true(all(c("bg_boundaries", "ndi_panel") %in% ds$dataset))
  expect_equal(ds[["next"]][ds$dataset == "bg_boundaries"], 'ss_fetch(datasets = "bg_boundaries")')
  expect_false(attr(st, "ready"))
  expect_equal(attr(st, "data_release"), "2026.07")
  expect_output(print(st), "action_required")
})

test_that("a real data.plan() prints its items and manual steps", {
  plan <- structure(py_fixture("fetch_plan"), class = c("ss_fetch_plan", "list"))
  out <- capture.output(print(plan))
  expect_true(any(grepl("bg_boundaries_v1.tar.gz", out, fixed = TRUE)))
  expect_true(any(grepl("Needs a manual step", out, fixed = TRUE)))
  expect_true(any(grepl("ndi_panel", out, fixed = TRUE)))
  expect_length(spacescans:::.ss_plan_terms(plan), 0L)
})

test_that("a real compute_coverage() value becomes one row per variable", {
  cov <- spacescans:::.ss_coverage_df(py_fixture("coverage"))
  expect_equal(cov$variable, c("ndi", "temis"))
  expect_type(cov$coverage_pct, "double")
  expect_equal(cov$pct, cov$coverage_pct)
  expect_equal(cov$patients_covered, c(2L, 2L))
})

test_that("a real define_polygon() definition is keyed by its variable_key for link", {
  def <- structure(py_fixture("custom_polygon"), class = c("ss_custom", "list"))
  m <- spacescans:::.ss_custom_map(def)
  expect_equal(names(m), def$variable_key)
  expect_match(names(m), "^custom_[0-9a-f]{8}$")
  expect_equal(spacescans:::.ss_link_variables("ndi", m), c("ndi", def$variable_key))
  json <- jsonlite::fromJSON(spacescans:::.ss_to_json(list(custom = m)), simplifyVector = FALSE)
  expect_equal(json$custom[[1]]$value_cols, list("score"))
  expect_output(print(def), "Fixture")
})

test_that("real data.terms() output prints", {
  t <- structure(py_fixture("terms_faqsd"), class = c("ss_terms", "list"))
  expect_output(print(t), "faqsd")
})

test_that("shell next steps become R calls", {
  f <- spacescans:::.ss_r_command
  expect_equal(f("spacescans data fetch ndi walkability"), 'ss_fetch(c("ndi", "walkability"))')
  expect_equal(f("spacescans data fetch ndi; then manual step"), 'ss_fetch("ndi"); then manual step')
})
