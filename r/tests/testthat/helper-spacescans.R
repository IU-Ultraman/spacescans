test_python <- function() {
  py <- Sys.getenv("SPACESCANS_PYTHON", "")
  if (nzchar(py) && file.exists(py)) py else NULL
}

skip_if_no_python <- function() {
  if (is.null(test_python())) {
    skip("no spacescans Python environment (set SPACESCANS_PYTHON to run these tests)")
  }
  invisible(test_python())
}

skip_if_no_geo <- function() {
  skip_if_no_python()
  caps <- spacescans:::.ss_hello()
  if (!isTRUE(caps$extras$geo$available)) skip("the geo extra is not installed")
}

reset_spacescans_state <- function() {
  st <- spacescans:::.ss_state
  rm(list = ls(st, all.names = TRUE), envir = st)
}

local_clean_state <- function(env = parent.frame()) {
  reset_spacescans_state()
  withr::defer(reset_spacescans_state(), envir = env)
}

# A shell script that acts as "python" but runs the bridge with the geo, rda,
# hdf4 and nc stacks hidden, i.e. like a base-only install.
base_only_python <- function(dir = withr::local_tempdir(.local_envir = parent.frame())) {
  runner <- file.path(dir, "base_only_bridge.py")
  writeLines(c(
    "import sys, importlib.util",
    "from importlib.abc import MetaPathFinder",
    "_B = {'geopandas', 'rasterio', 'shapely', 'exactextract', 'pyogrio', 'pyreadr', 'pyhdf', 'xarray', 'netCDF4'}",
    "class _Blocker(MetaPathFinder):",
    "    def find_spec(self, name, path, target=None):",
    "        if name.split('.')[0] in _B:",
    "            raise ModuleNotFoundError(f'No module named {name!r}', name=name.split('.')[0])",
    "        return None",
    "sys.meta_path.insert(0, _Blocker())",
    "_real = importlib.util.find_spec",
    "importlib.util.find_spec = lambda n, *a, **k: None if n.split('.')[0] in _B else _real(n, *a, **k)",
    "import runpy",
    "sys.argv = ['spacescans.bridge'] + sys.argv[1:]",
    "runpy.run_module('spacescans.bridge', run_name='__main__', alter_sys=True)"
  ), runner)
  wrapper <- file.path(dir, "python")
  # The client calls: <python> -I -u -X utf8 -m spacescans.bridge <args>; drop the first six.
  writeLines(c("#!/bin/sh", "shift 6",
               sprintf("exec '%s' -I -u -X utf8 '%s' \"$@\"", test_python(), runner)), wrapper)
  Sys.chmod(wrapper, "0755")
  wrapper
}

# A tiny yearly_areal C4 config whose inputs are written from R with nanoparquet.
c4_fixture <- function(dir) {
  patients <- data.frame(
    pid = c("P1", "P1", "P2"),
    startDate = as.Date(c("2017-01-01", "2018-01-01", "2017-06-01")),
    endDate = as.Date(c("2017-12-31", "2018-12-31", "2018-05-31")),
    longitude = c(-86.1, -86.2, -86.3),
    latitude = c(39.7, 39.8, 39.9),
    episode_id = 0:2
  )
  weights <- data.frame(geoid = c(0L, 1L, 2L, 2L), GEOID10 = c("A", "B", "A", "B"),
                        weight = c(1, 1, 0.5, 0.5))
  exposure <- data.frame(GEOID10 = c("A", "A", "B", "B"), year = c(2017L, 2018L, 2017L, 2018L),
                         v = c(10, 20, 30, 40))
  nanoparquet::write_parquet(patients, file.path(dir, "patients.parquet"))
  nanoparquet::write_parquet(weights, file.path(dir, "weights.parquet"))
  nanoparquet::write_parquet(exposure, file.path(dir, "exposure.parquet"))
  list(
    name = "c4_fixture",
    linkage_pattern = "yearly_areal",
    geometry_type = "polygon",
    source = list(file = "weights.parquet", join_col = "GEOID10"),
    buffer = list(patient_file = "patients.parquet", patient_adapter = "demo_conus"),
    exposure = list(file = "exposure.parquet", join_col = "GEOID10", value_cols = "v", year_col = "year"),
    time = list(years = c(2017, 2018), output_grouping = "episode"),
    output = list(path = "c4_fixture.parquet")
  )
}

process_alive <- function(pid) {
  identical(suppressWarnings(system2("kill", c("-0", pid), stdout = FALSE, stderr = FALSE)), 0L)
}
