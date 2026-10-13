#' The JSON schema of a pipeline config
#'
#' The schema of `DatasetConfig`, the model every C3 and C4 config is
#' validated against (fields, types, defaults and allowed values).
#'
#' @param as `"list"` (default) for a nested R list, or `"json"` for the
#'   schema as a JSON string.
#' @return The schema.
#' @examples
#' \dontrun{
#' schema <- ss_config_schema()
#' names(schema$properties)
#' }
#' @export
ss_config_schema <- function(as = c("list", "json")) {
  as <- match.arg(as)
  value <- .ss_bridge("schema")
  if (as == "json") {
    return(jsonlite::toJSON(value$schema, auto_unbox = TRUE, null = "null", pretty = TRUE))
  }
  value$schema
}

.ss_config_arg <- function(config) {
  if (is.character(config) && length(config) == 1L) {
    path <- path.expand(config)
    if (!file.exists(path)) .ss_abort("config", sprintf("Config file not found: %s", config))
    return(normalizePath(path))
  }
  if (is.list(config) && !is.null(names(config))) return(config)
  .ss_abort("config", "`config` must be a path to a YAML file or a named list.")
}

.ss_dir_arg <- function(path, name, create = FALSE) {
  if (is.null(path)) return(NULL)
  if (!is.character(path) || length(path) != 1L || is.na(path)) {
    .ss_abort("config", sprintf("`%s` must be a single path.", name))
  }
  path <- path.expand(path)
  if (create) dir.create(path, recursive = TRUE, showWarnings = FALSE)
  normalizePath(path, mustWork = FALSE)
}

#' Validate a pipeline config without running it
#'
#' Resolves every path (relative paths against `data_dir` and `output_dir`)
#' and validates the config against the schema. Length-one R vectors are
#' accepted where the schema wants a list (`value_cols = "pm25"`). Keys the
#' schema does not know are reported, because the pipeline would ignore them.
#'
#' @param config Path to a YAML config, or the config as a named list.
#' @param data_dir Root for relative input paths. Default: the
#'   `SPACESCANS_DATA_DIR` environment variable, then the config's `base_dir`.
#' @param output_dir Root for a relative `output.path`.
#' @param strict If `TRUE`, unknown keys are an error instead of a warning.
#' @return The validated config as a list, with every default filled in and
#'   paths absolute; attribute `unknown_keys` lists ignored keys.
#' @examples
#' \dontrun{
#' cfg <- ss_validate("c4/zbp.yaml", data_dir = "~/ss-data")
#' cfg$exposure$value_cols
#' }
#' @export
ss_validate <- function(config, data_dir = NULL, output_dir = NULL, strict = FALSE) {
  args <- list(config = .ss_config_arg(config),
               data_dir = .ss_dir_arg(data_dir, "data_dir"),
               output_dir = .ss_dir_arg(output_dir, "output_dir"),
               strict = isTRUE(strict))
  value <- .ss_bridge("resolve", Filter(Negate(is.null), args))
  out <- .ss_simplify(value$config)
  attr(out, "unknown_keys") <- unlist(value$unknown_keys) %||% character()
  out
}

#' Config templates shipped with the Python package
#'
#' Two sets exist: the 25 catalog templates that [ss_link()] renders for the
#' catalog variables (`kind = "catalog"`, the default; listed with their
#' SHA-256), and the 5 starter configs for writing your own C3/C4 configs
#' for [ss_run()] (`kind = "init"`).
#'
#' @param name `NULL` (default) to list the templates, or a template name
#'   such as `"c3/county"` to get that template.
#' @param kind `"catalog"` or `"init"`.
#' @return Without `name`, a data frame with one row per template (`name`,
#'   `kind`, `file`, and for catalog templates `sha256`, ...). With `name`,
#'   the template as a list (attribute `text` holds the YAML text).
#' @examples
#' \dontrun{
#' ss_templates()
#' ss_templates(kind = "init")
#' cfg <- ss_templates("c4/zbp", kind = "init")
#' }
#' @export
ss_templates <- function(name = NULL, kind = c("catalog", "init")) {
  kind <- match.arg(kind)
  if (is.null(name)) {
    rows <- .ss_bridge("templates", list(kind = kind))$templates
    df <- .ss_records_df(rows, first = c("name", "kind", "file", "config_name", "linkage_pattern", "sha256"))
    return(df)
  }
  value <- .ss_bridge("templates", list(name = name, kind = kind))
  out <- .ss_simplify(value$config)
  attr(out, "text") <- value$text
  out
}

#' Read and write pipeline configs as YAML
#'
#' `ss_read_config()` parses a YAML config into a list; `ss_write_config()`
#' writes a list as YAML the way spacescans-web does
#' (`yaml.safe_dump(sort_keys = FALSE)`), wrapping length-one vectors into
#' lists where the schema needs a list. Both use the Python bridge, so no R
#' YAML package is needed.
#'
#' @param path Path of the YAML file.
#' @param config The config as a named list.
#' @return `ss_read_config()`: the config as a list. `ss_write_config()`:
#'   invisibly, the YAML text.
#' @examples
#' \dontrun{
#' cfg <- ss_read_config("c3_bg.yaml")
#' cfg$buffer$buffer_m <- 500L
#' ss_write_config(cfg, "c3_bg_500m.yaml")
#' }
#' @export
ss_read_config <- function(path) {
  path <- .ss_config_arg(path)
  if (!is.character(path)) .ss_abort("config", "`path` must be a file path.")
  .ss_simplify(.ss_bridge("yaml-load", list(path = path))$config)
}

#' @rdname ss_read_config
#' @export
ss_write_config <- function(config, path) {
  if (!is.list(config) || is.null(names(config))) .ss_abort("config", "`config` must be a named list.")
  target <- .ss_dir_arg(path, "path")
  value <- .ss_bridge("yaml-dump", list(config = config, path = target))
  invisible(value$text)
}
