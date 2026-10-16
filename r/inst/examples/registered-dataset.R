# Registering your own data once and linking it by name. The dataset here
# is three points (city parks) written as GeoJSON; the variable is the
# distance in metres from each location to the nearest point. It needs no
# catalog data, only the Python side (ss_install()):
#   source(system.file("examples", "registered-dataset.R", package = "spacescans"))
library(spacescans)

cohort <- data.frame(
  pid = c("P1", "P2", "P2", "P3"),
  startDate = as.Date(c("2015-01-01", "2014-03-01", "2016-07-01", "2017-01-01")),
  endDate = as.Date(c("2017-12-31", "2016-06-30", "2019-12-31", "2018-06-30")),
  longitude = c(-86.1581, -87.6298, -84.3880, -122.4194),
  latitude = c(39.7684, 41.8781, 33.7490, 37.7749)
)

# GeoJSON coordinates are longitude and latitude (WGS 84).
parks <- file.path(tempdir(), "example_parks.geojson")
writeLines('{"type": "FeatureCollection", "features": [
  {"type": "Feature", "properties": {"name": "A"}, "geometry": {"type": "Point", "coordinates": [-86.1700, 39.7800]}},
  {"type": "Feature", "properties": {"name": "B"}, "geometry": {"type": "Point", "coordinates": [-87.6200, 41.8800]}},
  {"type": "Feature", "properties": {"name": "C"}, "geometry": {"type": "Point", "coordinates": [-84.3700, 33.7600]}}
]}', parks)

# Prepared once and stored in the registry (see ?ss_datasets for where).
rec <- ss_register_dataset(parks, name = "example_parks", kind = "features", temporal = "static",
                           description = "Three example park locations.", overwrite = TRUE)
print(rec)
print(ss_datasets())

# Linked by name, like a catalog variable; no catalog variable is needed here.
data_dir <- tryCatch(ss_data_dir(), spacescans_error_config = function(e) tempdir())
res <- ss_link(cohort, character(), custom = "example_parks", data_dir = data_dir)
print(res)                        # pid and example_parks_dist_m, one row per participant

ss_remove_dataset("example_parks")
