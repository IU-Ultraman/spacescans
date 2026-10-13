"""JSON-lines events on the bridge's private stdout, and pipeline output capture."""
from __future__ import annotations

import io
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, TextIO

from spacescans.bridge.protocol import dumps


def __getattr__(name: str) -> Any:
    # parse_progress lives in spacescans.orchestrate.events (phase-A mismatch #14).
    # Re-exported lazily: importing spacescans.orchestrate loads pandas, which
    # bridge commands that run no pipeline should not pay for.
    if name == "parse_progress":
        from spacescans.orchestrate.events import parse_progress
        return parse_progress
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class EventStream:
    """Writes one JSON object per line. Thread-safe; a closed pipe disables it quietly."""

    def __init__(self, stream: TextIO | None, *, verbose: bool = False,
                 progress_interval_s: float = 0.25):
        self._stream = stream
        self._lock = threading.Lock()
        self.verbose = verbose
        self._interval = progress_interval_s
        self._last_progress: dict[Any, float] = {}

    def emit(self, ev: str, **fields: Any) -> None:
        line = dumps({"ev": ev, "t": round(time.time(), 3), **fields})
        with self._lock:
            if self._stream is None:
                return
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except (OSError, ValueError):  # client went away; keep working
                self._stream = None

    def progress(self, frac: float, *, step: str | None = None, text: str | None = None,
                 force: bool = False) -> None:
        """At most one progress event per step every ``progress_interval_s`` (4/s by default)."""
        now = time.monotonic()
        last = self._last_progress.get(step)
        if not force and frac < 1.0 and last is not None and now - last < self._interval:
            return
        self._last_progress[step] = now
        self.emit("progress", step=step, frac=round(frac, 4), text=text)

    def line(self, text: str, *, step: str | None = None) -> None:
        if self.verbose:
            self.emit("line", step=step, text=text)

    def close(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.close()
                except OSError:
                    pass
                self._stream = None


class LineSink(io.TextIOBase):
    """A file-like object that hands complete lines to a callback."""

    def __init__(self, on_line: Callable[[str], Any]):
        super().__init__()
        self._on_line = on_line
        self._buf = ""
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, s: str) -> int:
        with self._lock:
            self._buf += s
            parts = self._buf.split("\n")
            self._buf = parts.pop()
        for part in parts:
            self._emit(part)
        return len(s)

    def _emit(self, line: str) -> None:
        line = line.rstrip("\r")
        if line:
            self._on_line(line)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        with self._lock:
            rest, self._buf = self._buf, ""
        if rest:
            self._emit(rest)


def pipeline_line_handler(events: EventStream, step: str | None) -> Callable[[str], None]:
    """Echo a pipeline output line to stderr and turn it into line/progress events."""
    from spacescans.orchestrate.events import parse_progress

    def handle(line: str) -> None:
        try:
            sys.__stderr__.write(line + "\n")
            sys.__stderr__.flush()
        except (OSError, ValueError, AttributeError):
            pass
        events.line(line, step=step)
        frac = parse_progress(line)
        if frac is not None:
            events.progress(frac, step=step, text=line.strip())
    return handle
