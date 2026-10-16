"""Bridge commands. Each takes (args, ctx) and returns (value, warnings)."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spacescans.bridge.args import bool_arg as _bool_arg
from spacescans.bridge.args import choice_arg
from spacescans.bridge.args import int_arg as _int_arg
from spacescans.bridge.args import str_arg as _str_arg
from spacescans.bridge.configs import (
    absolute_path_arg,
    coerce_scalars,
    load_raw_config,
    unknown_keys,
)
from spacescans.bridge.errors import BridgeError
from spacescans.bridge.events import EventStream, pipeline_line_handler
from spacescans.bridge.protocol import PROTOCOL, PROTOCOL_RANGE
from spacescans.bridge.worker import run_captured, warning_records

Outcome = tuple[Any, list]

__all__ = ["COMMANDS", "Context", "run_captured"]


@dataclass
class Context:
    events: EventStream
    verbose: bool = False
    warnings: list = field(default_factory=list)
    client: dict = field(default_factory=dict)

    def log(self, msg: str) -> None:
        """A line for the human log (stderr)."""
        try:
            sys.__stderr__.write(msg.rstrip("\n") + "\n")
            sys.__stderr__.flush()
        except (OSError, ValueError, AttributeError):
            pass


# --- helpers -----------------------------------------------------------------


def _maxrss_bytes() -> int | None:
    """Peak RSS of this process in bytes (ru_maxrss is bytes on macOS, KiB on Linux)."""
    try:
        import resource
    except ImportError:  # not POSIX
        return None
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(rss) if sys.platform == "darwin" else int(rss) * 1024


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
    warns += warning_records(caught)
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
    """``spacescans.capabilities()`` (version, code_fp, extras, registry, native
    libraries, ``catalog`` {hash, templates, ...}, ``data_release``) plus the protocol."""
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


def _init_template_entries() -> list[tuple[str, str, Any]]:
    """The ``init-config`` starter templates (spacescans/resources/configs)."""
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


def _templates_init(name: str | None) -> Outcome:
    import yaml
    entries = _init_template_entries()
    if name is None:
        rows = []
        for tname, kind, entry in entries:
            text = entry.read_text(encoding="utf-8")
            cfg = yaml.safe_load(text) or {}
            rows.append({"name": tname, "kind": kind, "set": "init", "file": f"{tname}.yaml",
                         "sha256": _sha256_text(text),
                         "config_name": cfg.get("name"), "linkage_pattern": cfg.get("linkage_pattern")})
        return {"set": "init", "templates": rows}, []
    for tname, kind, entry in entries:
        if tname == name or f"{tname}.yaml" == name:
            text = entry.read_text(encoding="utf-8")
            return {"name": tname, "kind": kind, "set": "init", "file": f"{tname}.yaml",
                    "sha256": _sha256_text(text), "text": text, "config": yaml.safe_load(text)}, []
    raise BridgeError("config", f"unknown init template {name!r}",
                      details={"available": [e[0] for e in entries], "set": "init"})


def _sha256_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _templates_catalog(name: str | None) -> Outcome:
    import yaml

    from spacescans import catalog
    from spacescans._capabilities import catalog_fingerprint
    hashes = catalog.template_hashes()
    if name is None:
        rows = []
        for rel in catalog.templates():
            cfg = catalog.template(rel) or {}
            rows.append({"name": rel[:-5], "kind": rel.split("/", 1)[0], "set": "catalog", "file": rel,
                         "sha256": hashes[rel], "config_name": cfg.get("name"),
                         "linkage_pattern": cfg.get("linkage_pattern")})
        return {"set": "catalog", "hash": catalog_fingerprint(hashes), "templates": rows}, []
    rel = name if name.endswith(".yaml") else f"{name}.yaml"
    if rel not in hashes:
        raise BridgeError("config", f"unknown catalog template {name!r}",
                          hint="templates with kind 'init' lists the init-config starter templates",
                          details={"available": [r[:-5] for r in catalog.templates()], "set": "catalog"})
    text = catalog.template_text(rel)
    return {"name": rel[:-5], "kind": rel.split("/", 1)[0], "set": "catalog", "file": rel,
            "sha256": hashes[rel], "text": text, "config": yaml.safe_load(text)}, []


def cmd_templates(args: dict, ctx: Context) -> Outcome:
    """The 25 catalog templates (default, with sha256 and the catalog hash) or,
    with ``kind: "init"``, the 5 init-config starter templates; ``name`` gets one."""
    which = choice_arg(args, "kind", ("catalog", "init"), default="catalog")
    name = _str_arg(args, "name")
    return _templates_catalog(name) if which == "catalog" else _templates_init(name)


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


def _register() -> None:
    from spacescans.bridge import cmd_catalog, cmd_data, cmd_datasets, cmd_link
    for table in (cmd_catalog.COMMANDS, cmd_data.COMMANDS, cmd_link.COMMANDS,
                  cmd_datasets.COMMANDS):
        COMMANDS.update(table)


_register()
