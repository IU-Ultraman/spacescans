# spacescans bridge, protocol 1

The bridge runs one command per process for clients that do not import
spacescans in-process (the R package today; the web later). Implementation:
`src/spacescans/bridge/`. Tests: `tests/test_bridge.py`.

## Invocation

```
<python> -I -u -X utf8 -m spacescans.bridge <command> --request <dir>/request.json --result <dir>/result.json [--verbose]
```

- Use `-X utf8`, not `PYTHONUTF8=1`: `-I` ignores every `PYTHON*` variable.
- The client sets the working directory to its run directory (several readers
  fall back to the cwd) and passes absolute paths only; relative paths in
  arguments are rejected.
- `--request` may be omitted for commands without arguments. Without
  `--result`, the result is sent as a final `{"ev": "result", "result": ...}` event.

## Request

```json
{"protocol": 1, "command": "run",
 "client": {"name": "spacescans-r", "version": "0.2.7", "protocol_range": [1, 1]},
 "verbose": false,
 "args": {"config": {"name": "...", "...": "..."}, "data_dir": "/abs", "output_dir": "/abs",
          "threads": 2, "memory_limit": "4GB", "temp_directory": "/abs", "strict": false}}
```

`protocol` (default 1) must be 1; `client.protocol_range`, when present, must
include 1; `command`, when present, must match argv. Any of these failing is
a `protocol` error with exit code 2.

## Result (always one JSON object)

```json
{"protocol": 1, "ok": true, "command": "run",
 "spacescans": {"version": "0.2.7", "code_fp": "sha256:...", "python": "3.12.13",
                "executable": "/.../bin/python", "platform": "macOS-26.7.1-arm64", "pid": 123},
 "elapsed_s": 1.23, "value": {}, "warnings": [], "error": null}
```

On failure `ok` is false, `value` is null and
`error = {"kind", "message", "hint", "details", "type", "traceback"}`.
`hint` is client-neutral; clients build their own advice from `kind` and
`details`. `code_fp` hashes every installed `spacescans/**/*.py` file
(`spacescans._capabilities.code_fingerprint`), so it catches editable or stale
installs whose metadata version is wrong.

Warnings are objects with a `code`: `unknown_key` (a config key the schema
does not know; pydantic would drop it silently) and `python_warning`
(non-deprecation warnings raised while the pipeline ran).

## Exit codes

| code | meaning | result file |
|---|---|---|
| 0 | success | written, `ok` true |
| 1 | handled error | written, `ok` false |
| 2 | protocol or usage error | written when `--result` was given |
| 3 | the result file could not be written | absent |
| 130 / 143 | SIGINT / SIGTERM | written, kind `interrupted` |
| other, or killed by a signal | crash | usually absent; the client reports `crashed` |

## Error kinds

Design section 3.5 (`spacescans.bridge.protocol.ERROR_KINDS`):

| kind | when | details |
|---|---|---|
| `protocol` | bad request, protocol out of range, unknown command (exit 2) | |
| `config` | bad arguments, unknown variable, invalid pipeline config, rejected custom definition | pydantic: `errors[].loc` |
| `cohort` | missing columns, unparseable dates, missing or out-of-range coordinates, end before start | `code`, `issues[]` with 1-based `rows` and `count`; never values |
| `data_missing` | data not in the data directory (`data.status`), or a file that is not there or not readable | `path` or the data module's details |
| `data_corrupt` | checksum or size mismatch | |
| `terms_required` | a provider download needs explicit terms | |
| `missing_extra` | a pattern or reader needs an extra that is not installed | `extra`, `missing` |
| `network` / `disk_space` / `locked` | download failed after retries / not enough space / another fetch holds the lock | `report` (what was done) |
| `step_failed` | a pipeline step exited with rc != 0 | `step`, `rc`, `log_tail` (last 40 lines), `oom` (rc -9 or 137) |
| `interrupted` | SIGINT / SIGTERM (exit 130 / 143) | `signal` |
| `internal` | anything else: a bug; `traceback` shows where | |

An exception that declares its own `kind` (and usually `to_dict()`) keeps
it: the `spacescans.data` errors, `cohort.CohortError`,
`custom.CustomExposomeError`, the orchestrator's `StepFailed` and
`RenderError`. Phase A's `data` is now `data_missing` and `runtime` is
`internal`. Clients must accept kinds they do not know.

Cohort values (ids, dates, coordinates) never appear in messages, details,
tracebacks, events, logs or provenance: cohort problems are reported by row
number.

## Events (stdout, one JSON object per line)

Every event has `ev` and `t` (Unix time). stdout is reserved for events: at
start-up the bridge duplicates fd 1 for itself and points fd 1 at stderr, so
`print()` and C libraries cannot corrupt the stream.

```
{"ev":"start","command":"run","pid":123,"protocol":1}
{"ev":"step_start","step":"c4_fixture","pattern":"yearly_areal","i":0,"n":1}
{"ev":"progress","step":"c3_bg","frac":0.499,"text":"[overlap_fast] tile 7460/14938 ( 49.9%) ..."}
{"ev":"line","step":"c3_bg","text":"..."}            (only with verbose)
{"ev":"step_end","step":"c4_fixture","ok":true,"rc":0,"seconds":0.4,"maxrss_bytes":259702784}
{"ev":"done","ok":true,"exit":0}
```

Progress events are limited to 4 per second per step and carry `overall`,
the fraction of the whole command (for one-step commands it equals `frac`).
`maxrss_bytes` is the bridge process's peak RSS for `run`, the step's for
`link`, normalised to bytes (macOS reports bytes, Linux KiB).

`link` relays the orchestrator's events, each with `exp` (experiment):
`plan`, `cohort` (counts only), `step_start` (`i`, `n`, `overall`),
`cache_hit` (`key`), `spawn` (`text`, the command line), `progress`,
`step_end` (`rc`, `seconds`, `maxrss_bytes`, `overall`), `cache_write`,
`cache_warning`, `merge`, `warning` (`code`, ...), `experiment_done`; `line`
only with verbose. A data field that clashes with one of these names gets a
`data_` prefix. `data-fetch` and `data-verify` relay the data module's events
(`artifact_start`, `download` with `bytes`/`total`/`rate`, `verify`,
`extract_progress`, `artifact_end`, `retry`, ...).

`run_dir/logs.jsonl` (`link`) holds the web's log lines,
`{"ts", "level", "source", "msg"}`, so the paper's stage-timing tools parse
R runs too.

## Commands

List arguments also accept a single value (R writes a length-1 vector as a
scalar). Paths must be absolute.

| command | args | value |
|---|---|---|
| `hello` (alias `capabilities`) | — | `spacescans.capabilities()` (version, code_fp, extras, registry, native libraries, `catalog` {`hash`, `templates`, `variables`, `experiments`, `variables_sha256`, `features_sha256`}, `data_release`) plus `protocol`, `protocol_range`, `commands` |
| `catalog` | `variables`, `custom`, `data_dir` (all optional) | `{"variables": [{key, label, ..., value_cols, columns, requires, datasets, steps, templates, patterns, plugins, missing_extras, runnable_here, custom}], "experiments", "features", "catalog", "data_release"}`; with `data_dir` each row gains `status`, `usable`, `next` and the value gains `status` (`data.status`) |
| `coverage` | `cohort` (.parquet or .csv), `variables`, `custom` | `{"episodes", "patients", "variables": [{variable, coverage_years, episodes, episodes_in_time_window, episodes_in_region, episodes_covered, pct, warnings, boundary, display_unit, temporal}]}`; counts are episodes (rows) |
| `data-status` | `variables`, `datasets`, `data_dir`, `deep`, `manifest` | `data.status(...).to_dict()` |
| `data-plan` | as `data-status` plus `accept_terms`, `keep_archives`, `force`, `mirror` | `data.plan(...).to_dict()` (no network) |
| `data-verify` | as `data-status` (`deep` default true) plus `archives` | `data.verify(...).to_dict()` |
| `data-fetch` | as `data-plan` plus `dry_run` | `data.fetch(...).to_dict()`; interrupting keeps `.part` files |
| `data-import` | `dataset`, `paths`, `data_dir`, `move`, `manifest` | `data.import_files(...).to_dict()` |
| `data-terms` | `dataset`, `manifest` | `data.terms(dataset)` |
| `custom-validate` | `kind` (`polygon`, `raster`, `check`), `files`, `options` {`boundary`, `key_col`, `value_cols`, `year_col`, `name`, `description`, `labels`, `units`; raster: `years`, `bands`}; `check`: `definition`, `key`, `deep` | `{"kind", "definition", "key"}`; the definition goes back unchanged into `link`'s `custom` |
| `link` | `cohort`, `variables`, `custom`, `data_dir`, `run_dir` (default: a new 0700 directory under `tools::R_user_dir("spacescans", "cache")/runs` with an `owner.json` naming the client process, swept once that process is gone), `buffer_m` (270), `raster_res_m` (25), `cache_dir` (WeightCache root, v2 keys; none = no cache), `executor` (`subprocess`/`inprocess`), `threads`, `memory_limit`, `on_error` (`stop`/`continue`), `reserved_names`, `cancel_grace_s` (7) | `{"exposures": <run_dir/exposures.parquet>, "rows", "episodes", "patients", "columns", "features": <run_dir/feature_dictionary.csv>, "coverage", "steps", "failed", "skipped", "warnings", "provenance": <run_dir/provenance.json>, "run_dir", "seconds"}`; the dictionary describes a catalog column with the catalog's row and a custom column with its definition (label, unit, description) |
| `cache-info` | `cache_dir`, `key_format` (`v2`), `cache_root` (optional) | `{"cache_dir", "key_format", "entries", "bytes", "oldest", "newest", "temp_files", "temp_bytes", "runs"}`; `runs` is the run-directory sweep below |
| `cache-clear` | `cache_dir`, `key_format`, `older_than_s`, `cache_root` (optional) | `{"cache_dir", "key_format", "older_than_s", "removed", "kept", "bytes", "runs"}` (`WeightCache.clear`, then the sweep) |
| `runs-sweep` | `cache_root` (optional: the per-user cache root, default `tools::R_user_dir("spacescans", "cache")` as Python computes it), `dry_run` | `{"root": <cache_root>/runs, "dry_run", "removed": [dir names], "kept", "unknown": [dir names], "bytes", "errors"}`: run directories under the root whose owner process (its `owner.json`: pid, start time, host) no longer exists are deleted, never archived; a live owner is kept; a record from another host, or none, is left alone (`unknown`) |
| `schema` | — | `{"model": "DatasetConfig", "schema": <JSON schema>}` |
| `resolve` | `config` (mapping or absolute YAML path), `data_dir`, `output_dir`, `strict` | `{"config": <validated, paths resolved>, "unknown_keys", "config_source"}` |
| `run` | as `resolve`, plus `threads`, `memory_limit`, `temp_directory` | `{"output", "format", "rows", "columns", "name", "linkage_pattern", "output_grouping", "seconds", "maxrss_bytes", "engine", "config", "config_source", "unknown_keys"}` |
| `quickstart` | `output_dir`, `threads`, `memory_limit` | as `run`, plus `"demo": true` |
| `templates` | `kind` (`catalog`, default, or `init`), `name` (optional) | list: `{"set", "hash" (catalog), "templates": [{"name", "kind", "set", "file", "sha256", "config_name", "linkage_pattern"}]}`; one: `{"name", "kind", "set", "file", "sha256", "text", "config"}`. `catalog` is the 25 templates the recipes render; `init` the 5 init-config starter templates |
| `yaml-load` | `path` or `text` | `{"config", "path"}` |
| `yaml-dump` | `config`, `path` (optional), `as_config` (default true) | `{"text", "path"}`; written with `yaml.safe_dump(sort_keys=False)` |
| `ping` | `echo`, `sleep_s`, `fail_kind`, `exit_code`, `signal` | `{"pong", "echo", "slept_s"}`; diagnostics for client tests |

`link` reads the cohort, checks it with `cohort.validate` (errors are
`cohort` errors with row numbers) and passes only the five required columns
to `orchestrate.link`. Custom definitions are repaired and checked with
`custom.check_definition` first. With a `data_dir` in the request the bridge
sets `SPACESCANS_DATA_DIR` to it for itself and every step subprocess, so
`${SPACESCANS_DATA_DIR}` in a config and relative paths name the same
directory; without one the environment's value is the data root for both.

On SIGINT/SIGTERM during `link` the bridge sets the orchestrator's `cancel`
event (the executor stops the step's process group: SIGTERM, SIGKILL after
5 s), waits up to `cancel_grace_s`, kills any child process still alive,
writes the result (kind `interrupted`) and exits 130/143. Clients should
allow 10 s before killing the bridge.

`run` executes in-process (`Pipeline.from_dict`) on a worker thread; the main
thread only waits, so SIGINT/SIGTERM are honoured within about 0.1 s even
during a long C call. `run` and `resolve` wrap scalars where the schema allows
only lists (R serialises length-1 vectors as scalars).

## Versioning

Additive changes (new optional fields, events, commands, error kinds) keep
protocol 1. Removing or renaming anything bumps it.
