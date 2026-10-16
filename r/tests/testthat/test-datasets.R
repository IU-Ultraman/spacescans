# Registered datasets (phase D): ss_register_dataset(), ss_datasets(),
# ss_dataset(), ss_update_dataset(), ss_remove_dataset(), and registered
# datasets in ss_link() and ss_coverage(), against the fake bridge's
# file-backed registry (shaped like track B's datasets-* commands).

csv_file <- function(dir = withr::local_tempdir(.local_envir = parent.frame())) {
  path <- file.path(dir, "svi_tract.csv")
  writeLines(c("GEOID,RPL_THEMES,score", "18097000100,0.5,1", "18097000200,0.25,2"), path)
  path
}

any_file <- function(ext, dir = withr::local_tempdir(.local_envir = parent.frame())) {
  path <- file.path(dir, paste0("data", ext))
  writeBin(as.raw(1:10), path)
  path
}

test_that("ss_register_dataset() sends the files, metadata and variables and returns the record", {
  dir <- local_fake_bridge()
  csv <- csv_file()
  rec <- ss_register_dataset(csv, name = "SVI 2020", kind = "polygon_table", temporal = "static",
                             key = list(boundary = "tract", column = "GEOID"), variables = "RPL_THEMES",
                             source = "CDC/ATSDR SVI", version = "2020",
                             description = "Social vulnerability, 2020.")
  expect_s3_class(rec, "ss_dataset")
  expect_equal(rec$name, "SVI 2020")
  expect_match(rec$id, "^[0-9a-f]{32}$")
  expect_equal(rec$variable_key, paste0("custom_", substr(rec$id, 1, 8)))
  expect_equal(rec$kind, "polygon_table")
  expect_equal(rec$variables$name, "RPL_THEMES")
  expect_true(all(c("name", "unit", "description") %in% names(rec$variables)))
  expect_equal(rec$visibility, "private")
  expect_type(rec$files, "character")
  expect_equal(attr(rec, "root"), dirname(rec$path))
  expect_output(print(rec), 'spacescans dataset "SVI 2020"')
  expect_output(print(rec), "polygon_table, static")
  a <- last_request(dir, "datasets-register")$args
  expect_equal(unlist(a$files), normalizePath(csv))
  expect_null(a$periods)
  expect_equal(a$name, "SVI 2020")
  expect_equal(a$kind, "polygon_table")
  expect_equal(a$temporal, "static")
  expect_equal(a$key, list(boundary = "tract", column = "GEOID"))
  expect_equal(a$variables, list(list(name = "RPL_THEMES")))
  expect_equal(a$metadata, list(description = "Social vulnerability, 2020.", source = "CDC/ATSDR SVI",
                                version = "2020", visibility = "private"))
  expect_null(a$root)                                   # Python resolves the default registry
  expect_false(a$overwrite)
})

test_that("variables can be a data frame with units, descriptions, columns and bands", {
  dir <- local_fake_bridge()
  vars <- data.frame(name = c("svi", "score"), unit = c("percentile", NA),
                     description = c("Overall SVI", "A score"), column = c("RPL_THEMES", NA),
                     stringsAsFactors = FALSE)
  rec <- ss_register_dataset(csv_file(), name = "svi", kind = "polygon_table", temporal = "static",
                             key = "tract", variables = vars)
  sent <- last_request(dir, "datasets-register")$args$variables
  expect_equal(sent[[1]], list(name = "svi", unit = "percentile", description = "Overall SVI",
                               column = "RPL_THEMES"))
  expect_equal(sent[[2]], list(name = "score", description = "A score"))   # NAs are not sent
  expect_equal(rec$variables$unit, c("percentile", NA))
  expect_output(print(rec), "svi \\(percentile\\), score")
  ss_register_dataset(any_file(".tif"), name = "bands", kind = "raster", temporal = "static",
                      variables = data.frame(name = c("a", "b"), band = c(1, 2)))
  sent <- last_request(dir, "datasets-register")$args$variables
  expect_identical(sent[[2]]$band, 2L)
})

test_that("periods give each raster file its year or start date", {
  dir <- local_fake_bridge()
  d <- withr::local_tempdir()
  tifs <- file.path(d, c("a.tif", "b.tif"))
  for (f in tifs) writeBin(as.raw(1:4), f)
  ss_register_dataset(tifs, name = "pm", kind = "raster", temporal = "biweekly",
                      periods = as.Date(c("2016-01-01", "2016-01-15")))
  expect_equal(last_request(dir, "datasets-register")$args$periods, list("2016-01-01", "2016-01-15"))
  ss_register_dataset(tifs, name = "pm_yearly", kind = "raster", temporal = "yearly", periods = c(2016, 2017))
  expect_equal(last_request(dir, "datasets-register")$args$periods, list(2016L, 2017L))
  expect_error(ss_register_dataset(tifs, name = "x", kind = "raster", temporal = "yearly", periods = 2016),
               "one year or date per file", class = "spacescans_error_config")
  expect_error(ss_register_dataset(tifs, name = "x", kind = "raster", temporal = "yearly",
                                   periods = c(2016.5, 2017)), class = "spacescans_error_config")
})

test_that("a data frame registers as a polygon table through a private temporary CSV", {
  dir <- local_fake_bridge()
  df <- data.frame(GEOID = c("18097000100", "18097000200"), value = c(1, 2))
  rec <- ss_register_dataset(df, name = "from_df", kind = "polygon_table", temporal = "static",
                             key = "tract")
  path <- unlist(last_request(dir, "datasets-register")$args$files)
  expect_length(path, 1L)
  expect_match(path, "spacescans-datasets/from_df-.*[.]csv$")
  expect_equal(as.character(file.info(path)$mode), "600")
  expect_equal(read.csv(path, colClasses = "character")$GEOID, df$GEOID)
  expect_error(ss_register_dataset(df, name = "r", kind = "raster", temporal = "static"),
               "only be registered as a \"polygon_table\"", class = "spacescans_error_config")
})

test_that("arguments are checked in R before the bridge starts", {
  local_clean_state()
  withr::local_envvar(SPACESCANS_PYTHON = "/nonexistent/python")
  csv <- csv_file()
  reg <- function(...) ss_register_dataset(csv, ...)
  expect_error(reg(kind = "polygon_table", temporal = "static"), "`name` is required",
               class = "spacescans_error_config")
  expect_error(reg(name = "x", temporal = "static"), "`kind` is required", class = "spacescans_error_config")
  expect_error(reg(name = "x", kind = "polygon_table"), "`temporal` is required",
               class = "spacescans_error_config")
  expect_error(reg(name = "x", kind = "table", temporal = "static"), class = "spacescans_error_config")
  expect_error(reg(name = "x", kind = "features", temporal = "daily"),
               'A features dataset is "static" or "yearly", not "daily"', class = "spacescans_error_config")
  expect_error(reg(name = "x", kind = "polygon_table", temporal = "biweekly"),
               "polygon_table dataset is", class = "spacescans_error_config")
  expect_error(reg(name = "x", kind = "raster", temporal = "hourly"), class = "spacescans_error_config")
  expect_error(reg(name = "x", kind = "raster", temporal = "static", visibility = "public"),
               class = "spacescans_error_config")
  expect_error(reg(name = "x", kind = "raster", temporal = "static", variables = 3),
               class = "spacescans_error_config")
  expect_error(reg(name = "x", kind = "raster", temporal = "static", variables = data.frame(name = "a", colour = "red")),
               "Unknown column", class = "spacescans_error_config")
  expect_error(reg(name = "x", kind = "raster", temporal = "static", key = c("a", "b")),
               class = "spacescans_error_config")
  expect_error(reg(name = "x", kind = "raster", temporal = "static", source = 1),
               class = "spacescans_error_config")
  expect_error(reg(name = "", kind = "raster", temporal = "static"), class = "spacescans_error_config")
  expect_error(ss_register_dataset(file.path(tempdir(), "no-such-file.tif"), name = "x", kind = "raster",
                                   temporal = "static"),
               "File\\(s\\) not found", class = "spacescans_error_config")
  expect_error(ss_dataset(c("a", "b")), class = "spacescans_error_config")
  expect_error(ss_remove_dataset(NA_character_), class = "spacescans_error_config")
  expect_error(ss_update_dataset("x"), "Nothing to change", class = "spacescans_error_config")
  expect_error(ss_update_dataset("x", visibility = "public"), class = "spacescans_error_config")
  expect_error(ss_update_dataset("x", variables = data.frame(name = "a", band = 1)),
               class = "spacescans_error_config")
})

test_that("ss_datasets() lists, ss_dataset() gets by name, id, key or record, ss_remove_dataset() removes", {
  dir <- local_fake_bridge()
  empty <- ss_datasets()
  expect_equal(nrow(empty), 0L)
  expect_true(all(c("name", "id", "kind", "temporal", "variables", "visibility") %in% names(empty)))
  a <- ss_register_dataset(csv_file(), name = "svi", kind = "polygon_table", temporal = "yearly",
                           key = list(boundary = "tract", column = "GEOID", year_column = "yr"),
                           variables = c("RPL_THEMES", "score"))
  b <- ss_register_dataset(any_file(".tif"), name = "noise_local", kind = "raster", temporal = "static",
                           visibility = "deployment", owner = "study team")
  ls <- ss_datasets()
  expect_equal(ls$name, c("svi", "noise_local"))
  expect_equal(ls$variables, c("RPL_THEMES, score", "noise_local"))
  expect_equal(ls$visibility, c("private", "deployment"))
  expect_equal(ls$owner, c(NA, "study team"))
  expect_equal(ls$key[1], "boundary=tract, column=GEOID, year_column=yr")
  expect_equal(ls$variable_key, c(a$variable_key, b$variable_key))
  expect_false(any(c("files", "details") %in% names(ls)))
  expect_false(is.null(attr(ls, "root")))
  expect_equal(ss_dataset("SVI")$id, a$id)                  # names ignore case
  expect_equal(ss_dataset(b$id)$name, "noise_local")
  expect_equal(ss_dataset(b$variable_key)$name, "noise_local")
  expect_equal(ss_dataset(b)$name, "noise_local")
  expect_equal(last_request(dir, "datasets-get")$args$name, b$id)
  expect_message(gone <- ss_remove_dataset("svi"), 'Removed the registered dataset "svi"')
  expect_equal(gone$id, a$id)
  expect_equal(ss_datasets()$name, "noise_local")
  e <- tryCatch(ss_dataset("svi"), error = identity)
  expect_s3_class(e, "spacescans_error_config")
  expect_match(conditionMessage(e), "no dataset named 'svi'")
  expect_error(ss_remove_dataset("svi"), class = "spacescans_error_config")
})

test_that("ss_update_dataset() changes the name, metadata or variable units, and only those", {
  dir <- local_fake_bridge()
  rec <- ss_register_dataset(csv_file(), name = "svi", kind = "polygon_table", temporal = "static",
                             variables = c("RPL_THEMES", "score"), source = "CDC")
  up <- ss_update_dataset("svi", name = "SVI 2020", visibility = "deployment",
                          variables = data.frame(name = "score", unit = "points"))
  a <- last_request(dir, "datasets-update")$args
  expect_equal(a$name, "svi")
  expect_equal(a$new_name, "SVI 2020")
  expect_equal(a$metadata, list(visibility = "deployment"))     # the source is not touched
  expect_equal(a$variables, list(list(name = "score", unit = "points")))
  expect_equal(up$name, "SVI 2020")
  expect_equal(up$visibility, "deployment")
  expect_equal(up$source, "CDC")
  expect_equal(up$variables$unit, c(NA, "points"))
  expect_equal(up$id, rec$id)
})

test_that("a name is registered once unless overwrite = TRUE", {
  dir <- local_fake_bridge()
  csv <- csv_file()
  first <- ss_register_dataset(csv, name = "svi", kind = "polygon_table", temporal = "static")
  e <- tryCatch(ss_register_dataset(csv, name = "SVI", kind = "polygon_table", temporal = "static"),
                error = identity)
  expect_s3_class(e, "spacescans_error_config")
  expect_match(conditionMessage(e), "already exists")
  second <- ss_register_dataset(csv, name = "svi", kind = "polygon_table", temporal = "static",
                                overwrite = TRUE)
  expect_true(last_request(dir, "datasets-register")$args$overwrite)
  expect_equal(nrow(ss_datasets()), 1L)
  expect_false(identical(first$id, second$id))
})

test_that("root and the spacescans.datasets_dir option choose the registry", {
  dir <- local_fake_bridge()
  root <- file.path(withr::local_tempdir(), "my-registry")
  dir.create(root)
  rec <- ss_register_dataset(csv_file(), name = "svi", kind = "polygon_table", temporal = "static",
                             root = root)
  expect_equal(last_request(dir, "datasets-register")$args$root, normalizePath(root))
  expect_equal(attr(rec, "root"), normalizePath(root))
  expect_equal(nrow(ss_datasets()), 0L)                      # the default registry is another one
  expect_equal(ss_datasets(root = root)$name, "svi")
  withr::local_options(spacescans.datasets_dir = root)
  expect_equal(ss_datasets()$name, "svi")
  expect_equal(last_request(dir, "datasets-list")$args$root, normalizePath(root))
  # ss_link() looks registered datasets up in the same registry
  res <- ss_link(fake_cohort(), "ndi", custom = "svi", data_dir = tempdir(), progress = FALSE, cache = FALSE)
  expect_equal(last_request(dir, "datasets-get")$args$root, normalizePath(root))
  expect_true("value" %in% names(res))
})

test_that("ss_link(custom = ) links registered datasets through their definitions, in the order given", {
  dir <- local_fake_bridge()
  csv <- csv_file()
  svi <- ss_register_dataset(csv, name = "svi", kind = "polygon_table", temporal = "static",
                             variables = c("RPL_THEMES", "score"))
  parks <- ss_register_dataset(any_file(".gpkg"), name = "parks", kind = "features", temporal = "static")
  expect_equal(parks$variables$name, "parks_dist_m")
  res <- ss_link(fake_cohort(), "ndi", custom = "svi", data_dir = tempdir(), progress = FALSE, cache = FALSE)
  req <- last_request(dir, "link")
  expect_null(req$args$datasets)
  expect_equal(unlist(req$args$variables), c("ndi", svi$variable_key))
  expect_equal(names(req$args$custom), svi$variable_key)
  def <- req$args$custom[[1]]
  expect_equal(def$registered, svi$id)                      # the registry's definition, sent unchanged
  expect_equal(unlist(def$value_cols), c("RPL_THEMES", "score"))
  expect_equal(names(res), c("pid", "ndi", "RPL_THEMES", "score"))
  expect_equal(attr(res, "coverage")$variable, c("ndi", svi$variable_key))
  # a record, an inline definition and an id together, no catalog variable: the order is kept
  own <- ss_custom_polygon(csv, boundary = "Tract", key_col = "GEOID", value_cols = "score", name = "Own")
  res <- ss_link(fake_cohort(), character(), custom = list(parks, own, svi$id), data_dir = tempdir(),
                 progress = FALSE, cache = FALSE, output_level = "episode")
  req <- last_request(dir, "link")
  expect_equal(unlist(req$args$variables), c(parks$variable_key, own$variable_key, svi$variable_key))
  expect_equal(names(req$args$custom), c(parks$variable_key, own$variable_key, svi$variable_key))
  expect_true(all(c("parks_dist_m", "score", "RPL_THEMES") %in% names(res)))
  f <- attr(res, "features")
  expect_equal(f$detailed_description[f$feature_name == "parks_dist_m"],
               "Registered static features dataset. Unit: meters.")
  # names in a character vector; the same dataset twice counts once
  ss_link(fake_cohort(), "ndi", custom = c("svi", "parks", "SVI"), data_dir = tempdir(),
          progress = FALSE, cache = FALSE)
  expect_equal(names(last_request(dir, "link")$args$custom), c(svi$variable_key, parks$variable_key))
  # the data preflight only asks about catalog variables
  expect_equal(unlist(last_request(dir, "data-status")$args$variables), "ndi")
})

test_that("an unknown registered dataset stops before the link starts", {
  dir <- local_fake_bridge()
  e <- tryCatch(ss_link(fake_cohort(), "ndi", custom = "nope", data_dir = tempdir(), progress = FALSE,
                        cache = FALSE), error = identity)
  expect_s3_class(e, "spacescans_error_config")
  expect_match(conditionMessage(e), "no dataset named 'nope'")
  expect_length(fake_requests(dir, "link"), 0L)
  expect_error(ss_link(fake_cohort(), "ndi", custom = list(3), data_dir = tempdir()),
               "registered dataset's name or id", class = "spacescans_error_config")
  expect_error(ss_link(fake_cohort(), character(), data_dir = tempdir()), "at least one variable",
               class = "spacescans_error_config")
})

test_that("a cohort problem is reported before any registered dataset is looked up", {
  dir <- local_fake_bridge()
  bad <- fake_cohort()
  bad$latitude[2] <- NA
  expect_error(ss_link(bad, "ndi", custom = "svi", data_dir = tempdir()), class = "spacescans_error_cohort")
  expect_length(fake_requests(dir, "datasets-get"), 0L)
})

test_that("ss_coverage() estimates registered datasets from their definitions", {
  dir <- local_fake_bridge()
  svi <- ss_register_dataset(csv_file(), name = "svi", kind = "polygon_table", temporal = "static")
  cov <- ss_coverage(fake_cohort(), "ndi", custom = "svi")
  expect_equal(cov$variable, c("ndi", svi$variable_key))
  req <- last_request(dir, "coverage")
  expect_equal(names(req$args$custom), svi$variable_key)
  expect_null(req$args$datasets)
})
