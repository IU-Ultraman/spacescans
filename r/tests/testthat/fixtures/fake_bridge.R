# A stand-in for `python -m spacescans.bridge` used by the tests: it speaks
# protocol 1 (request/result files, JSON Lines events on stdout, exit codes)
# without Python. Started through a shell wrapper (see helper-fake.R) as
#   Rscript fake_bridge.R <command> --request <req> --result <res>
#
# Environment:
#   SS_FAKE_VERSION   the spacescans version to report
#   SS_FAKE_SCENARIO  JSON file: {"<command>": {options}}; options are
#                     value (returned as is), error {kind, message, details},
#                     exit (exit code without a result), signal (kill self),
#                     sleep_s (run that long, emitting progress), events
#                     (extra events), and command-specific ones below
#   SS_FAKE_LOG       directory; each request is copied there as
#                     <seq>-<command>.json with a <seq>-<command>.seen.json
#                     recording what the fake saw (cohort columns, modes)

suppressMessages({
  library(jsonlite)
  library(nanoparquet)
})

`%||%` <- function(x, y) if (is.null(x)) y else x

argv <- commandArgs(trailingOnly = TRUE)
command <- argv[1]
opt <- function(flag) {
  i <- match(flag, argv)
  if (is.na(i)) NULL else argv[i + 1L]
}
req_path <- opt("--request")
res_path <- opt("--result")

emit <- function(ev, ...) {
  line <- toJSON(c(list(ev = ev, t = round(as.numeric(Sys.time()), 3)), list(...)),
                 auto_unbox = TRUE, null = "null", digits = NA)
  cat(line, "\n", sep = "")
  flush(stdout())
}

write_result <- function(ok, value = NULL, error = NULL, warnings = list()) {
  res <- list(protocol = 1L, ok = ok, command = command,
              spacescans = list(version = Sys.getenv("SS_FAKE_VERSION", "0.0.0"), code_fp = NULL,
                                python = "3.12.0", executable = "/fake/python",
                                platform = "macOS-15.0-arm64", pid = Sys.getpid()),
              elapsed_s = 0.01, value = if (ok) value, warnings = warnings, error = error)
  if (is.null(res_path)) {
    emit("result", result = res)
    return(invisible())
  }
  tmp <- paste0(res_path, ".tmp")
  writeLines(toJSON(res, auto_unbox = TRUE, null = "null", digits = NA, pretty = TRUE), tmp)
  file.rename(tmp, res_path)
}

fail <- function(kind, message, details = list(), exit = 1L, hint = NULL) {
  write_result(FALSE, error = list(kind = kind, message = message, hint = hint, details = details,
                                   type = "fake.Error", traceback = "Traceback (fake)"))
  emit("done", ok = FALSE, exit = exit)
  quit(save = "no", status = exit)
}

scenario <- list()
sc_path <- Sys.getenv("SS_FAKE_SCENARIO", "")
if (nzchar(sc_path) && file.exists(sc_path)) scenario <- read_json(sc_path, simplifyVector = FALSE)
sc <- scenario[[command]] %||% list()

req <- if (!is.null(req_path)) read_json(req_path, simplifyVector = FALSE) else list(args = list())
args <- req$args %||% list()
seen <- list(cwd = getwd(), bridge_parent = Sys.getenv("SPACESCANS_BRIDGE_PARENT"))

log_dir <- Sys.getenv("SS_FAKE_LOG", "")
seq_id <- NA
if (nzchar(log_dir)) {
  dir.create(log_dir, showWarnings = FALSE, recursive = TRUE)
  seq_id <- sprintf("%03d", length(list.files(log_dir, pattern = "^[0-9]+-[a-z-]+[.]json$")) + 1L)
  if (!is.null(req_path)) file.copy(req_path, file.path(log_dir, sprintf("%s-%s.json", seq_id, command)))
}
log_seen <- function() {
  if (nzchar(log_dir)) {
    writeLines(toJSON(seen, auto_unbox = TRUE, null = "null", digits = NA),
               file.path(log_dir, sprintf("%s-%s.seen.json", seq_id, command)))
  }
}

# Protocol checks, as the real bridge does them (exit 2).
if (!identical(as.integer(req$protocol %||% 1L), 1L)) fail("protocol", "unsupported protocol", exit = 2L)
if (!is.null(req$command) && !identical(req$command, command)) fail("protocol", "command mismatch", exit = 2L)
rng <- unlist(req$client$protocol_range)
if (!is.null(rng) && !(rng[1] <= 1 && 1 <= rng[2])) fail("protocol", "client protocol range excludes 1", exit = 2L)

emit("start", command = command, pid = Sys.getpid(), protocol = 1L)
for (ev in sc$events %||% list()) do.call(emit, ev)

is_abs <- function(p) is.character(p) && length(p) == 1L && startsWith(p, "/")
for (key in c("cohort", "data_dir", "run_dir", "cache_dir", "output_dir")) {
  if (!is.null(args[[key]]) && !is_abs(args[[key]])) fail("config", sprintf("argument '%s' must be an absolute path", key))
}

if (!is.null(sc$signal)) {
  tools::pskill(Sys.getpid(), as.integer(sc$signal))
  Sys.sleep(5)
}
if (!is.null(sc$exit)) quit(save = "no", status = as.integer(sc$exit))

# A long run: progress events at most 4 per second; SIGINT -> exit 130.
interrupted <- function() {
  write_result(FALSE, error = list(kind = "interrupted", message = "interrupted by SIGINT",
                                   hint = NULL, details = list(signal = "SIGINT"),
                                   type = "KeyboardInterrupt", traceback = ""))
  emit("done", ok = FALSE, exit = 130L)
  quit(save = "no", status = 130L)
}
sleep_s <- as.numeric(sc$sleep_s %||% 0)
if (sleep_s > 0) {
  if (!is.null(sc$pid_file)) writeLines(as.character(Sys.getpid()), sc$pid_file)
  tryCatch({
    t0 <- Sys.time()
    last <- 0
    repeat {
      el <- as.numeric(difftime(Sys.time(), t0, units = "secs"))
      if (el >= sleep_s) break
      if (el - last >= 0.25) {
        emit("progress", step = sc$step %||% "c3_fake", frac = round(el / sleep_s, 4),
             overall = round(el / sleep_s / 2, 4), text = sprintf("[overlap] %d/100", round(100 * el / sleep_s)))
        last <- el
      }
      Sys.sleep(0.05)
    }
  }, interrupt = function(e) interrupted())
}

if (!is.null(sc$error)) {
  e <- sc$error
  fail(e$kind %||% "runtime", e$message %||% "fake failure", details = e$details %||% list(),
       exit = as.integer(e$exit %||% 1L), hint = e$hint)
}

# --- default values per command -------------------------------------------

version <- Sys.getenv("SS_FAKE_VERSION", "0.0.0")
commands <- c("capabilities", "hello", "catalog", "coverage", "data-status", "data-plan",
              "data-verify", "data-fetch", "data-import", "data-terms", "custom-validate", "link",
              "cache-info", "cache-clear", "run", "schema", "quickstart", "resolve", "templates",
              "yaml-load", "yaml-dump", "ping")

catalog_vars <- list(
  list(key = "ndi", label = "Neighborhood Deprivation Index (NDI)", experiment = "bg_ndi_wi",
       spatial_method = "areal", boundary = "BG", coverage_years = list(2012L, 2022L),
       coverage_region = "CONUS", data_source = "US Census ACS (5-year)", temporal = "yearly",
       value_cols = list("ndi"), datasets = list("bg_boundaries", "ndi_panel"), extras = list("geo")),
  list(key = "walkability", label = "Walkability Index", experiment = "bg_ndi_wi",
       spatial_method = "areal", boundary = "BG", coverage_years = list(2021L, 2021L),
       coverage_region = "CONUS", data_source = "EPA Smart Location Database", temporal = "static",
       value_cols = list("NatWalkInd"), datasets = list("bg_boundaries", "walkability_panel"),
       extras = list("geo")),
  list(key = "temis", label = "TEMIS UV index", experiment = "temis", spatial_method = "grid",
       boundary = "Point", coverage_years = list(2004L, 2023L), coverage_region = "CONUS",
       data_source = "TEMIS", temporal = "daily", value_cols = list("uvi"),
       datasets = list("temis_template", "temis_uv_raw"), extras = list("geo", "hdf4"))
)
catalog_features <- list(
  list(feature_name = "ndi", short_description = "NDI", detailed_description = "Deprivation index", variable = "ndi"),
  list(feature_name = "NatWalkInd", short_description = "Walkability", detailed_description = "EPA index", variable = "walkability"),
  list(feature_name = "uvi", short_description = "UV index", detailed_description = "Erythemal UV", variable = "temis")
)

var_status <- function(v, missing) {
  bad <- v %in% missing
  list(variable = v, status = if (bad) (sc$status %||% "missing") else "ready", usable = !bad,
       datasets = list("bg_boundaries", paste0(v, "_panel")),
       chosen = list("bg_boundaries", paste0(v, "_panel")),
       not_usable = if (bad) list(paste0(v, "_panel")) else list(),
       download_bytes = if (bad) 1234567 else 0, disk_bytes = 2345678,
       `next` = if (bad) sprintf("spacescans data fetch --dataset %s_panel", v) else "")
}

value <- sc$value
if (is.null(value)) {
  value <- switch(command,
    capabilities = , hello = list(
      protocol = 1L, protocol_range = list(1L, 1L),
      spacescans = list(version = version, code_fp = NULL, path = "/fake/spacescans"),
      python = list(version = "3.12.0", executable = "/fake/python"),
      platform = list(system = "Darwin", machine = "arm64"),
      extras = list(geo = list(available = TRUE, missing = list()),
                    hdf4 = list(available = isTRUE(sc$hdf4), missing = list("pyhdf"))),
      registry = list(unavailable = setNames(list(), character())),
      native = list(gdal = "3.12.0", geos = "3.13.0", proj = "9.6.0"),
      catalog_hash = "sha256:fake", data_release = "2026.07",
      commands = as.list(sc$commands %||% commands)),
    catalog = list(variables = catalog_vars, features = catalog_features, data_release = "2026.07",
                   catalog_hash = "sha256:fake"),
    `data-status` = {
      vars <- unlist(args$variables) %||% vapply(catalog_vars, function(v) v$key, "")
      missing <- unlist(sc$missing)
      vs <- lapply(vars, var_status, missing = missing)
      list(data_dir = args$data_dir, data_release = "2026.07", deep = isTRUE(args$deep),
           ready = !any(vars %in% missing), variables = vs,
           datasets = lapply(unique(unlist(lapply(vs, function(v) v$chosen))), function(d) {
             bad <- d %in% paste0(missing, "_panel")
             list(dataset = d, status = if (bad) "missing" else "ready", usable = !bad, title = d,
                  acquire = "mirror", download_bytes = if (bad) 1234567 else 0, disk_bytes = 2345678,
                  present = if (bad) 0L else 3L, expected = 3L,
                  `next` = if (bad) sprintf("spacescans data fetch --dataset %s", d) else "")
           }),
           download_bytes = 0, disk_bytes = 0, free_bytes = 1e12, next_steps = list(),
           warnings = list())
    },
    `data-plan` = list(
      data_dir = args$data_dir,
      items = list(list(artifact = "ndi_panel.tar.gz", datasets = list("ndi_panel"), kind = "tar",
                        bytes = 1234567, disk_bytes = 2345678, resume_from = 0, remaining_bytes = 1234567,
                        verified_archive = FALSE, source = "iu-onedrive")),
      manual = sc$manual %||% list(), already_usable = list("bg_boundaries"),
      download_bytes = 1234567, disk_bytes = 2345678, free_bytes = 1e12,
      terms_required = sc$terms_required %||% list(), terms_used = list()),
    `data-fetch` = {
      need <- vapply(scenario[["data-plan"]]$terms_required %||% list(), function(t) t$id, "")
      acc <- unlist(args$accept_terms)
      missing_terms <- setdiff(need, acc)
      if (length(missing_terms)) {
        fail("terms_required", sprintf("terms %s must be accepted", missing_terms[1]),
             details = list(terms = Filter(function(t) t$id %in% missing_terms,
                                           scenario[["data-plan"]]$terms_required)))
      }
      emit("download", artifact = "ndi_panel.tar.gz", bytes = 617283, total = 1234567, rate = 1e6)
      emit("download", artifact = "ndi_panel.tar.gz", bytes = 1234567, total = 1234567, rate = 1e6)
      seen$accept_terms <- acc
      list(data_dir = args$data_dir, dry_run = FALSE, ok = TRUE, plan = list(),
           items = list(list(artifact = "ndi_panel.tar.gz", outcome = sc$outcome %||% "downloaded",
                             bytes_transferred = 1234567, seconds = 1.2, files = 3L, attempts = 1L,
                             error = NULL)),
           manual = list(), status = NULL)
    },
    `data-verify` = list(data_dir = args$data_dir, deep = isTRUE(args$deep),
                         datasets = list(list(dataset = "ndi_panel", status = "ready", files_checked = 3L))),
    `data-import` = list(dataset = args$dataset, imported = length(unlist(args$paths)), data_dir = args$data_dir),
    `data-terms` = list(dataset = args$dataset, title = "EPA FAQSD", provider = "EPA",
                        license = "public domain", redistribution = "allowed", attribution = "EPA",
                        source_url = "https://www.epa.gov/", acquire = "provider",
                        terms = list(id = "epa-faqsd", url = "https://www.epa.gov/terms"),
                        instructions = list("Download the files from the EPA site.")),
    `custom-validate` = {
      o <- args$options %||% list()
      files <- unlist(args$files)
      seen$files <- files
      vk <- paste0("custom_", substr(gsub("[^a-z0-9]", "", tolower(basename(files[1]))), 1, 8))
      list(definition = list(
        label = o$label %||% sub("[.][^.]*$", "", basename(files[1])),
        boundary = if (identical(args$kind, "raster")) "Point" else o$boundary,
        spatial_method = if (identical(args$kind, "raster")) "grid" else "areal",
        coverage_years = list(2010L, 2020L), coverage_region = "CONUS", experiment = "custom",
        temporal = if (is.null(o$year_col) && is.null(o$years)) "static" else "yearly",
        value_cols = o$value_cols %||% list("value"),
        geometry = if (identical(args$kind, "raster")) "raster" else "polygon",
        dataset_id = sub("custom_", "", vk), variable_key = vk,
        values_path = if (identical(args$kind, "polygon")) files[1],
        value_labels = setNames(list(), character()), sha256 = "0"))
    },
    coverage = {
      co <- read_parquet(args$cohort)
      seen$cohort_columns <- names(co)
      vars <- unlist(args$variables)
      list(row_count = nrow(co), variables = setNames(lapply(vars, function(v) list(
        coverage_years = list(2012L, 2022L), patients_in_time_window = nrow(co),
        patients_in_region = nrow(co), patients_covered = nrow(co), coverage_pct = 100,
        warnings = list(), boundary = "BG", display_unit = "", temporal = "yearly")), vars))
    },
    link = {
      co <- read_parquet(args$cohort)
      seen$cohort_columns <- names(co)
      seen$cohort_types <- vapply(co, function(x) class(x)[1], "")
      seen$run_dir_mode <- as.character(file.info(args$run_dir)$mode)
      seen$cohort_mode <- as.character(file.info(args$cohort)$mode)
      seen$cache_dir_mode <- if (!is.null(args$cache_dir)) as.character(file.info(args$cache_dir)$mode)
      seen$data_dir_env <- Sys.getenv("SPACESCANS_DATA_DIR")
      n <- nrow(co)
      vars <- unlist(args$variables)
      custom <- args$custom %||% list()
      reserved <- unlist(args$reserved_names)
      cols <- list()
      feats <- list()
      cov <- list()
      steps <- list()
      for (i in seq_along(vars)) {
        v <- vars[i]
        vc <- if (!is.null(custom[[v]])) unlist(custom[[v]]$value_cols) else
          switch(v, walkability = "NatWalkInd", temis = "uvi", v)
        emit("step_start", exp = v, step = paste0("c3_", v), i = i - 1L, n = length(vars),
             overall = round((i - 1) / length(vars), 3))
        emit("progress", step = paste0("c3_", v), frac = 0.5, overall = round((i - 0.5) / length(vars), 3),
             text = "[overlap_fast] tile 50/100 ( 50.0%)")
        emit("step_end", step = paste0("c3_", v), rc = 0L, seconds = 0.1, maxrss_bytes = 1e8)
        na_frac <- as.numeric(sc$na_frac[[v]] %||% 0)
        with_value <- n
        for (c in vc) {
          name <- if (c %in% reserved) paste0(c, "_", v, "_dup") else c
          vals <- round(seq_len(n) / 7 + i, 4)
          if (na_frac > 0) vals[seq_len(floor(n * na_frac))] <- NA
          with_value <- sum(!is.na(vals))
          cols[[name]] <- vals
          feats[[length(feats) + 1L]] <- list(feature_name = name, short_description = paste("fake", c),
                                              detailed_description = paste("fake feature", c, "of", v))
        }
        cov[[length(cov) + 1L]] <- list(variable = v, episodes = n, with_value = with_value,
                                        pct = round(100 * with_value / n, 1))
        steps[[length(steps) + 1L]] <- list(name = paste0("c3_", v), status = "ran", seconds = 0.1,
                                            rc = 0L, maxrss_bytes = 1e8, cache_key = paste0("key_", v))
      }
      ids <- seq_len(n) - 1
      if (isTRUE(sc$bad_ids)) ids <- rev(ids)
      ex <- data.frame(episode_id = ids)
      for (nm in names(cols)) ex[[nm]] <- cols[[nm]]
      exp_path <- file.path(args$run_dir, "exposures.parquet")
      write_parquet(ex, exp_path, schema = parquet_schema(episode_id = "INT64"))
      feat_path <- file.path(args$run_dir, "feature_dictionary.csv")
      write.csv(do.call(rbind, lapply(feats, as.data.frame)), feat_path, row.names = FALSE)
      prov_path <- file.path(args$run_dir, "provenance.json")
      writeLines(toJSON(list(spacescans = list(version = version, code_fp = "sha256:fake"),
                             variables = vars, buffer_m = args$buffer_m, threads = args$threads,
                             cache = list(dir = args$cache_dir), platform = "macOS-15.0-arm64"),
                        auto_unbox = TRUE, null = "null"), prov_path)
      list(exposures = exp_path, rows = n, episodes = n, patients = length(unique(co$pid)),
           features = feat_path, coverage = cov, steps = steps,
           failed = as.list(unlist(sc$failed)), skipped = list(),
           warnings = sc$warnings %||% list(), provenance = prov_path)
    },
    `cache-info` = list(cache_dir = args$cache_dir, entries = 2L, bytes = 2048L,
                        oldest = "2026-10-01T00:00:00", newest = "2026-10-12T00:00:00"),
    `cache-clear` = list(cache_dir = args$cache_dir, removed = 2L, bytes = 2048L,
                         older_than_s = args$older_than_s),
    templates = list(templates = list(
      list(name = "c3/bg_us_demo", kind = "c3", file = "c3/bg_us_demo.yaml", sha256 = "abc"),
      list(name = "c4/ndi_demo", kind = "c4", file = "c4/ndi_demo.yaml", sha256 = "def"))),
    ping = list(pong = TRUE, echo = args$echo),
    fail("protocol", sprintf("unknown command '%s'", command), exit = 2L)
  )
}

log_seen()
write_result(TRUE, value = value, warnings = sc$result_warnings %||% list())
emit("done", ok = TRUE, exit = 0L)
quit(save = "no", status = 0L)
