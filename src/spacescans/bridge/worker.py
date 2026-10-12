"""Running a command's work on a worker thread, and stopping it cleanly.

The main thread only waits, in short joins, so SIGINT/SIGTERM reach the
bridge within about 0.1 s even while the worker sits in a long C call (GDAL,
exactextract, DuckDB) or a socket read.

``run_cancellable`` adds what ``link`` needs on an interrupt: set the
``cancel`` event (the orchestrator then stops its step process group:
SIGTERM, SIGKILL after 5 s), give the worker ``grace_s`` to finish, then kill
any child process that is still alive, so nothing outlives the bridge.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
import warnings as _warnings
from collections.abc import Callable
from typing import Any

from spacescans.bridge.events import LineSink

_QUIET_WARNINGS = (DeprecationWarning, PendingDeprecationWarning, FutureWarning)
DEFAULT_CANCEL_GRACE_S = 7.0   # the executor's SIGTERM -> SIGKILL takes 5 s


def warning_records(caught: list) -> list[dict]:
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


def _start(fn: Callable[[], Any], on_line: Callable[[str], None]) -> tuple[threading.Thread, dict, LineSink]:
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
    return worker, box, sink


def _finish(box: dict, sink: LineSink) -> tuple[Any, list]:
    sink.close()
    for w in box["warnings"]:  # keep them in the human log too
        try:
            sys.__stderr__.write(_warnings.formatwarning(w.message, w.category, w.filename, w.lineno))
        except (OSError, ValueError, AttributeError):
            pass
    if "error" in box:
        raise box["error"]
    return box.get("value"), box["warnings"]


def run_captured(fn: Callable[[], Any], on_line: Callable[[str], None]) -> tuple[Any, list]:
    """Run ``fn`` in a worker thread with stdout captured line by line.

    Returns (value, recorded warnings); re-raises the worker's exception. An
    interrupt in the main thread propagates at once (the worker is a daemon
    thread; the bridge exits with ``os._exit``).
    """
    worker, box, sink = _start(fn, on_line)
    while worker.is_alive():
        worker.join(0.1)
    return _finish(box, sink)


def run_cancellable(fn: Callable[[], Any], on_line: Callable[[str], None], *,
                    cancel: threading.Event, grace_s: float = DEFAULT_CANCEL_GRACE_S,
                    log: Callable[[str], None] | None = None) -> tuple[Any, list]:
    """``run_captured`` for work that owns child processes.

    On SIGINT/SIGTERM (KeyboardInterrupt or another BaseException in the main
    thread): ignore further signals, set ``cancel``, wait up to ``grace_s``
    for the worker, kill whatever child process is left, then re-raise.
    """
    worker, box, sink = _start(fn, on_line)
    try:
        while worker.is_alive():
            worker.join(0.1)
    except BaseException:
        with contextlib.suppress(ValueError, OSError):  # only the main thread may set handlers
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        cancel.set()
        deadline = time.monotonic() + max(0.0, grace_s)
        while worker.is_alive() and time.monotonic() < deadline:
            worker.join(0.1)
        killed = kill_descendants()
        if log is not None:
            state = "stopped" if not worker.is_alive() else "still running"
            log(f"cancelled: worker {state}; killed {len(killed)} leftover process(es)")
        raise
    return _finish(box, sink)


def _process_table() -> list[tuple[int, int, int]]:
    """(pid, ppid, pgid) of every process, from ``ps`` (macOS and Linux)."""
    try:
        p = subprocess.Popen(["ps", "-A", "-o", "pid=,ppid=,pgid="], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return []
    try:
        out, _ = p.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(x.lstrip("-").isdigit() for x in parts):
            pid, ppid, pgid = map(int, parts)
            if pid != p.pid:
                rows.append((pid, ppid, pgid))
    return rows


def descendants(root: int | None = None) -> list[tuple[int, int]]:
    """(pid, pgid) of every live descendant of ``root`` (default: this process)."""
    root = os.getpid() if root is None else root
    table = _process_table()
    children: dict[int, list[tuple[int, int]]] = {}
    for pid, ppid, pgid in table:
        children.setdefault(ppid, []).append((pid, pgid))
    out, stack = [], [root]
    while stack:
        for pid, pgid in children.get(stack.pop(), []):
            out.append((pid, pgid))
            stack.append(pid)
    return out


def kill_descendants() -> list[int]:
    """SIGKILL every descendant process. Returns the pids.

    A process group is killed as a whole only when its leader is one of our
    descendants (a step started in its own session); anything else is
    killed by pid, so no unrelated group is ever signalled.
    """
    found = descendants()
    pids = {pid for pid, _ in found}
    for pid, pgid in found:
        with contextlib.suppress(OSError):
            if pgid in pids:
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
    return sorted(pids)
