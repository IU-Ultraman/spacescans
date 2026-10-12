"""JSON-lines events on the bridge's private stdout, and pipeline output capture."""
from __future__ import annotations

import io
import re
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, TextIO

from spacescans.bridge.protocol import dumps

# The C3 patterns' progress lines (same regex as spacescans-web's runners):
#   [overlap_fast] tile 7460/14938 ( 49.9%) ...   [overlap]   1600/3221 ( 49.7%)
#   [nhd_proximity] tile 5/100 ( 5.0%) elapsed=0.50m
_PROGRESS_RE = re.compile(r"\[(?:overlap|overlap_fast|nhd_proximity)\]\s+(?:tile\s+)?(\d+)/(\d+)")


def parse_progress(line: str) -> float | None:
    """Progress fraction in [0, 1] for a tile/iteration line, else None."""
    m = _PROGRESS_RE.search(line)
    if not m:
        return None
    cur, total = int(m.group(1)), int(m.group(2))
    if total <= 0:
        return None
    return min(max(cur / total, 0.0), 1.0)


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
                 force: bool = False, overall: float | None = None, **fields: Any) -> bool:
        """At most one progress event per step every ``progress_interval_s`` (4/s by default).

        ``overall`` is the whole command's fraction; a one-step command passes
        none and gets ``frac``. Returns whether an event was emitted.
        """
        now = time.monotonic()
        last = self._last_progress.get(step)
        if not force and frac < 1.0 and last is not None and now - last < self._interval:
            return False
        self._last_progress[step] = now
        self.emit("progress", step=step, frac=round(frac, 4),
                  overall=round(frac if overall is None else overall, 4), text=text, **fields)
        return True

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
