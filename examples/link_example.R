#!/usr/bin/env Rscript
# Link a synthetic cohort to a catalog source and to two registered datasets, then check it.
#
# The sections marked "# --- S3.n <title> ---" are the worked example; the rest of this file
# runs them reproducibly and checks their results: the example runs against the data directory
# in SPACESCANS_DATA_DIR, with the dataset registry in a new temporary directory (or the one in
# SPACESCANS_EXAMPLE_REGISTRY), so your own registry is never touched and names never clash;
# the checks recompute the results independently (participant-level values = the
# contributing-day-weighted mean of the location-episode values; the county table's
# location-episode values = the day-weighted county values, every 270 m buffer lying at least
# 13 km inside one county; the distances = nearest distances computed here in EPSG:5070).
# Outputs go to examples/outputs: the linked tables (r_*.csv), what the example printed
# (r_console.txt, and per section in r_console_sections.json), the checks (r_checks.csv) and
# the run facts (r_run.csv); the exit status is 1 when a check fails.
#
# Requires the spacescans R package and its Python environment (ss_install()). The first run
# downloads the NPS Soundscape Noise rasters and the 2010 county boundaries from the project
# mirror. Synthetic data only. Run from the root of the spacescans repository:
#
#   SPACESCANS_DATA_DIR=~/spacescans-data Rscript examples/link_example.R

if (!file.exists(file.path("examples", "link_example.R"))) {
  stop("run from the root of the spacescans repository")
}
if (!nzchar(Sys.getenv("SPACESCANS_DATA_DIR"))) {
  stop("set SPACESCANS_DATA_DIR: the example downloads data into it")
}
out_dir <- file.path("examples", "outputs")
dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
own_registry <- !nzchar(Sys.getenv("SPACESCANS_EXAMPLE_REGISTRY"))
registry <- if (own_registry) tempfile("spacescans-example-registry-") else
  Sys.getenv("SPACESCANS_EXAMPLE_REGISTRY")
dir.create(registry, recursive = TRUE, showWarnings = FALSE)
Sys.setenv(SPACESCANS_DATASETS_DIR = registry)
# What each section prints is kept, one file per section.
capture_dir <- tempfile("spacescans-example-output-")
dir.create(capture_dir)
captured <- character()
.section <- function(name) {
  if (sink.number() > 0) sink()
  captured[[name]] <<- file.path(capture_dir, paste0(name, ".txt"))
  sink(captured[[name]], split = TRUE)
}

# === harness ===
.section("S3.1")
# --- S3.1 A synthetic cohort of location episodes ---
library(spacescans)

cohort <- data.frame(
  pid       = c("P1", "P1", "P2", "P3", "P3"),
  startDate = as.Date(c("2013-01-01", "2015-07-01", "2014-03-15", "2013-05-01", "2016-01-01")),
  endDate   = as.Date(c("2015-06-30", "2019-12-31", "2018-09-30", "2014-12-31", "2019-06-30")),
  longitude = c(-86.1581, -87.6359, -86.5264, -82.9988, -86.1600),
  latitude  = c(39.7684, 41.8789, 39.1653, 39.9612, 39.8100)
)

# === harness ===
.section("S3.2")
# --- S3.2 Catalog entry, data, and coverage before linkage ---
catalog <- ss_catalog()
print(catalog[catalog$variable == "noise", c("variable", "label", "value_cols")])
invisible(ss_fetch("noise"))             # into SPACESCANS_DATA_DIR (see ss_data_dir()), once
print(ss_coverage(cohort, "noise"))

# === harness ===
.section("S3.3")
# --- S3.3 Participant-level linkage (the default) ---
result <- ss_link(cohort, "noise", buffer_m = 270)
print(result)                            # one row per participant: pid and the noise columns
print(attr(result, "coverage"))          # linkage rates: share of participants with a value
# attr(result, "features"): feature dictionary; attr(result, "provenance"): versions, settings

# === harness ===
.section("S3.4")
# --- S3.4 Location-episode output (the option) ---
episodes <- ss_link(cohort, "noise", buffer_m = 270, output_level = "episode")
print(episodes)                          # the cohort, one row per location episode, plus noise

# === harness ===
.section("S3.5")
# --- S3.5 Register user-supplied datasets once, then link them by name ---
work <- tempfile("spacescans-example-")
dir.create(work)

# A yearly table keyed by 2010 county FIPS code (synthetic values)
heat_days <- list("18097" = c(8, 11, 9, 14, 10, 12, 7),     # Marion County, IN
                  "17031" = c(5, 7, 6, 9, 8, 6, 4),         # Cook County, IL
                  "18105" = c(10, 12, 9, 15, 11, 13, 8),    # Monroe County, IN
                  "39049" = c(7, 9, 8, 12, 9, 11, 6))       # Franklin County, OH
heat <- data.frame(county_fips = rep(names(heat_days), each = 7),
                   year = rep(2013:2019, times = length(heat_days)),
                   heat_days = unlist(heat_days, use.names = FALSE))
write.csv(heat, file.path(work, "county_heat.csv"), row.names = FALSE)

# Point features (synthetic clinic sites), linked as the distance to the nearest one
sites <- data.frame(site = paste0("C", 1:5),
                    lon = c(-86.1500, -87.6250, -86.5350, -83.0100, -86.1700),
                    lat = c(39.7750, 41.8850, 39.1700, 39.9700, 39.8200))
features <- sprintf(paste0('{"type": "Feature", "properties": {"site": "%s"}, ',
                           '"geometry": {"type": "Point", "coordinates": [%.4f, %.4f]}}'),
                    sites$site, sites$lon, sites$lat)
writeLines(sprintf('{"type": "FeatureCollection", "features": [%s]}',
                   paste(features, collapse = ", ")),
           file.path(work, "clinics.geojson"))

heat_record <- ss_register_dataset(
  file.path(work, "county_heat.csv"), name = "county_heat",
  kind = "polygon_table", temporal = "yearly",
  key = list(boundary = "county", column = "county_fips", year_column = "year"),
  variables = data.frame(name = "heat_days", unit = "days",
                         description = "Extreme-heat days per year (synthetic)"),
  source = "Synthetic example", version = "1",
  description = "County table for the Supplement (synthetic)")
clinic_record <- ss_register_dataset(
  file.path(work, "clinics.geojson"), name = "clinics",
  kind = "features", temporal = "static",
  source = "Synthetic example", version = "1",
  description = "Clinic sites for the Supplement (synthetic)")
registered <- ss_datasets()                # listed by name below
cols <- c("name", "kind", "temporal", "visibility", "variables")
print(registered[order(registered$name), cols], row.names = FALSE)

invisible(ss_fetch(datasets = "county_boundaries"))   # the 2010 county polygons of the table
later <- ss_link(cohort, "noise", buffer_m = 270, custom = c("county_heat", "clinics"))
print(later)                             # noise, heat_days and clinics_dist_m per participant

# === Checks ==================================================
sink()
printed <- vapply(captured, function(path) {
  lines <- readLines(path, warn = FALSE)
  if (length(lines)) paste0(lines, "\n", collapse = "") else ""
}, character(1))
writeLines(paste0(printed, collapse = ""), file.path(out_dir, "r_console.txt"), sep = "")
jsonlite::write_json(as.list(printed), file.path(out_dir, "r_console_sections.json"),
                     auto_unbox = TRUE, pretty = TRUE)
unlink(capture_dir, recursive = TRUE)
checks <- data.frame(check = character(), passed = logical(), max_abs_diff = numeric(),
                     tolerance = numeric(), values = integer(), stringsAsFactors = FALSE)
check <- function(name, got, want, tol) {
  if (is.null(got)) {
    checks[nrow(checks) + 1, ] <<- list(name, FALSE, NA_real_, tol, 0L)
    cat("FAIL ", name, ": column missing from the output\n", sep = "")
    return(invisible())
  }
  got <- as.numeric(got)
  want <- as.numeric(want)
  same_missing <- identical(is.na(got), is.na(want))
  diff <- if (any(!is.na(got) & !is.na(want))) max(abs(got - want), na.rm = TRUE) else 0
  ok <- same_missing && diff <= tol
  checks[nrow(checks) + 1, ] <<- list(name, ok, diff, tol, sum(!is.na(want)))
  cat(if (ok) "PASS " else "FAIL ", name, ": max |diff| ", format(diff, digits = 3),
      " (tolerance ", tol, ")\n", sep = "")
}
column <- function(df, col) if (col %in% names(df)) df[[col]] else NULL
scale_tol <- function(x) 1e-9 * max(1, max(abs(x), na.rm = TRUE))

days <- as.numeric(cohort$endDate - cohort$startDate) + 1   # start and end inclusive
pids <- unique(cohort$pid)                                    # first appearance

# The article's participant-level formula from location-episode values: every source here has a
# value on every day of an episode or on none, so an episode's contributing days are its duration
# when defined.
participant_reference <- function(ep, cols) {
  out <- data.frame(pid = pids, stringsAsFactors = FALSE)
  for (col in cols) {
    out[[col]] <- vapply(pids, function(p) {
      sel <- ep$pid == p
      x <- as.numeric(ep[[col]][sel])
      d <- ifelse(is.na(x), 0, days[sel])
      if (sum(d) > 0) sum(d * ifelse(is.na(x), 0, x)) / sum(d) else NA_real_
    }, numeric(1))
  }
  out
}

# Day-weighted county values per location episode (each buffer lies inside one county).
heat_reference <- function() {
  county <- c("18097", "17031", "18105", "39049", "18097")
  vapply(seq_len(nrow(cohort)), function(e) {
    s <- cohort$startDate[e]
    t <- cohort$endDate[e]
    years <- as.integer(format(s, "%Y")):as.integer(format(t, "%Y"))
    d <- vapply(years, function(y) {
      lo <- max(s, as.Date(sprintf("%d-01-01", y)))
      hi <- min(t, as.Date(sprintf("%d-12-31", y)))
      as.numeric(hi - lo) + 1
    }, numeric(1))
    sum(d * heat_days[[county[e]]][years - 2012]) / sum(d)
  }, numeric(1))
}

# EPSG:5070 (NAD83 / Conus Albers, GRS80): Snyder (1987), Map Projections - A Working Manual,
# equations 3-12 and 14-1 to 14-4. Datum shifts cancel in distances between nearby points.
albers <- function(lon, lat) {
  a <- 6378137
  f <- 1 / 298.257222101
  e2 <- f * (2 - f)
  e <- sqrt(e2)
  rad <- pi / 180
  m <- function(phi) cos(phi) / sqrt(1 - e2 * sin(phi)^2)
  q <- function(phi) (1 - e2) * (sin(phi) / (1 - e2 * sin(phi)^2) -
                                   log((1 - e * sin(phi)) / (1 + e * sin(phi))) / (2 * e))
  p1 <- 29.5 * rad
  p2 <- 45.5 * rad
  p0 <- 23 * rad
  n <- (m(p1)^2 - m(p2)^2) / (q(p2) - q(p1))
  cc <- m(p1)^2 + n * q(p1)
  rho <- function(phi) a * sqrt(cc - n * q(phi)) / n
  theta <- n * (lon - (-96)) * rad
  r <- rho(lat * rad)
  cbind(x = r * sin(theta), y = rho(p0) - r * cos(theta))
}
distance_reference <- function() {
  p <- albers(cohort$longitude, cohort$latitude)
  s <- albers(sites$lon, sites$lat)
  vapply(seq_len(nrow(p)), function(i) {
    min(sqrt((s[, "x"] - p[i, "x"])^2 + (s[, "y"] - p[i, "y"])^2))
  }, numeric(1))
}
episode_table <- function(res, cols) {
  missing <- setdiff(cols, names(res))
  if (length(missing)) cat("FAIL location-episode output lacks column(s)", missing, "\n")
  ep <- data.frame(pid = cohort$pid, stringsAsFactors = FALSE)
  for (col in cols) ep[[col]] <- if (col %in% names(res)) res[[col]] else NA_real_
  ep
}

noise_cols <- c("l50dba_exi", "l50dba_imp", "l50dba_nat")
if (!identical(as.character(result$pid), pids)) {
  stop("participant rows are not one per participant in first-appearance order")
}
ep_noise <- episode_table(episodes, noise_cols)
ref <- participant_reference(ep_noise, noise_cols)
for (col in noise_cols) {
  check(paste("noise participant", col, "= day-weighted mean of location-episode values"),
        column(result, col), ref[[col]], scale_tol(ref[[col]]))
}
later_cols <- c(noise_cols, "heat_days", "clinics_dist_m")
episodes_later <- ss_link(cohort, "noise", buffer_m = 270, custom = c("county_heat", "clinics"),
                          output_level = "episode")
ep_later <- episode_table(episodes_later, later_cols)
check("county table, location-episode heat_days = day-weighted county values",
      ep_later$heat_days, heat_reference(), 1e-6)
check("features, location-episode clinics_dist_m = nearest distance in EPSG:5070 (m)",
      ep_later$clinics_dist_m, distance_reference(), 1.0)
if (!identical(as.character(later$pid), pids)) {
  stop("registered-dataset link: participant rows are not one per participant in order")
}
ref_later <- participant_reference(ep_later, later_cols)
for (col in later_cols) {
  check(paste("registered link participant", col, "= day-weighted mean of location-episode values"),
        column(later, col), ref_later[[col]], scale_tol(ref_later[[col]]))
}
for (col in noise_cols) {
  check(paste("noise", col, "unchanged when registered datasets are linked too"),
        column(later, col), result[[col]], scale_tol(result[[col]]))
}

# ---- outputs
plain <- function(df) {
  df <- as.data.frame(df)
  attributes(df)[setdiff(names(attributes(df)), c("names", "row.names", "class"))] <- NULL
  df
}
write.csv(plain(result), file.path(out_dir, "r_noise_participant.csv"), row.names = FALSE)
write.csv(plain(attr(result, "coverage")), file.path(out_dir, "r_noise_linkage_rates.csv"),
          row.names = FALSE)
write.csv(ep_noise, file.path(out_dir, "r_noise_location_episode.csv"), row.names = FALSE)
write.csv(plain(later), file.path(out_dir, "r_registered_participant.csv"), row.names = FALSE)
write.csv(ep_later, file.path(out_dir, "r_registered_location_episode.csv"), row.names = FALSE)
write.csv(checks, file.path(out_dir, "r_checks.csv"), row.names = FALSE)
write.csv(data.frame(spacescans_r = as.character(utils::packageVersion("spacescans")),
                     r = paste(R.version$major, R.version$minor, sep = "."),
                     platform = R.version$platform,
                     os = utils::sessionInfo()$running,
                     machine = Sys.info()[["machine"]],
                     run_date = format(Sys.Date()),
                     run_utc = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
                     checks_passed = sum(checks$passed), checks = nrow(checks)),
          file.path(out_dir, "r_run.csv"), row.names = FALSE)
if (own_registry) unlink(registry, recursive = TRUE)
unlink(work, recursive = TRUE)
cat(sum(checks$passed), "of", nrow(checks), "checks passed; outputs in", out_dir, "\n")
quit(status = if (all(checks$passed)) 0 else 1)
