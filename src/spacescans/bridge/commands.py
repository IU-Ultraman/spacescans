"""Bridge commands. Each takes (args, ctx) and returns (value, warnings)."""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading
import time
import warnings as _warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spacescans.bridge.configs import (
    absolute_path_arg,
    coerce_scalars,
    load_raw_config,
    unknown_keys,
)
from spacescans.bridge.errors import BridgeError
from spacescans.bridge.events import EventStream, LineSink, pipeline_line_handler
from spacescans.bridge.protocol import PROTOCOL, PROTOCOL_RANGE

Outcome = tuple[Any, list]


@dataclass
class Context:
    events: EventStream
    verbose: bool = False
    warnings: list = field(default_factory=list)


# --- helpers -----------------------------------------------------------------

def _bool_arg(args: dict, name: str, default: bool = False) -> bool:
    value = args.get(name, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise BridgeError("config", f"argument '{name}' must be true or false")
    return value


def _int_arg(args: dict, name: str, *, minimum: int | None = None) -> int | None:
    value = args.get(name)
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BridgeError("config", f"argument '{name}' must be a whole number, got {value!r}")
    if minimum is not None and value < minimum:
        raise BridgeError("config", f"argument '{name}' must be at least {minimum}, got {value}")
    return value


def _str_arg(args: dict, name: str) -> str | None:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise BridgeError("config", f"argument '{name}' must be a non-empty string")
    return value


def _maxrss_bytes() -> int | None:
    """Peak RSS of this process in bytes (ru_maxrss is bytes on macOS, KiB on Linux)."""
    try:
        import resource
    except ImportError:  # not POSIX
        return None
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(rss) if sys.platform == "darwin" else int(rss) * 1024


_QUIET_WARNINGS = (DeprecationWarning, PendingDeprecationWarning, FutureWarning)


def _warning_records(caught: list) -> list[dict]:
    out, seen = [], set()
    for w in caught:
        if issubclass(w.category, _QUIET_WARNINGS):
            continue
        key = (w.category.__name__, str(w.message))
        if key in seen:
            continue
        seen.add(key)
        out.append({"code": "python_warning", "category": w.category.__name__,
                    "message": str(w.message), "source": f"{w.filename}:{w.lineno}"})
        if len(out) >= 50:
            break
    return out


def run_captured(fn: Callable[[], Any], on_line: Callable[[str], None]) -> tuple[Any, list]:
    """Run ``fn`` in a worker thread with stdout captured line by line.

    The main thread only waits, in short joins, so SIGINT/SIGTERM are handled
    within ~0.1 s even while the worker sits in a long C call (GDAL,
    exactextract, DuckDB). Returns (value, recorded warnings); re-raises the
    worker's exception.
    """
    sink = LineSink(on_line)
    box: dict[str, Any] = {"warnings": []}

    def target() -> None:
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("default")
            try:
                with contextlib.redirect_stdout(sink):
                    box["value"] = fn()
            except BaseException as e:
                box["error"] = e
            finally:
                box["warnings"] = list(caught)

    worker = threading.Thread(target=target, name="spacescans-bridge-worker", daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(0.1)
    sink.close()
    for w in box["warnings"]:  # keep them in the human log too
        try:
            sys.__stderr__.write(_warnings.formatwarning(w.message, w.category, w.filename, w.lineno))
        except (OSError, ValueError, AttributeError):
            pass
    if "error" in box:
        raise box["error"]
    return box.get("value"), box["warnings"]


def _output_summary(out: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"output": str(out), "format": out.suffix.lstrip(".").lower() or None,
                            "rows": None, "columns": None}
    if out.suffix.lower() == ".parquet" and out.is_file():
        import pyarrow.parquet as pq
        md = pq.read_metadata(str(out))
        info["rows"] = md.num_rows
        info["columns"] = md.schema.to_arrow_schema().names
    return info


def _execute(raw: dict, args: dict, ctx: Context, source: dict) -> Outcome:
    """Shared body of ``run`` and ``quickstart``: validate, then run one config in-process."""
    raw = coerce_scalars(raw)
    for key in ("threads", "memory_limit", "temp_directory"):
        if key == "threads":
            val = _int_arg(args, key, minimum=1)
        elif key == "temp_directory":
            val = absolute_path_arg(args, key)
        else:
            val = _str_arg(args, key)
        if val is not None:
            engine = raw.setdefault("engine", {})
            if not isinstance(engine, dict):
                raise BridgeError("config", "config field 'engine' must be a mapping")
            engine[key] = val
    unknown = unknown_keys(raw)
    if unknown and _bool_arg(args, "strict"):
        raise BridgeError("config", f"unknown config key(s): {', '.join(unknown)}",
                          details={"unknown_keys": unknown})
    warns = [{"code": "unknown_key", "key": k, "message": f"unknown config key '{k}' is ignored"}
             for k in unknown]

    from spacescans.pipeline.runner import Pipeline
    pipe = Pipeline.from_dict(raw, data_dir=absolute_path_arg(args, "data_dir"),
                              output_dir=absolute_path_arg(args, "output_dir"))
    cfg = pipe.config
    step = cfg.name
    ctx.events.emit("step_start", step=step, pattern=cfg.linkage_pattern.value, i=0, n=1)
    t0 = time.perf_counter()
    try:
        out, caught = run_captured(pipe.run, pipeline_line_handler(ctx.events, step))
    except BaseException:
        ctx.events.emit("step_end", step=step, ok=False, seconds=round(time.perf_counter() - t0, 3))
        raise
    seconds = round(time.perf_counter() - t0, 3)
    maxrss = _maxrss_bytes()
    ctx.events.progress(1.0, step=step, force=True)
    ctx.events.emit("step_end", step=step, ok=True, rc=0, seconds=seconds, maxrss_bytes=maxrss)
    warns += _warning_records(caught)
    value = {
        **_output_summary(Path(out)),
        "name": cfg.name,
        "linkage_pattern": cfg.linkage_pattern.value,
        "output_grouping": cfg.time.output_grouping if cfg.time is not None else None,
        "seconds": seconds,
        "maxrss_bytes": maxrss,
        "engine": pipe.engine.settings() if hasattr(pipe.engine, "settings") else None,
        "config": cfg.model_dump(mode="json"),
        "config_source": source,
        "unknown_keys": unknown,
    }
    return value, warns


# --- commands ----------------------------------------------------------------

def cmd_capabilities(args: dict, ctx: Context) -> Outcome:
    from spacescans._capabilities import capabilities
    caps = capabilities()
    caps["protocol"] = PROTOCOL
    caps["protocol_range"] = list(PROTOCOL_RANGE)
    caps["commands"] = sorted(COMMANDS)
    return caps, []


def cmd_schema(args: dict, ctx: Context) -> Outcome:
    from spacescans.models.config import DatasetConfig
    return {"model": "DatasetConfig", "schema": DatasetConfig.model_json_schema()}, []


def cmd_resolve(args: dict, ctx: Context) -> Outcome:
    if "config" not in args:
        raise BridgeError("config", "argument 'config' is required")
    raw, source = load_raw_config(args["config"])
    raw = coerce_scalars(raw)
    unknown = unknown_keys(raw)
    if unknown and _bool_arg(args, "strict"):
        raise BridgeError("config", f"unknown config key(s): {', '.join(unknown)}",
                          details={"unknown_keys": unknown})
    from spacescans.pipeline.loader import load_config_dict
    model = load_config_dict(raw, data_dir=absolute_path_arg(args, "data_dir"),
                             output_dir=absolute_path_arg(args, "output_dir"))
    warns = [{"code": "unknown_key", "key": k, "message": f"unknown config key '{k}' is ignored"}
             for k in unknown]
    return {"config": model.model_dump(mode="json"), "unknown_keys": unknown,
            "config_source": source}, warns


def cmd_run(args: dict, ctx: Context) -> Outcome:
    if "config" not in args:
        raise BridgeError("config", "argument 'config' is required")
    raw, source = load_raw_config(args["config"])
    return _execute(raw, args, ctx, source)


def _substitute(obj: Any, mapping: dict[str, str]) -> Any:
    if isinstance(obj, str):
        for k, v in mapping.items():
            obj = obj.replace("${" + k + "}", v)
        return obj
    if isinstance(obj, dict):
        return {k: _substitute(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute(v, mapping) for v in obj]
    return obj


def cmd_quickstart(args: dict, ctx: Context) -> Outcome:
    """Run the bundled C3 demo (sample counties and patients) without touching os.environ."""
    from importlib import resources

    import yaml

    output_dir = absolute_path_arg(args, "output_dir") or str(Path.cwd() / "quickstart-out")
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    data_root = resources.files("spacescans.resources.data")
    qs_yaml = resources.files("spacescans.resources.configs.quickstart") / "end_to_end.yaml"
    with tempfile.TemporaryDirectory(prefix="spacescans-quickstart-") as tmp:
        sample = Path(tmp) / "sample"
        sample.mkdir()
        for entry in data_root.iterdir():
            if entry.is_file() and not entry.name.startswith("__"):
                (sample / entry.name).write_bytes(entry.read_bytes())
        raw = _substitute(yaml.safe_load(qs_yaml.read_text(encoding="utf-8")), {
            "SPACESCANS_QUICKSTART_DATA_DIR": str(sample),
            "SPACESCANS_OUTPUT_DIR": output_dir,
        })
        run_args = {k: v for k, v in args.items() if k in ("threads", "memory_limit", "temp_directory")}
        value, warns = _execute(raw, {**run_args, "output_dir": output_dir}, ctx,
                                {"source": "quickstart", "path": "quickstart/end_to_end.yaml"})
    value["demo"] = True
    return value, warns


_TEMPLATE_DIRS = (("c3", "templates/c3"), ("c4", "templates/c4"), ("quickstart", "quickstart"))


def _template_entries() -> list[tuple[str, str, Any]]:
    from importlib import resources
    root = resources.files("spacescans.resources.configs")
    out = []
    for kind, sub in _TEMPLATE_DIRS:
        d = root.joinpath(*sub.split("/"))
        if not d.is_dir():
            continue
        for entry in sorted(d.iterdir(), key=lambda e: e.name):
            if entry.name.endswith(".yaml"):
                out.append((f"{kind}/{entry.name[:-5]}", kind, entry))
    return out


def cmd_templates(args: dict, ctx: Context) -> Outcome:
    import yaml
    name = _str_arg(args, "name")
    entries = _template_entries()
    if name is None:
        rows = []
        for tname, kind, entry in entries:
            cfg = yaml.safe_load(entry.read_text(encoding="utf-8")) or {}
            rows.append({"name": tname, "kind": kind, "file": f"{tname}.yaml",
                         "config_name": cfg.get("name"), "linkage_pattern": cfg.get("linkage_pattern")})
        return {"templates": rows}, []
    for tname, kind, entry in entries:
        if tname == name:
            text = entry.read_text(encoding="utf-8")
            return {"name": tname, "kind": kind, "text": text, "config": yaml.safe_load(text)}, []
    raise BridgeError("config", f"unknown template {name!r}",
                      details={"available": [e[0] for e in entries]})


def cmd_yaml_load(args: dict, ctx: Context) -> Outcome:
    import yaml
    if args.get("text") is not None:
        text = args["text"]
        if not isinstance(text, str):
            raise BridgeError("config", "argument 'text' must be a string")
        try:
            return {"config": yaml.safe_load(text), "path": None}, []
        except yaml.YAMLError as e:
            raise BridgeError("config", f"cannot parse YAML: {e}") from e
    path = absolute_path_arg(args, "path", required=True)
    if not Path(path).is_file():
        raise BridgeError("config", f"YAML file not found: {path}", details={"path": path})
    try:
        return {"config": yaml.safe_load(Path(path).read_text(encoding="utf-8")), "path": path}, []
    except yaml.YAMLError as e:
        raise BridgeError("config", f"cannot parse {path}: {e}", details={"path": path}) from e


def cmd_yaml_dump(args: dict, ctx: Context) -> Outcome:
    """Serialise like the web renderer does: yaml.safe_dump(sort_keys=False)."""
    import yaml
    if "config" not in args:
        raise BridgeError("config", "argument 'config' is required")
    obj = args["config"]
    if isinstance(obj, dict) and "linkage_pattern" in obj and _bool_arg(args, "as_config", True):
        obj = coerce_scalars(obj)
    text = yaml.safe_dump(obj, sort_keys=False)
    path = absolute_path_arg(args, "path")
    if path is not None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, target)
    return {"text": text, "path": path}, []


def cmd_ping(args: dict, ctx: Context) -> Outcome:
    """Diagnostics for client tests: echo, delay with progress, stray output, failures.

    args: echo (any), sleep_s (seconds, <= 3600), fail_kind (raise that error
    kind), exit_code (exit at once without a result), signal (kill this process
    with that signal number).
    """
    print("ping: a stray print() goes to stderr, not the event stream")
    os.write(1, b"ping: so does a raw write to fd 1\n")
    if args.get("signal") is not None:
        os.kill(os.getpid(), _int_arg(args, "signal", minimum=1))
        time.sleep(5)
    if args.get("exit_code") is not None:
        os._exit(_int_arg(args, "exit_code", minimum=0))
    if args.get("fail_kind") is not None:
        kind = _str_arg(args, "fail_kind")
        raise BridgeError(kind, f"ping was asked to fail with kind {kind!r}",
                          hint="this is a test failure", details={"requested": kind})
    sleep_s = float(args.get("sleep_s") or 0)
    if not 0 <= sleep_s <= 3600:
        raise BridgeError("config", "argument 'sleep_s' must be between 0 and 3600")
    t0 = time.monotonic()
    while (elapsed := time.monotonic() - t0) < sleep_s:
        ctx.events.progress(elapsed / sleep_s, step="ping", text=f"ping {elapsed:.1f}/{sleep_s:.1f}s")
        time.sleep(min(0.05, sleep_s - elapsed))
    if sleep_s:
        ctx.events.progress(1.0, step="ping", force=True)
    return {"pong": True, "echo": args.get("echo"), "slept_s": round(time.monotonic() - t0, 3)}, []


COMMANDS: dict[str, Callable[[dict, Context], Outcome]] = {
    "capabilities": cmd_capabilities,
    "hello": cmd_capabilities,
    "schema": cmd_schema,
    "resolve": cmd_resolve,
    "run": cmd_run,
    "quickstart": cmd_quickstart,
    "templates": cmd_templates,
    "yaml-load": cmd_yaml_load,
    "yaml-dump": cmd_yaml_dump,
    "ping": cmd_ping,
}
