"""``link``, ``cache-info`` and ``cache-clear``.

``link`` runs :func:`spacescans.orchestrate.link` (contract 5.3) on a worker
thread and writes three files into ``run_dir``:

* ``exposures.parquet`` - ``episode_id`` (0..n-1, input order) and the exposure columns;
* ``feature_dictionary.csv`` - the dictionary rows for the columns produced;
* ``provenance.json`` - versions, fingerprints, cache keys; never cohort values.

The value is ``{exposures, rows, episodes, patients, columns, features,
coverage, steps, failed, skipped, warnings, provenance, run_dir}``.

Events (design 3.6): the orchestrator's events are relayed as ``plan``,
``step_start``, ``cache_hit``, ``spawn``, ``progress`` (at most 4 per second per
step, with ``overall``, the fraction of the whole link), ``step_end``,
``cache_write``, ``cache_warning``, ``merge``, ``warning``; raw ``line`` events
only with ``verbose``. ``run_dir/logs.jsonl`` gets the web's log lines
(``ts``/``level``/``source``/``msg``) through the orchestrate ``LogWriter``.

On SIGINT/SIGTERM the bridge sets the orchestrator's ``cancel`` event (which
stops the step's process group), waits ``cancel_grace_s`` (default 7 s),
kills any child process still alive and exits 130/143 with kind
``interrupted``. C3 steps already finished stay in the cache.

``WeightCache`` (orch-core) and ``orchestrate.link`` (orch-link) are imported
lazily, so this module imports on builds that do not have them yet.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from spacescans.bridge.args import (
    choice_arg,
    int_arg,
    number_arg,
    path_arg,
    str_arg,
    str_list_arg,
)
from spacescans.bridge.errors import BridgeError
from spacescans.bridge.protocol import (
    PROTOCOL,
    to_jsonable,
    write_bytes_atomic,
    write_json_atomic,
)
from spacescans.bridge.worker import (
    DEFAULT_CANCEL_GRACE_S,
    run_cancellable,
    warning_records,
)

Outcome = tuple[Any, list]

EXPOSURES_FILE = "exposures.parquet"
FEATURES_FILE = "feature_dictionary.csv"
PROVENANCE_FILE = "provenance.json"
LOG_FILE = "logs.jsonl"
DIR_MODE = 0o700


# --- orchestrate parts that live on other phase-B branches -------------------

def link_function() -> Callable[..., Any]:
    """``spacescans.orchestrate.link`` (the function, even if ``link`` is also a module)."""
    try:
        import spacescans.orchestrate as orch
    except ImportError as e:  # pragma: no cover - orchestrate is part of the base install
        raise BridgeError("internal", f"spacescans.orchestrate cannot be imported: {e}") from e
    fn = getattr(orch, "link", None)
    if fn is not None and not callable(fn):  # the submodule, not the function
        fn = getattr(fn, "link", None)
    if fn is None or not callable(fn):
        raise BridgeError("internal", "this spacescans build has no orchestrate.link",
                          hint="install a spacescans build that includes orchestrate.link (tier 2)",
                          details={"missing": "spacescans.orchestrate.link"})
    return fn


def weight_cache(root: str | os.PathLike, key_format: str = "v2") -> Any:
    """``WeightCache(root, key_format=...)`` from ``spacescans.orchestrate.cache``."""
    cls = None
    try:
        from spacescans.orchestrate.cache import WeightCache as cls  # type: ignore[no-redef]
    except ImportError:
        try:
            from spacescans.orchestrate import WeightCache as cls  # type: ignore[no-redef]
        except ImportError:
            cls = None
    if cls is None:
        raise BridgeError("internal", "this spacescans build has no orchestrate.WeightCache",
                          hint="install a spacescans build that includes the C3 weight cache (tier 2)",
                          details={"missing": "spacescans.orchestrate.cache.WeightCache"})
    return cls(Path(root), key_format=key_format)


# --- logs.jsonl ---------------------------------------------------------------

class _FileLog:
    """The web's logs.jsonl line: ``{"ts", "level", "source", "msg"}`` (json.dumps defaults)."""

    def __init__(self, path: Path):
        self._fh = open(path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, level: str, source: str, msg: str) -> None:
        line = json.dumps({"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                           "level": level, "source": source, "msg": msg})
        with self._lock:
            if self._fh is not None:
                self._fh.write(line + "\n")
                self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


class RunLog:
    """``run_dir/logs.jsonl`` through ``spacescans.orchestrate.events.LogWriter`` when the
    build has it (``LogWriter(run_dir)`` with ``.write(level, source, msg)``), else an
    equivalent writer of the web's format."""

    def __init__(self, run_dir: Path):
        self.path = run_dir / LOG_FILE
        self._writer: Any = None
        try:
            from spacescans.orchestrate.events import LogWriter
            writer = LogWriter(run_dir)
            if callable(getattr(writer, "write", None)):
                self._writer = writer
        except Exception:
            self._writer = None
        if self._writer is None:
            self._writer = _FileLog(self.path)

    def write(self, level: str, source: str, msg: str) -> None:
        try:
            self._writer.write(level, source, msg)
        except Exception:  # logging must never break a run
            pass

    def close(self) -> None:
        close = getattr(self._writer, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


# --- event relay ----------------------------------------------------------------

def _get(ev: Any, name: str, default: Any = None) -> Any:
    if isinstance(ev, Mapping):
        return ev.get(name, default)
    return getattr(ev, name, default)


class LinkRelay:
    """orchestrate ``Event`` -> bridge event (and a logs.jsonl line).

    ``overall`` is (finished steps + the current step's fraction) / planned
    steps, never decreasing. A step counts as finished at ``step_end``,
    ``cache_hit`` or a ``reused`` status. Any relay error is swallowed: the
    run matters more than its progress display.
    """

    _PASS = ("cache_write", "cache_warning", "merge", "warning", "cohort")
    _RESERVED = ("start", "done", "result")
    # Fields the relay sets itself; an event's data keys with these names get a "data_" prefix.
    _TAKEN = frozenset({"ev", "t", "exp", "step", "text", "i", "n", "frac", "overall", "force"})

    def __init__(self, events: Any, log: RunLog | None, *, total_steps: int | None, verbose: bool):
        self.events = events
        self.log = log
        self.total = total_steps if total_steps and total_steps > 0 else None
        self.verbose = verbose
        self.finished: set[str] = set()
        self.current: dict[str, float] = {}
        self._overall = 0.0
        self.counts: dict[str, int] = {}

    def overall(self) -> float | None:
        if self.total is None:
            return None
        done = len(self.finished)
        cur = max([f for s, f in self.current.items() if s not in self.finished] or [0.0])
        value = min(1.0, (done + cur) / max(self.total, done + (1 if cur else 0), 1))
        self._overall = max(self._overall, value)
        return round(self._overall, 4)

    def _finish(self, step: str | None) -> None:
        if step:
            self.finished.add(step)
            self.current.pop(step, None)

    def _log_line(self, kind: str, step: str | None, text: str | None) -> None:
        if self.log is None or not text:
            return
        if kind == "line":
            self.log.write("info", step or "pipeline", text)
        elif kind in ("warning", "cache_warning"):
            self.log.write("warning", "runner", text)
        elif kind == "merge":
            self.log.write("info", "merge", text)
        elif kind != "progress":
            self.log.write("info", "runner", text)

    def __call__(self, ev: Any) -> None:
        try:
            self._handle(ev)
        except Exception as e:  # pragma: no cover - defensive
            try:
                sys.__stderr__.write(f"[spacescans.bridge] event relay error: {type(e).__name__}: {e}\n")
            except (OSError, ValueError, AttributeError):
                pass

    def _handle(self, ev: Any) -> None:
        kind = str(_get(ev, "kind") or "")
        exp = _get(ev, "experiment")
        step = _get(ev, "step")
        text = _get(ev, "text")
        data = {k if k not in self._TAKEN else f"data_{k}": v
                for k, v in dict(_get(ev, "data") or {}).items()}
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self._log_line(kind, step, text)
        emit = self.events.emit
        if kind == "plan":
            total = data.get("total") or data.get("total_steps")
            if isinstance(total, int) and total > 0:
                self.total = total
            emit("plan", exp=exp, **data)
        elif kind == "step_start":
            if step:
                self.current.setdefault(step, 0.0)
            emit("step_start", exp=exp, step=step, i=_get(ev, "index"), n=_get(ev, "total"),
                 overall=self.overall(), **data)
        elif kind == "cache_hit":
            self._finish(step)
            key = data.pop("key", None) or text
            emit("cache_hit", exp=exp, step=step, key=key, overall=self.overall(), **data)
        elif kind == "spawn":
            emit("spawn", exp=exp, step=step, text=text)
        elif kind == "line":
            if self.verbose:
                emit("line", exp=exp, step=step, text=text)
        elif kind == "progress":
            frac = _get(ev, "frac")
            if not isinstance(frac, (int, float)):
                return
            frac = min(max(float(frac), 0.0), 1.0)
            if step:
                self.current[step] = max(self.current.get(step, 0.0), frac)
            self.events.progress(frac, step=step, text=text, overall=self.overall(), exp=exp)
        elif kind == "step_end":
            self._finish(step)
            emit("step_end", exp=exp, step=step, overall=self.overall(), text=text, **data)
        elif kind in self._PASS:
            if data.get("status") == "reused":
                self._finish(step)
            emit(kind, exp=exp, step=step, text=text, **data)
        elif kind == "done":
            emit("experiment_done", exp=exp, text=text, **data)
        elif kind:
            emit(f"orchestrate_{kind}" if kind in self._RESERVED else kind,
                 exp=exp, step=step, text=text, **data)


def planned_steps(variables: list[str], overlay: Mapping[str, Any] | None) -> int | None:
    """Distinct steps the link will run (an identical C3 step runs once per link)."""
    try:
        from spacescans import catalog
        names: set[str] = set()
        for exp, keys in catalog.group(variables, overlay=overlay).items():
            names.update(s.name for s in catalog.plan(exp, keys, overlay=overlay))
        return len(names) or None
    except Exception:
        return None


# --- helpers ----------------------------------------------------------------------

def _make_run_dir(path: str | None) -> Path:
    if path is None:
        return Path(tempfile.mkdtemp(prefix="spacescans-link-"))  # mkdtemp: mode 0700
    p = Path(path)
    if not p.exists():
        p.mkdir(parents=True, mode=DIR_MODE)
    elif not p.is_dir():
        raise BridgeError("config", f"run_dir {path} is not a directory", details={"run_dir": path})
    return p


def _records(df: Any) -> list[dict]:
    if df is None:
        return []
    to_dict = getattr(df, "to_dict", None)
    if callable(to_dict) and hasattr(df, "columns"):
        return to_jsonable(df.to_dict(orient="records"))
    return to_jsonable(list(df))


def _warning_entry(w: Any) -> dict:
    d = dict(w) if isinstance(w, Mapping) else {"message": str(w)}
    if not d.get("message"):
        d["message"] = str(d.get("msg") or d.get("text") or d.get("code") or "warning")
    d.setdefault("code", "link_warning")
    return to_jsonable(d)


def _write_features(features: Any, columns: list[str], path: Path) -> None:
    from spacescans import catalog
    cols = getattr(features, "columns", None)
    if cols is not None and list(cols) and set(catalog.FEATURE_FIELDNAMES) <= set(cols):
        write_bytes_atomic(path, lambda tmp: features[list(catalog.FEATURE_FIELDNAMES)].to_csv(
            tmp, index=False, lineterminator="\n"))
    else:
        write_bytes_atomic(path, lambda tmp: catalog.write_feature_csv(columns, tmp))


# --- commands -----------------------------------------------------------------------

def cmd_link(args: dict, ctx) -> Outcome:
    """Link a cohort to catalog and custom variables (contract 5.3/5.4)."""
    from spacescans.bridge.cmd_catalog import custom_definitions
    from spacescans.bridge.cohorts import read_cohort

    cohort_path = path_arg(args, "cohort", required=True)
    variables = list(dict.fromkeys(str_list_arg(args, "variables", required=True, allow_empty=False)))
    custom = custom_definitions(args, files=True)
    data_dir = path_arg(args, "data_dir") or os.environ.get("SPACESCANS_DATA_DIR") or None
    if data_dir is None:
        raise BridgeError("config", "no data directory: pass data_dir or set SPACESCANS_DATA_DIR",
                          hint="the data directory is where `data-fetch` put the exposure data")
    run_dir = _make_run_dir(path_arg(args, "run_dir"))
    cache_dir = path_arg(args, "cache_dir")
    buffer_m = int_arg(args, "buffer_m", minimum=1, default=270)
    raster_res_m = int_arg(args, "raster_res_m", minimum=1, default=25)
    executor = choice_arg(args, "executor", ("subprocess", "inprocess"), default="subprocess")
    threads = int_arg(args, "threads", minimum=1)
    memory_limit = str_arg(args, "memory_limit")
    on_error = choice_arg(args, "on_error", ("stop", "continue"), default="stop")
    reserved = str_list_arg(args, "reserved_names") or []
    grace = number_arg(args, "cancel_grace_s", minimum=0, maximum=60, default=DEFAULT_CANCEL_GRACE_S)

    unknown_custom = [k for k in (custom or {}) if k not in variables]
    if unknown_custom:
        raise BridgeError("config", f"custom definitions for variables not requested: {', '.join(unknown_custom)}",
                          details={"unknown": unknown_custom})
    co = read_cohort(cohort_path)                       # CohortError -> kind "cohort"
    link = link_function()
    cache = weight_cache(cache_dir, "v2") if cache_dir is not None else None

    log = RunLog(run_dir)
    relay = LinkRelay(ctx.events, log, total_steps=planned_steps(variables, custom), verbose=ctx.verbose)
    cancel = threading.Event()
    log.write("info", "runner", f"bridge link: {len(variables)} variable(s), {co.rows} episode(s), "
                                f"{co.patients} patient(s); executor={executor}")

    def on_line(line: str) -> None:   # stdout of in-process work
        ctx.log(line)
        log.write("info", "pipeline", line)

    t0 = time.perf_counter()
    try:
        result, caught = run_cancellable(
            lambda: link(co.table, variables, data_dir=data_dir, run_dir=run_dir, buffer_m=buffer_m,
                         raster_res_m=raster_res_m, custom=custom, cache=cache, executor=executor,
                         threads=threads, memory_limit=memory_limit, on_error=on_error,
                         reserved_names=tuple(reserved), on_event=relay, cancel=cancel),
            on_line, cancel=cancel, grace_s=grace, log=ctx.log)
    except BaseException as e:
        log.write("error", "runner", f"bridge link stopped: {type(e).__name__}")
        log.close()
        raise
    seconds = round(time.perf_counter() - t0, 3)

    exposures = result.exposures
    columns = [str(c) for c in exposures.columns]
    exp_path = run_dir / EXPOSURES_FILE
    write_bytes_atomic(exp_path, lambda tmp: exposures.to_parquet(tmp, index=False))
    feat_path = run_dir / FEATURES_FILE
    _write_features(getattr(result, "features", None),
                    [c for c in columns if c not in ("episode_id", "pid")], feat_path)
    provenance = dict(to_jsonable(getattr(result, "provenance", None) or {}))
    provenance["bridge"] = {"protocol": PROTOCOL, "command": "link", "client": to_jsonable(ctx.client),
                            "executor": executor, "cache_dir": cache_dir, "seconds": seconds}
    prov_path = run_dir / PROVENANCE_FILE
    write_json_atomic(prov_path, provenance)

    link_warnings = [_warning_entry(w) for w in (getattr(result, "warnings", None) or [])]
    warnings = list(co.warnings) + link_warnings + warning_records(caught)
    failed = list(getattr(result, "failed", None) or [])
    for exp in failed:
        warnings.append({"code": "failed", "experiment": exp,
                         "message": f"{exp}: failed; its columns are missing from the result"})
    value = {
        "exposures": str(exp_path), "rows": len(exposures), "episodes": co.rows,
        "patients": co.patients, "columns": columns, "features": str(feat_path),
        "coverage": _records(getattr(result, "coverage", None)),
        "steps": to_jsonable(list(getattr(result, "steps", None) or [])),
        "failed": failed, "skipped": list(getattr(result, "skipped", None) or []),
        "warnings": warnings, "provenance": str(prov_path), "run_dir": str(run_dir),
        "seconds": seconds,
    }
    if value["rows"] != co.rows:
        warnings.append({"code": "row_count", "message": f"the result has {value['rows']} rows for "
                                                         f"{co.rows} cohort episodes"})
    log.write("info", "runner", f"bridge link done in {seconds:.1f}s: {value['rows']} rows, "
                                f"{len(columns)} columns, {len(failed)} failed")
    log.close()
    return value, warnings


def cmd_cache_info(args: dict, ctx) -> Outcome:
    """``WeightCache.info()``: entries, bytes, oldest, newest."""
    root = path_arg(args, "cache_dir", required=True)
    key_format = choice_arg(args, "key_format", ("v2", "legacy-v1"), default="v2")
    info = weight_cache(root, key_format).info()
    return {"cache_dir": root, "key_format": key_format, **to_jsonable(dict(info))}, []


def cmd_cache_clear(args: dict, ctx) -> Outcome:
    """``WeightCache.clear(older_than_s=...)``; without ``older_than_s`` everything goes."""
    root = path_arg(args, "cache_dir", required=True)
    key_format = choice_arg(args, "key_format", ("v2", "legacy-v1"), default="v2")
    older = number_arg(args, "older_than_s", minimum=0)
    res = weight_cache(root, key_format).clear(older_than_s=older)
    return {"cache_dir": root, "key_format": key_format, "older_than_s": older,
            **to_jsonable(dict(res))}, []


COMMANDS = {
    "link": cmd_link,
    "cache-info": cmd_cache_info,
    "cache-clear": cmd_cache_clear,
}
