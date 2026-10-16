# Registered datasets (phase D): ss_register_dataset(), ss_datasets(),
# ss_dataset(), ss_remove_dataset(), and registered names in ss_link() and
# ss_coverage(), against the fake bridge's file-backed registry.

csv_file <- function(dir = withr::local_tempdir(.local_envir = parent.frame())) {
  path <- file.path(dir, "svi_tract.csv")
  writeLines(c("GEOID,RPL_THEMES,score", "18097000100,0.5,1", "18097000200,0.25,2"), path)
  path
}

test_that("ss_register_dataset() sends the files, metadata and variables and returns the record", {
  dir <- local_fake_bridge()
  csv <- csv_file()
  rec <- ss_register_dataset(csv, name = "svi_2020", kind = "polygon_table", temporal = "static",
                             key = "Tract", variables = "RPL_THEMES", source = "CDC/ATSDR SVI",
                             version = "2020", description = "Social vulnerability, 2020.")
  expect_s3_class(rec, "ss_dataset")
  expect_equal(rec$name, "svi_2020")
  expect_match(rec$id, "^[0-9a-f]{32}$")
  expect_equal(rec$kind, "polygon_table")
  expect_equal(rec$variables$name, "RPL_THEMES")
  expect_true(all(c("name", "unit", "description") %in% names(rec$variables)))
  expect_equal(rec$visibility, "private")
  expect_type(rec$files, "character")
  expect_output(print(rec), 'spacescans dataset "svi_2020"')
  expect_output(print(rec), "polygon_table, static")
  req <- last_request(dir, "dataset-register")
  a <- req$args
  expect_equal(unlist(a$paths), normalizePath(csv))
  expect_equal(a$name, "svi_2020")
  expect_equal(a$kind, "polygon_table")
  expect_equal(a$temporal, "static")
  expect_equal(a$key, "Tract")
  expect_equal(a$variables, list(list(name = "RPL_THEMES")))
  expect_equal(a$metadata$visibility, "private")
  expect_equal(a$metadata$source, "CDC/ATSDR SVI")
  expect_equal(a$metadata$version, "2020")
  expect_null(a$metadata$owner)
  expect_null(a$root)                                   # Python resolves the default root
  expect_false(a$overwrite)
})

test_that("variables can be a data frame with units and descriptions", {
  dir <- local_fake_bridge()
  vars <- data.frame(name = c("RPL_THEMES", "score"), unit = c("percentile", NA),
                     description = c("Overall SVI", "A score"), stringsAsFactors = FALSE)
  rec <- ss_register_dataset(csv_file(), name = "svi", kind = "polygon_table", temporal = "static",
                             variables = vars)
  sent <- last_request(dir, "dataset-register")$args$variables
  expect_equal(sent[[1]], list(name = "RPL_THEMES", unit = "percentile", description = "Overall SVI"))
  expect_equal(sent[[2]], list(name = "score", description = "A score"))   # NA unit not sent
  expect_equal(rec$variables$unit, c("percentile", NA))
  expect_output(print(rec), "RPL_THEMES \\(percentile\\), score")
})

test_that("a data frame registers as a polygon table through a private temporary CSV", {
  dir <- local_fake_bridge()
  df <- data.frame(GEOID = c("18097000100", "18097000200"), value = c(1, 2))
  rec <- ss_register_dataset(df, name = "from_df", kind = "polygon_table", temporal = "static")
  path <- unlist(last_request(dir, "dataset-register")$args$paths)
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
  expect_error(reg(name = "x", kind = "raster", temporal = "static", key = c("a", "b")),
               class = "spacescans_error_config")
  expect_error(reg(name = "x", kind = "raster", temporal = "static", options = list(1)),
               class = "spacescans_error_config")
  expect_error(reg(name = "", kind = "raster", temporal = "static"), class = "spacescans_error_config")
  expect_error(ss_register_dataset(file.path(tempdir(), "no-such-file.tif"), name = "x", kind = "raster",
                                   temporal = "static"),
               "File\\(s\\) not found", class = "spacescans_error_config")
  expect_error(ss_dataset(c("a", "b")), class = "spacescans_error_config")
  expect_error(ss_remove_dataset(NA_character_), class = "spacescans_error_config")
})

test_that("ss_datasets() lists, ss_dataset() gets by name, id or record, ss_remove_dataset() removes", {
  dir <- local_fake_bridge()
  empty <- ss_datasets()
  expect_equal(nrow(empty), 0L)
  expect_true(all(c("name", "id", "kind", "temporal", "variables", "visibility") %in% names(empty)))
  a <- ss_register_dataset(csv_file(), name = "svi", kind = "polygon_table", temporal = "yearly",
                           variables = c("RPL_THEMES", "score"))
  tif <- file.path(withr::local_tempdir(), "noise.tif")
  writeBin(as.raw(1:10), tif)
  b <- ss_register_dataset(tif, name = "noise_local", kind = "raster", temporal = "static",
                           visibility = "deployment", owner = "study team")
  ls <- ss_datasets()
  expect_equal(ls$name, c("svi", "noise_local"))
  expect_equal(ls$variables, c("RPL_THEMES, score", "band1"))
  expect_equal(ls$visibility, c("private", "deployment"))
  expect_equal(ls$owner, c(NA, "study team"))
  expect_false("files" %in% names(ls))
  expect_false(is.null(attr(ls, "root")))
  expect_equal(ss_dataset("svi")$id, a$id)
  expect_equal(ss_dataset(b$id)$name, "noise_local")
  expect_equal(ss_dataset(b)$name, "noise_local")
  expect_message(gone <- ss_remove_dataset("svi"), 'Removed the registered dataset "svi"')
  expect_equal(gone$id, a$id)
  expect_equal(ss_datasets()$name, "noise_local")
  e <- tryCatch(ss_dataset("svi"), error = identity)
  expect_s3_class(e, "spacescans_error_config")
  expect_equal(e$details$name_or_id, "svi")
  expect_error(ss_remove_dataset("svi"), class = "spacescans_error_config")
})

test_that("a name is registered once unless overwrite = TRUE", {
  dir <- local_fake_bridge()
  csv <- csv_file()
  first <- ss_register_dataset(csv, name = "svi", kind = "polygon_table", temporal = "static")
  e <- tryCatch(ss_register_dataset(csv, name = "svi", kind = "polygon_table", temporal = "static"),
                error = identity)
  expect_s3_class(e, "spacescans_error_config")
  expect_match(conditionMessage(e), "already registered")
  second <- ss_register_dataset(csv, name = "svi", kind = "polygon_table", temporal = "static",
                                overwrite = TRUE)
  expect_true(last_request(dir, "dataset-register")$args$overwrite)
  expect_equal(nrow(ss_datasets()), 1L)
  expect_false(identical(first$id, second$id))
})

test_that("root and the spacescans.datasets_dir option choose the registry", {
  dir <- local_fake_bridge()
  root <- file.path(withr::local_tempdir(), "my-registry")
  dir.create(root)
  rec <- ss_register_dataset(csv_file(), name = "svi", kind = "polygon_table", temporal = "static",
                             root = root)
  expect_equal(last_request(dir, "dataset-register")$args$root, normalizePath(root))
  expect_equal(attr(rec, "root"), normalizePath(root))
  expect_equal(nrow(ss_datasets()), 0L)                      # the default registry is another one
  expect_equal(ss_datasets(root = root)$name, "svi")
  withr::local_options(spacescans.datasets_dir = root)
  expect_equal(ss_datasets()$name, "svi")
  expect_equal(last_request(dir, "dataset-list")$args$root, normalizePath(root))
  # ss_link() hands the registry to the bridge through SPACESCANS_DATASETS_DIR
  res <- ss_link(fake_cohort(), "ndi", custom = "svi", data_dir = tempdir(), progress = FALSE, cache = FALSE)
  expect_equal(attr(last_request(dir, "link"), "seen")$datasets_dir_env, normalizePath(root))
  expect_true("value" %in% names(res))
})

test_that("ss_link(custom = ) links registered datasets by name, id or record, with definitions", {
  dir <- local_fake_bridge()
  csv <- csv_file()
  svi <- ss_register_dataset(csv, name = "svi", kind = "polygon_table", temporal = "static",
                             variables = c("RPL_THEMES", "score"))
  feat <- withr::local_tempfile(fileext = ".gpkg")
  writeBin(as.raw(1:10), feat)
  parks <- ss_register_dataset(feat, name = "parks", kind = "features", temporal = "static")
  expect_equal(parks$variables$name, "parks_dist_m")
  res <- ss_link(fake_cohort(), "ndi", custom = "svi", data_dir = tempdir(), progress = FALSE, cache = FALSE)
  req <- last_request(dir, "link")
  expect_equal(unlist(req$args$datasets), "svi")
  expect_equal(unlist(req$args$variables), "ndi")         # the registry knows the dataset's variables
  expect_equal(names(res), c("pid", "ndi", "RPL_THEMES", "score"))
  expect_equal(attr(res, "coverage")$variable, c("ndi", "reg_svi"))
  # a definition from ss_custom_polygon(), a record and an id together; no catalog variable
  def <- ss_custom_polygon(csv, boundary = "Tract", key_col = "GEOID", value_cols = "score", name = "Own")
  res <- ss_link(fake_cohort(), character(), custom = list(def, parks, svi$id), data_dir = tempdir(),
                 progress = FALSE, cache = FALSE, output_level = "episode")
  req <- last_request(dir, "link")
  expect_equal(unlist(req$args$datasets), c(parks$id, svi$id))
  expect_equal(unlist(req$args$variables), def$variable_key)
  expect_equal(names(req$args$custom), def$variable_key)
  expect_true(all(c("score", "parks_dist_m", "RPL_THEMES") %in% names(res)))
  f <- attr(res, "features")
  expect_equal(f$detailed_description[f$feature_name == "parks_dist_m"], "Unit: m.")
  # names in a character vector, duplicates once
  ss_link(fake_cohort(), "ndi", custom = c("svi", "parks", "svi"), data_dir = tempdir(),
          progress = FALSE, cache = FALSE)
  expect_equal(unlist(last_request(dir, "link")$args$datasets), c("svi", "parks"))
  # the data preflight only asks about catalog variables
  expect_equal(unlist(last_request(dir, "data-status")$args$variables), "ndi")
})

test_that("an unknown registered dataset stops the link with a config error", {
  dir <- local_fake_bridge()
  e <- tryCatch(ss_link(fake_cohort(), "ndi", custom = "nope", data_dir = tempdir(), progress = FALSE,
                        cache = FALSE), error = identity)
  expect_s3_class(e, "spacescans_error_config")
  expect_match(conditionMessage(e), "no registered dataset named or with id nope")
  expect_error(ss_link(fake_cohort(), "ndi", custom = list(3), data_dir = tempdir()),
               "registered dataset's name or id", class = "spacescans_error_config")
  expect_error(ss_link(fake_cohort(), character(), data_dir = tempdir()), "at least one variable",
               class = "spacescans_error_config")
})

test_that("ss_coverage() estimates registered datasets too, and says when the bridge did not", {
  dir <- local_fake_bridge()
  ss_register_dataset(csv_file(), name = "svi", kind = "polygon_table", temporal = "static")
  cov <- ss_coverage(fake_cohort(), "ndi", custom = "svi")
  expect_equal(cov$variable, c("ndi", "reg_svi"))
  expect_equal(unlist(last_request(dir, "coverage")$args$datasets), "svi")
  dir <- local_fake_bridge(list(coverage = list(ignore_datasets = TRUE)))
  ss_register_dataset(csv_file(), name = "svi", kind = "polygon_table", temporal = "static")
  expect_warning(cov <- ss_coverage(fake_cohort(), "ndi", custom = "svi"),
                 "reported no coverage for the registered dataset\\(s\\) svi")
  expect_equal(cov$variable, "ndi")
})
