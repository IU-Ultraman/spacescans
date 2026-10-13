"""Run one rendered pipeline step, in a subprocess (the default) or in this process.

:class:`SubprocessExecutor` does what the web runners' ``run_pipeline_step`` does
(``app/experiments/bg_ndi_wi.py:208-255``): start ``<prefix> run --data-dir <data_dir>
<config>`` in its own session, stream its merged stdout/stderr line by line, wait
for it. It adds what the web lacks: cancellation and a timeout that stop the step's
whole process group (SIGTERM, then SIGKILL after 5 s), the step's peak RSS from
``os.wait4``, and ``SPACESCANS_DATA_DIR`` set to the data dir so ``${SPACESCANS_DATA_DIR}``
in a config means the same directory as ``--data-dir`` (phase-A mismatch #11).

:class:`InProcessExecutor` runs the same config through ``Pipeline.from_dict`` (U1)
with optional DuckDB threads / memory_limit / temp_directory (U4), for tests and
small jobs.

:func:`execute_step` runs a step through any executor and reports it as events with
the web's log text (``spawning ...``, the output lines, ``step ... exit code ...``);
``run_source`` (orch-link) calls it for every step that is not a cache hit.

Stdlib only at import time (PyYAML and the pipeline load when a step runs in-process).
POSIX first: process groups and ``wait4`` are POSIX; elsewhere the subprocess
executor stops only the direct child and reports no peak RSS.
"""
from __future__ import annotations

import contextlib
import io
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, Union, runtime_checkable

from spacescans.orchestrate.events import LOG_EXIT, LOG_SPAWN, Event, parse_progress

__all__ = [
    "DATA_DIR_ENV",
    "KILL_GRACE_S",
    "TAIL_LINES",
    "Executor",
    "InProcessExecutor",
    "StepRun",
    "SubprocessExecutor",
    "command_line",
    "default_prefix",
    "execute_step",
    "make_executor",
]

PathLike = Union[str, "os.PathLike[str]"]

#: Seconds between SIGTERM and SIGKILL when a step is cancelled or times out.
KILL_GRACE_S = 5.0
#: Output lines kept in :attr:`StepRun.tail`.
TAIL_LINES = 50
#: After a step exits, how long its output may take to end before leftover
#: processes in its group are killed.
DRAIN_S = 2.0
#: The variable set to the data dir for every step (#11).
DATA_DIR_ENV = "SPACESCANS_DATA_DIR"

_POSIX = os.name == "posix"
_EOF = object()


def default_prefix() -> tuple[str, ...]:
    """``[sys.executable, "-I", "-u", "-m", "spacescans"]``: this interpreter, isolated
    from PYTHON* variables and the working directory, unbuffered (U5)."""
    return (sys.executable, "-I", "-u", "-m", "spacescans")


@dataclass
class StepRun:
    """What one step execution returned.

    ``rc`` is the exit code, negative for a signal (-15 SIGTERM, -9 SIGKILL) as in
    ``subprocess``. ``seconds`` is wall time. ``maxrss_bytes`` is the step's peak
    resident set size (the subprocess's, from ``wait4``; in-process, this process's
    peak so far), None where unknown. On Linux the subprocess's figure also counts what
    it shared with this process before exec, so it is at least this process's resident
    size when the step started. ``tail`` holds the last output lines (raw
    pipeline output: hand it on as a log tail, not as a user message). ``cancelled``
    and ``timed_out`` say the executor stopped the step.
    """

    rc: int
    seconds: float
    maxrss_bytes: int | None
    tail: list[str]
    cancelled: bool = False
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.cancelled and not self.timed_out


@runtime_checkable
class Executor(Protocol):
    """Runs one rendered config. ``on_line`` gets every non-empty output line, on the
    calling thread; ``cancel`` set stops the step. Process failures are a non-zero
    ``rc``, not an exception."""

    def run(self, config: Path, *, data_dir: Path, cwd: Path,
            on_line: Callable[[str], None], cancel: threading.Event | None) -> StepRun: ...


def command_line(executor: Any, config: PathLike, data_dir: PathLike) -> str:
    """The text after ``spawning <step>: `` in the log: the executor's command joined by
    spaces, as the web writes ``' '.join(cmd)``."""
    command = getattr(executor, "command", None)
    if callable(command):
        return " ".join(command(config, data_dir))
    return f"{type(executor).__name__} run --data-dir {os.fspath(data_dir)} {os.fspath(config)}"


def _maxrss_to_bytes(ru_maxrss: int) -> int:
    """``ru_maxrss`` is bytes on macOS and KiB on Linux and the BSDs."""
    return int(ru_maxrss) if sys.platform == "darwin" else int(ru_maxrss) * 1024


#: At most this many output lines are handed on between two cancel/exit checks.
_BATCH = 500


def _drain(lines: queue.SimpleQueue, tail: deque, on_line: Callable[[str], None]) -> bool:
    """Hand on queued output lines (waiting up to 50 ms for the first); True at EOF.

    Like the web: the trailing newline is stripped and empty lines are dropped.
    """
    try:
        item = lines.get(timeout=0.05)
    except queue.Empty:
        return False
    handed = 0
    while True:
        if item is _EOF:
            return True
        line = item.rstrip("\n")
        if line:
            tail.append(line)
            on_line(line)
        handed += 1
        if handed >= _BATCH:
            return False
        try:
            item = lines.get_nowait()
        except queue.Empty:
            return False


class _Clock:
    """Cancel / timeout bookkeeping shared by both executors."""

    def __init__(self, timeout_s: float | None, cancel: threading.Event | None):
        self.t0 = time.monotonic()
        self.deadline = None if timeout_s is None else self.t0 + timeout_s
        self.cancel = cancel
        self.reason: Literal["cancel", "timeout"] | None = None
        self.stopped_at: float | None = None

    def check(self) -> bool:
        """True the first time a stop is due."""
        if self.reason is not None:
            return False
        if self.cancel is not None and self.cancel.is_set():
            self.reason = "cancel"
        elif self.deadline is not None and time.monotonic() >= self.deadline:
            self.reason = "timeout"
        else:
            return False
        self.stopped_at = time.monotonic()
        return True

    def elapsed(self) -> float:
        return time.monotonic() - self.t0


# ---------------------------------------------------------------------------
# subprocess
# ---------------------------------------------------------------------------

class SubprocessExecutor:
    """Runs ``cmd_prefix + ["run", "--data-dir", data_dir, config]`` as a subprocess.

    * ``cmd_prefix``: default :func:`default_prefix`. The web passes
      ``[PIPELINE_PYTHON, PIPELINE_CLI]``, so its ``spawning`` log line stays byte-identical.
    * ``env``: changes on top of ``os.environ`` (a None value removes a variable);
      ``SPACESCANS_DATA_DIR`` is then set to the step's data dir in every case (#11).
    * ``timeout_s``: wall-clock limit per step; None (the default, as in the web) means none.

    The step runs in a new session (``start_new_session=True``, as in the web), so it
    and everything it starts form one process group. stdin is /dev/null; stderr is
    merged into stdout and decoded as UTF-8 with universal newlines, like the web's
    ``text=True``. On cancel or timeout the group gets SIGTERM, and SIGKILL once the
    step has exited or ``kill_grace_s`` (5 s) has passed, whichever is first. If the
    calling thread gets an exception (``on_line`` raised, KeyboardInterrupt, a
    SIGTERM handler's SystemExit), the group is killed before it propagates. After a
    normal exit, whatever the step left running in its group is killed: as soon as
    its output has ended, or after ``DRAIN_S`` seconds when a leftover keeps the
    output open. Nothing of the step outlives ``run``.
    """

    def __init__(self, cmd_prefix: Sequence[PathLike] | None = None,
                 env: Mapping[str, str | None] | None = None,
                 timeout_s: float | None = None, *,
                 kill_grace_s: float = KILL_GRACE_S, tail_lines: int = TAIL_LINES):
        prefix = default_prefix() if cmd_prefix is None else tuple(os.fspath(p) for p in cmd_prefix)
        if not prefix:
            raise ValueError("cmd_prefix must name a program")
        if timeout_s is not None and not timeout_s > 0:
            raise ValueError(f"timeout_s must be positive or None, got {timeout_s!r}")
        if not kill_grace_s >= 0:
            raise ValueError(f"kill_grace_s must be >= 0, got {kill_grace_s!r}")
        self.cmd_prefix: tuple[str, ...] = prefix
        self.env: dict[str, str | None] = dict(env or {})
        self.timeout_s = timeout_s
        self.kill_grace_s = float(kill_grace_s)
        self.tail_lines = int(tail_lines)

    def __repr__(self) -> str:
        return (f"SubprocessExecutor(cmd_prefix={list(self.cmd_prefix)!r}, "
                f"timeout_s={self.timeout_s!r})")

    def command(self, config: PathLike, data_dir: PathLike) -> list[str]:
        """The argv for one step."""
        return [*self.cmd_prefix, "run", "--data-dir", os.fspath(data_dir), os.fspath(config)]

    def child_env(self, data_dir: PathLike) -> dict[str, str]:
        """The step's environment: ``os.environ`` + ``env`` + ``SPACESCANS_DATA_DIR``."""
        out = dict(os.environ)
        for key, value in self.env.items():
            if value is None:
                out.pop(key, None)
            else:
                out[key] = str(value)
        out[DATA_DIR_ENV] = os.fspath(data_dir)
        return out

    # -- process-group helpers --------------------------------------------

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        """Signal the step's whole process group (POSIX) or the child alone."""
        if _POSIX:
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass  # group already empty (macOS may say EPERM for a lone zombie)
        else:  # pragma: no cover - Windows
            with contextlib.suppress(OSError):
                proc.kill() if sig == getattr(signal, "SIGKILL", None) else proc.terminate()

    @staticmethod
    def _reap(proc: subprocess.Popen, *, block: bool) -> tuple[int | None, int | None]:
        """(rc, maxrss_bytes) once the child has exited, else (None, None)."""
        if not _POSIX:  # pragma: no cover - Windows
            rc = proc.wait() if block else proc.poll()
            return rc, None
        try:
            pid, status, usage = os.wait4(proc.pid, 0 if block else os.WNOHANG)
        except ChildProcessError:  # reaped elsewhere; Popen knows the code
            return (proc.returncode if proc.returncode is not None else -1), None
        if pid == 0:
            return None, None
        rc = os.waitstatus_to_exitcode(status)
        proc.returncode = rc  # tell Popen, so it never waits on a reused pid
        return rc, _maxrss_to_bytes(usage.ru_maxrss)

    def run(self, config: PathLike, *, data_dir: PathLike, cwd: PathLike,
            on_line: Callable[[str], None], cancel: threading.Event | None = None) -> StepRun:
        cmd = self.command(config, data_dir)
        tail: deque[str] = deque(maxlen=self.tail_lines)
        clock = _Clock(self.timeout_s, cancel)
        if clock.check():  # stopped before it started: start nothing
            return StepRun(rc=-int(signal.SIGTERM), seconds=0.0, maxrss_bytes=None, tail=[],
                           cancelled=clock.reason == "cancel",
                           timed_out=clock.reason == "timeout")
        try:
            proc = subprocess.Popen(
                cmd, cwd=os.fspath(cwd), env=self.child_env(data_dir),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", start_new_session=_POSIX,
            )
        except OSError as exc:
            # Shell conventions: 127 not found, 126 found but not runnable.
            msg = f"could not start {cmd[0]}: {exc}"
            tail.append(msg)
            on_line(msg)
            return StepRun(rc=127 if isinstance(exc, FileNotFoundError) else 126,
                           seconds=clock.elapsed(), maxrss_bytes=None, tail=list(tail))

        lines: queue.SimpleQueue = queue.SimpleQueue()

        def pump() -> None:
            try:
                for raw in proc.stdout:  # type: ignore[union-attr]
                    lines.put(raw)
            except (OSError, ValueError):
                pass
            finally:
                lines.put(_EOF)

        reader = threading.Thread(target=pump, name=f"spacescans-step-{proc.pid}", daemon=True)
        reader.start()

        rc: int | None = None
        maxrss: int | None = None
        exited_at: float | None = None
        killed = False
        eof = False
        try:
            while True:
                eof = _drain(lines, tail, on_line) or eof

                if rc is None:
                    rc, maxrss = self._reap(proc, block=False)
                    if rc is not None:
                        exited_at = time.monotonic()
                if rc is None and clock.check():
                    self._signal(proc, signal.SIGTERM)
                if clock.reason is not None and not killed:
                    # The step is gone (kill what it left) or the grace period is over.
                    if rc is not None or time.monotonic() - clock.stopped_at >= self.kill_grace_s:
                        self._signal(proc, signal.SIGKILL)
                        killed = True
                if rc is not None:
                    if eof:
                        break
                    waited = time.monotonic() - exited_at
                    if waited >= DRAIN_S and not killed:
                        self._signal(proc, signal.SIGKILL)  # leftovers hold the pipe open
                        killed = True
                    if waited >= DRAIN_S + KILL_GRACE_S:
                        break  # a process outside the group holds the pipe; stop reading
            if not killed:
                self._signal(proc, signal.SIGKILL)  # anything the step left in its group
        except BaseException:
            self._signal(proc, signal.SIGKILL)
            if rc is None:
                with contextlib.suppress(Exception):
                    self._reap(proc, block=True)
            raise
        finally:
            reader.join(timeout=1.0)
            if not reader.is_alive():  # never close a pipe another thread is reading
                with contextlib.suppress(Exception):
                    proc.stdout.close()  # type: ignore[union-attr]

        return StepRun(rc=rc, seconds=clock.elapsed(), maxrss_bytes=maxrss, tail=list(tail),
                       cancelled=clock.reason == "cancel", timed_out=clock.reason == "timeout")


# ---------------------------------------------------------------------------
# in-process
# ---------------------------------------------------------------------------

class _ThreadLines(io.TextIOBase):
    """A stdout stand-in that turns one thread's writes into lines.

    ``redirect_stdout`` is process-wide; writes from any other thread (for
    example an ``on_line`` callback that prints) go to the original stream, so
    they are neither captured nor fed back.
    """

    def __init__(self, owner: int, put: Callable[[str], None], passthrough: Any):
        super().__init__()
        self._owner = owner
        self._put = put
        self._passthrough = passthrough
        self._buf = ""

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, s: str) -> int:
        if threading.get_ident() != self._owner:
            if self._passthrough is not None:
                return self._passthrough.write(s)
            return len(s)
        self._buf += s
        *done, self._buf = re.split(r"\r\n|\r|\n", self._buf)
        for line in done:
            self._put(line)
        return len(s)

    def flush(self) -> None:
        if threading.get_ident() != self._owner and self._passthrough is not None:
            with contextlib.suppress(Exception):
                self._passthrough.flush()

    def close(self) -> None:
        if self._buf:
            rest, self._buf = self._buf, ""
            self._put(rest)


_DATA_DIR_VAR = "${" + DATA_DIR_ENV + "}"


def _substitute(obj: Any, old: str, new: str) -> Any:
    if isinstance(obj, str):
        return obj.replace(old, new)
    if isinstance(obj, dict):
        return {k: _substitute(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute(v, old, new) for v in obj]
    return obj


class InProcessExecutor:
    """Runs a step in this process with ``Pipeline.from_dict`` (U1).

    ``threads``, ``memory_limit`` and ``temp_directory`` override the config's
    ``engine`` block (U4); None leaves it as rendered. The step behaves like
    ``spacescans run``: ``${SPACESCANS_DATA_DIR}`` means ``data_dir`` (#11), a
    relative output path lands under ``cwd/output``, the output path is printed as
    ``Output: <path>`` and an exception becomes rc 1 with an ``Error: <message>``
    line. Python-level stdout of the worker thread is captured; output written by
    native libraries straight to file descriptor 1 is not.

    Cancel and timeout are best effort: the DuckDB connection of the step's engine
    is interrupted, and if the step has not returned after ``cancel_grace_s`` the
    executor returns (rc -15) and leaves the worker thread to finish on its own;
    work in GDAL or exactextract cannot be interrupted. Use
    :class:`SubprocessExecutor` when a step must be stoppable. ``maxrss_bytes`` is
    this process's peak so far, not the step's own.
    """

    def __init__(self, threads: int | None = None, memory_limit: str | None = None,
                 temp_directory: PathLike | None = None, *, timeout_s: float | None = None,
                 cancel_grace_s: float = KILL_GRACE_S, tail_lines: int = TAIL_LINES):
        if threads is not None and (not isinstance(threads, int) or isinstance(threads, bool)
                                    or threads < 1):
            raise ValueError(f"threads must be a positive integer or None, got {threads!r}")
        if timeout_s is not None and not timeout_s > 0:
            raise ValueError(f"timeout_s must be positive or None, got {timeout_s!r}")
        self.threads = threads
        self.memory_limit = memory_limit
        self.temp_directory = None if temp_directory is None else os.fspath(temp_directory)
        self.timeout_s = timeout_s
        self.cancel_grace_s = float(cancel_grace_s)
        self.tail_lines = int(tail_lines)

    def __repr__(self) -> str:
        return (f"InProcessExecutor(threads={self.threads!r}, "
                f"memory_limit={self.memory_limit!r}, temp_directory={self.temp_directory!r})")

    def command(self, config: PathLike, data_dir: PathLike) -> list[str]:
        """What the ``spawning`` log line shows for an in-process step."""
        return ["<in-process>", "run", "--data-dir", os.fspath(data_dir), os.fspath(config)]

    def engine_overrides(self) -> dict[str, Any]:
        return {k: v for k, v in (("threads", self.threads), ("memory_limit", self.memory_limit),
                                  ("temp_directory", self.temp_directory)) if v is not None}

    def prepare(self, config: PathLike, data_dir: PathLike) -> dict[str, Any]:
        """The mapping handed to ``Pipeline.from_dict``: the YAML with
        ``${SPACESCANS_DATA_DIR}`` replaced by ``data_dir`` and the engine overrides."""
        import yaml

        raw = yaml.safe_load(Path(config).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{os.fspath(config)}: a pipeline config must be a mapping")
        raw = _substitute(raw, _DATA_DIR_VAR, os.fspath(data_dir))
        overrides = self.engine_overrides()
        if overrides:
            engine = raw.get("engine")
            engine = dict(engine) if isinstance(engine, dict) else {"backend": "duckdb"}
            engine.update(overrides)
            raw["engine"] = engine
        return raw

    @staticmethod
    def _output_dir(raw: Mapping[str, Any], cwd: PathLike) -> str | None:
        """Where ``spacescans run`` started in ``cwd`` would put relative outputs."""
        if os.environ.get("SPACESCANS_OUTPUT_DIR") or raw.get("base_output_dir"):
            return None  # same resolution in both modes
        return os.fspath(Path(cwd) / "output")

    def run(self, config: PathLike, *, data_dir: PathLike, cwd: PathLike,
            on_line: Callable[[str], None], cancel: threading.Event | None = None) -> StepRun:
        tail: deque[str] = deque(maxlen=self.tail_lines)
        clock = _Clock(self.timeout_s, cancel)
        if clock.check():
            return StepRun(rc=-int(signal.SIGTERM), seconds=0.0, maxrss_bytes=None, tail=[],
                           cancelled=clock.reason == "cancel",
                           timed_out=clock.reason == "timeout")
        lines: queue.SimpleQueue = queue.SimpleQueue()
        box: dict[str, Any] = {}
        passthrough = sys.stdout

        def target() -> None:
            sink = _ThreadLines(threading.get_ident(), lines.put, passthrough)
            try:
                with contextlib.redirect_stdout(sink):
                    try:
                        from spacescans.pipeline.runner import Pipeline

                        raw = self.prepare(config, data_dir)
                        pipe = Pipeline.from_dict(raw, data_dir=os.fspath(data_dir),
                                                  output_dir=self._output_dir(raw, cwd))
                        box["pipe"] = pipe
                        result = pipe.run()
                        print(f"Output: {result}")
                        box["rc"] = 0
                    except Exception as exc:  # the CLI's `except Exception` -> rc 1
                        print(f"Error: {exc}")
                        box["rc"] = 1
            finally:
                sink.close()
                lines.put(_EOF)

        worker = threading.Thread(target=target, name="spacescans-inprocess-step", daemon=True)
        worker.start()
        abandoned = False
        try:
            while True:
                if _drain(lines, tail, on_line):
                    break
                if clock.check():
                    self._interrupt(box.get("pipe"))
                if (clock.reason is not None
                        and time.monotonic() - clock.stopped_at >= self.cancel_grace_s):
                    abandoned = True
                    break
        except BaseException:
            self._interrupt(box.get("pipe"))
            raise
        if not abandoned:
            worker.join()
        rc = -int(signal.SIGTERM) if abandoned else int(box.get("rc", 1))
        return StepRun(rc=rc, seconds=clock.elapsed(), maxrss_bytes=_self_maxrss(),
                       tail=list(tail), cancelled=clock.reason == "cancel",
                       timed_out=clock.reason == "timeout")

    @staticmethod
    def _interrupt(pipe: Any) -> None:
        conn = getattr(getattr(pipe, "engine", None), "_conn", None)
        interrupt = getattr(conn, "interrupt", None)
        if callable(interrupt):
            with contextlib.suppress(Exception):
                interrupt()


def _self_maxrss() -> int | None:
    try:
        import resource
    except ImportError:  # pragma: no cover - not POSIX
        return None
    return _maxrss_to_bytes(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def make_executor(executor: Executor | Literal["subprocess", "inprocess"] = "subprocess", *,
                  threads: int | None = None, memory_limit: str | None = None) -> Executor:
    """``link(executor=...)``: an executor instance as given, or one of the two by name.

    For "subprocess", threads and memory_limit reach the step through the rendered
    YAML's engine block (``RunContext.engine``), not through the executor.
    """
    if executor == "subprocess":
        return SubprocessExecutor()
    if executor == "inprocess":
        return InProcessExecutor(threads=threads, memory_limit=memory_limit)
    if isinstance(executor, str):
        raise ValueError(f"executor must be 'subprocess', 'inprocess' or an Executor, "
                         f"got {executor!r}")
    if not callable(getattr(executor, "run", None)):
        raise TypeError(f"{executor!r} has no run() method")
    return executor


# ---------------------------------------------------------------------------
# one step, reported as events
# ---------------------------------------------------------------------------

def execute_step(executor: Executor, config: PathLike, *, experiment: str, step: str,
                 data_dir: PathLike, cwd: PathLike,
                 on_event: Callable[[Event], Any] | None = None,
                 cancel: threading.Event | None = None,
                 index: int | None = None, total: int | None = None) -> StepRun:
    """Run one rendered step and report it the way the web runners log it.

    Emits, in order: ``spawn`` (text ``spawning <step>: <command>``, data ``cmd``);
    for every non-empty output line a ``line`` event (text = the line), followed by
    a ``progress`` event (``frac``) when :func:`parse_progress` reads one; and
    ``step_end`` (text ``step <step> exit code <rc>``, data ``rc``, ``seconds``,
    ``maxrss_bytes``, ``cancelled``, ``timed_out``). ``index``/``total`` are copied
    onto every event so consumers can compute the source's overall progress.

    It neither raises on a failed step nor checks outputs: the caller (``run_source``)
    emits ``step_start`` and the cache events, and raises ``StepFailed`` on ``rc != 0``.
    """
    emit = on_event if on_event is not None else (lambda _ev: None)
    config, data_dir, cwd = Path(config), Path(data_dir), Path(cwd)
    common = {"step": step, "index": index, "total": total}
    cmd = command_line(executor, config, data_dir)
    emit(Event("spawn", experiment, text=LOG_SPAWN.format(step=step, cmd=cmd),
               data={"cmd": cmd}, **common))

    def on_line(line: str) -> None:
        emit(Event("line", experiment, text=line, **common))
        frac = parse_progress(line)
        if frac is not None:
            emit(Event("progress", experiment, frac=frac, text=line.strip(), **common))

    run = executor.run(config, data_dir=data_dir, cwd=cwd, on_line=on_line, cancel=cancel)
    emit(Event("step_end", experiment, text=LOG_EXIT.format(step=step, rc=run.rc),
               data={"rc": run.rc, "seconds": run.seconds, "maxrss_bytes": run.maxrss_bytes,
                     "cancelled": run.cancelled, "timed_out": run.timed_out}, **common))
    return run
