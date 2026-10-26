"""Running a command's work on a worker thread, and stopping it cleanly.

The main thread only polls, every 0.05 s, so SIGINT/SIGTERM reach the bridge
within about 0.05 s even while the worker sits in a long C call (GDAL,
exactextract, DuckDB) or a socket read. While it waits, what the SIGINT/SIGTERM
handlers raise (KeyboardInterrupt, the bridge's ``_Terminated``) is kept and
raised by ``run_captured``/``run_cancellable`` themselves, never wherever the
main thread happens to be: from CPython 3.13 on, one that lands inside
``threading.Event.wait`` can leave the event's lock held, so that the worker's
``done.set()`` blocks for ever, and on 3.13 one can skip the ``except`` meant
to catch it.

``run_cancellable`` adds what ``link`` needs on an interrupt: set the
``cancel`` event (the orchestrator then stops its step process group:
SIGTERM, SIGKILL after 5 s), give the worker ``grace_s`` to finish, then kill
any child process that is still alive, so nothing outlives the bridge.
"""
from __future__ import annotations

import contextlib
import functools
import os
import signal
import subprocess
import sys
import threading
import time
import warnings as _warnings
from collections.abc import Callable, Iterator
from typing import Any

from spacescans.bridge.events import LineSink

_QUIET_WARNINGS = (DeprecationWarning, PendingDeprecationWarning, FutureWarning)
DEFAULT_CANCEL_GRACE_S = 7.0   # the executor's SIGTERM -> SIGKILL takes 5 s
_POLL_S = 0.05


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


def _keep(handler: Callable, kept: list[BaseException], signum: int, frame: Any) -> None:
    try:
        handler(signum, frame)
    except BaseException as e:
        kept.append(e)


@contextlib.contextmanager
def _interrupts_kept() -> Iterator[list[BaseException]]:
    """Wrap the SIGINT/SIGTERM handlers so that what they raise goes to the yielded list.

    Only Python handlers are wrapped (SIG_IGN and SIG_DFL raise nothing), and
    only in the main thread (no handler runs in another one); a handler that
    does not raise still runs and does not stop the work. On the way out the
    handlers are put back, or, once something was kept, both signals are
    ignored, so that a second Ctrl-C cannot stop the bridge from writing its
    result.
    """
    kept: list[BaseException] = []
    previous: dict[int, Callable] = {}
    with contextlib.suppress(ValueError):  # only the main thread may set handlers
        for sig in (signal.SIGINT, signal.SIGTERM):
            handler = signal.getsignal(sig)
            if callable(handler):
                previous[sig] = handler
                signal.signal(sig, functools.partial(_keep, handler, kept))
    try:
        yield kept
    finally:
        with contextlib.suppress(ValueError, OSError):
            if kept:
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            else:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


def _wait(done: threading.Event, kept: list[BaseException]) -> None:
    while not done.is_set() and not kept:
        time.sleep(_POLL_S)


def _start(fn: Callable[[], Any], on_line: Callable[[str], None]) -> tuple[threading.Event, dict, LineSink]:
    """Start ``fn`` on a daemon thread. Returns (done, box, sink).

    Callers poll ``done.is_set()`` and never block in ``Event.wait`` or
    ``Thread.join``: in CPython 3.12 a KeyboardInterrupt that lands inside
    ``join()`` marks the thread as stopped while it is still running, and from
    3.13 on one that lands inside ``Event.wait()`` can leave the event's lock
    held, after which the worker's ``done.set()`` blocks for ever.
    """
    sink = LineSink(on_line)
    box: dict[str, Any] = {"warnings": []}
    done = threading.Event()

    def target() -> None:
        try:
            with _warnings.catch_warnings(record=True) as caught:
                _warnings.simplefilter("default")
                try:
                    with contextlib.redirect_stdout(sink):
                        box["value"] = fn()
                except BaseException as e:
                    box["error"] = e
                finally:
                    box["warnings"] = list(caught)
        finally:
            done.set()

    worker = threading.Thread(target=target, name="spacescans-bridge-worker", daemon=True)
    worker.start()
    return done, box, sink


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

    Returns (value, recorded warnings); re-raises the worker's exception. On
    SIGINT/SIGTERM it raises, within ``_POLL_S``, what their handler raised and
    leaves both signals ignored; the worker, a daemon thread, keeps running
    (the bridge exits with ``os._exit``).
    """
    with _interrupts_kept() as kept:
        done, box, sink = _start(fn, on_line)
        _wait(done, kept)
    if kept:
        raise kept[0]
    return _finish(box, sink)


def run_cancellable(fn: Callable[[], Any], on_line: Callable[[str], None], *,
                    cancel: threading.Event, grace_s: float = DEFAULT_CANCEL_GRACE_S,
                    log: Callable[[str], None] | None = None) -> tuple[Any, list]:
    """``run_captured`` for work that owns child processes.

    On SIGINT/SIGTERM (what their handler raised, or another BaseException in
    the main thread): ignore further signals, set ``cancel``, wait up to
    ``grace_s`` for the worker, kill whatever child process is left, then
    raise it.
    """
    with _interrupts_kept() as kept:
        done, box, sink = _start(fn, on_line)
        try:
            _wait(done, kept)
        except BaseException as e:  # not raised by a SIGINT/SIGTERM handler
            kept.append(e)
    if not kept:
        return _finish(box, sink)
    cancel.set()
    t0 = time.monotonic()
    deadline = t0 + max(0.0, grace_s)
    while not done.is_set() and time.monotonic() < deadline:
        time.sleep(_POLL_S)
    killed = kill_descendants()
    if log is not None:
        state = "stopped" if done.is_set() else "still running"
        log(f"cancelled: worker {state} after {time.monotonic() - t0:.1f}s; "
            f"killed {len(killed)} leftover process(es)")
    raise kept[0]


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
