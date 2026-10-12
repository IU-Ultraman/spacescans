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

`protocol`, `config` (bad arguments or pipeline config, including pydantic
validation errors with `details.errors[].loc`), `data` (an input file is
missing or unreadable; `details.path`), `missing_extra` (`details.extra`,
`details.missing`), `runtime` (the pipeline raised), `interrupted`,
`internal`. Later phases add kinds (`data_missing`, `step_failed`, ...);
clients must accept unknown kinds.

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

Progress events are limited to 4 per second per step. `maxrss_bytes` is the
bridge process's peak RSS, normalised to bytes (macOS reports bytes, Linux KiB).

## Commands

| command | args | value |
|---|---|---|
| `capabilities` (alias `hello`) | — | `spacescans.capabilities()` plus `protocol`, `protocol_range`, `commands` |
| `schema` | — | `{"model": "DatasetConfig", "schema": <JSON schema>}` |
| `resolve` | `config` (mapping or absolute YAML path), `data_dir`, `output_dir`, `strict` | `{"config": <validated, paths resolved>, "unknown_keys", "config_source"}` |
| `run` | as `resolve`, plus `threads`, `memory_limit`, `temp_directory` | `{"output", "format", "rows", "columns", "name", "linkage_pattern", "output_grouping", "seconds", "maxrss_bytes", "engine", "config", "config_source", "unknown_keys"}` |
| `quickstart` | `output_dir`, `threads`, `memory_limit` | as `run`, plus `"demo": true` |
| `templates` | `name` (optional) | list: `{"templates": [{"name", "kind", "file", "config_name", "linkage_pattern"}]}`; one: `{"name", "kind", "text", "config"}` |
| `yaml-load` | `path` or `text` | `{"config", "path"}` |
| `yaml-dump` | `config`, `path` (optional), `as_config` (default true) | `{"text", "path"}`; written with `yaml.safe_dump(sort_keys=False)` |
| `ping` | `echo`, `sleep_s`, `fail_kind`, `exit_code`, `signal` | `{"pong", "echo", "slept_s"}`; diagnostics for client tests |

`run` executes in-process (`Pipeline.from_dict`) on a worker thread; the main
thread only waits, so SIGINT/SIGTERM are honoured within about 0.1 s even
during a long C call. `run` and `resolve` wrap scalars where the schema allows
only lists (R serialises length-1 vectors as scalars).

## Versioning

Additive changes (new optional fields, events, commands, error kinds) keep
protocol 1. Removing or renaming anything bumps it.
