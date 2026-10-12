"""Run events, the web's log strings, progress parsing and the ``logs.jsonl`` writer.

Everything a run reports goes through one :class:`Event` type. Consumers turn the
same stream into what they need: the web adapter into ``status.json`` slot writes
and ``logs.jsonl``, the bridge into JSON-lines events for R (design section 3.6),
and :class:`LogWriter` into ``run_dir/logs.jsonl`` in the web's format, so the
paper's timing tools parse an R run exactly like a web run.

The runner-authored log strings are defined once, here, byte for byte as the web
0.2.7 runners write them (``app/experiments/bg_ndi_wi.py`` and its clones). Golden
``logs.jsonl`` files hold them; ``tests/golden/golden_compare.runner_log_lines``
compares them.

Stdlib only, so the bridge can import this without pandas.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Union

__all__ = [
    "EVENT_KINDS",
    "LOGGED_KINDS",
    "Event",
    "EventKind",
    "LOG_CACHE_CHECK_FAILED",
    "LOG_CACHE_HIT",
    "LOG_CACHE_WRITE",
    "LOG_CACHE_WRITE_FAILED",
    "LOG_COHORT_FAILED",
    "LOG_EXIT",
    "LOG_MERGE_FAILED",
    "LOG_RENDER_FAILED",
    "LOG_REUSED",
    "LOG_SIGTERM",
    "LOG_SPAWN",
    "LOGS_FILE",
    "LogWriter",
    "PROGRESS_RE",
    "parse_progress",
    "tee",
]

PathLike = Union[str, "os.PathLike[str]"]

EventKind = Literal["plan", "cohort", "step_start", "cache_hit", "spawn", "line", "progress",
                    "step_end", "cache_write", "cache_warning", "merge", "warning", "done"]
#: Every event kind, in the order a run produces them.
EVENT_KINDS: tuple[str, ...] = ("plan", "cohort", "step_start", "cache_hit", "spawn", "line",
                                "progress", "step_end", "cache_write", "cache_warning", "merge",
                                "warning", "done")

# ---------------------------------------------------------------------------
# The web runners' log strings (web 3991c3b, app/experiments/bg_ndi_wi.py)
# ---------------------------------------------------------------------------

#: Before a step's subprocess starts (bg_ndi_wi.py:231); ``cmd`` is the command joined by spaces.
LOG_SPAWN = "spawning {step}: {cmd}"
#: After it exits (bg_ndi_wi.py:253-254); level "info" when rc == 0, else "error".
LOG_EXIT = "step {step} exit code {rc}"
#: A C3 step served from the weight cache (bg_ndi_wi.py:507-508); no YAML is rendered.
LOG_CACHE_HIT = "cache hit: {key} — skipping pipeline run"
#: A C3 output copied into the weight cache (bg_ndi_wi.py:596-597); ``file`` is "<key>.parquet".
LOG_CACHE_WRITE = "cache write: {file}"
#: The cache lookup raised (bg_ndi_wi.py:524-525); level "warning", source the step.
LOG_CACHE_CHECK_FAILED = "cache check failed for {step}: {exc!r} — running fresh"
#: The cache store raised OSError (bg_ndi_wi.py:599-600); level "warning", source the step.
LOG_CACHE_WRITE_FAILED = "cache write failed: {exc!r} — continuing"
#: Rendering a step's YAML raised (bg_ndi_wi.py:531-532); level "error", source "runner".
LOG_RENDER_FAILED = "render_yaml({step}) failed: {exc!r}"
#: Converting the cohort raised (bg_ndi_wi.py:479); level "error", source "runner".
LOG_COHORT_FAILED = "csv_to_parquet failed: {exc!r}"
#: Writing the partial result raised (bg_ndi_wi.py:614); level "error", source "runner".
LOG_MERGE_FAILED = "merge_results failed: {exc!r}"
#: A C3 step already computed earlier in the same link (orchestrate.link runs an identical
#: C3 step once); level "info", source the step. Not a web string: a web runner never
#: meets the same C3 step twice.
LOG_REUSED = "reused {step}: computed earlier in this run — skipping pipeline run"
#: The runner's SIGTERM handler (bg_ndi_wi.py:371); level "info", source "runner".
LOG_SIGTERM = "received SIGTERM — task cancelled"

#: File name of the run log inside a run (task) directory.
LOGS_FILE = "logs.jsonl"

# ---------------------------------------------------------------------------
# progress lines
# ---------------------------------------------------------------------------

#: The C3 patterns' progress lines, the same regex as the web runners (bg_ndi_wi.py:184-186):
#:   ``[overlap_fast] tile 7460/14938 ( 49.9%) ...``  ``[overlap]   1600/3221 ( 49.7%)``
#:   ``[nhd_proximity] tile 5/100 ( 5.0%) elapsed=0.50m``
PROGRESS_RE = re.compile(r"\[(?:overlap|overlap_fast|nhd_proximity)\]\s+(?:tile\s+)?(\d+)/(\d+)")


def parse_progress(line: str) -> float | None:
    """Progress fraction in [0, 1] for a tile/iteration line, else None.

    Same lines as the web's ``parse_step_progress``; a count above its total
    (which the patterns never print) is clamped to 1.0 instead of passed on.
    """
    m = PROGRESS_RE.search(line)
    if not m:
        return None
    cur, total = int(m.group(1)), int(m.group(2))
    if total <= 0:
        return None
    return min(max(cur / total, 0.0), 1.0)


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    """One thing that happened in a run.

    ``text`` carries the web's log text where the web logs one (see the ``LOG_*``
    strings). ``index``/``total`` place a step in its source's plan, so
    ``(index + frac) / total`` is the web's status.json slot progress. ``data``
    holds structured extras (``rc``, ``seconds``, ``maxrss_bytes``, ``key``, ...);
    for a ``warning`` without ``text`` it holds the fields of a msg-less log record,
    as the web's ``_merge._emit_log_warning`` writes them.

    Events never carry cohort values: ids, dates and coordinates stay out of
    ``text`` and ``data``; counts and row numbers are fine.
    """

    kind: EventKind
    experiment: str
    step: str | None = None
    index: int | None = None
    total: int | None = None
    frac: float | None = None
    text: str | None = None
    data: Mapping[str, Any] | None = None
    t: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {self.kind!r}; expected one of {EVENT_KINDS}")

    @property
    def overall(self) -> float | None:
        """This event's position in its source as a fraction, when index/total are known.

        ``(index + frac) / total`` for progress (the web's slot progress), ``(index + 1) / total``
        for step_end and cache_hit, ``index / total`` for the other step events.
        """
        if self.index is None or not self.total:
            return None
        if self.kind in ("step_end", "cache_hit"):
            done = 1.0
        else:
            done = self.frac if self.frac is not None else 0.0
        return min(max((self.index + done) / self.total, 0.0), 1.0)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready dict of the fields that are set (``data`` copied as a dict)."""
        out: dict[str, Any] = {"kind": self.kind, "experiment": self.experiment}
        for name in ("step", "index", "total", "frac", "text"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.data is not None:
            out["data"] = dict(self.data)
        out["t"] = self.t
        return out


def tee(*sinks: Callable[[Event], Any] | None) -> Callable[[Event], None]:
    """One ``on_event`` callback that hands each event to every sink, in order."""
    active = tuple(s for s in sinks if s is not None)

    def emit(event: Event) -> None:
        for sink in active:
            sink(event)
    return emit


# ---------------------------------------------------------------------------
# logs.jsonl
# ---------------------------------------------------------------------------

#: Event kinds :class:`LogWriter` writes, with the web's (level, source) for each.
#: ``None`` as a source means the event's step. ``step_end`` is "error" when rc != 0.
LOGGED_KINDS: dict[str, tuple[str, str | None]] = {
    "spawn": ("info", "runner"),
    "line": ("info", None),
    "step_end": ("info", "runner"),
    "cache_hit": ("info", None),
    "cache_write": ("info", None),
    "cache_warning": ("warning", None),
    "merge": ("info", "merge"),
    "warning": ("warning", None),
}


def _iso(ts: float) -> str:
    """``datetime.now(timezone.utc).isoformat()`` for a given epoch time (the web's ``ts``)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class LogWriter:
    """Appends to ``run_dir/logs.jsonl`` in the web runners' format.

    Each record is one ``json.dumps`` line, opened and appended per write like the
    web's ``_append_log``, with the keys ``ts`` (UTC ISO 8601), ``level``,
    ``source`` and ``msg``. A msg-less record (``record``) has a float ``ts`` and
    its own fields, like ``_merge._emit_log_warning``.

    Used as an ``on_event`` callback (``LogWriter`` instances are callable), it
    writes the kinds in :data:`LOGGED_KINDS` and skips plan, cohort, step_start,
    progress and done, which the web reports through status.json instead:

    ========================  =========================  ===========================
    kind                      level                      source
    ========================  =========================  ===========================
    spawn                     info                       runner
    line                      info                       the step (pipeline output)
    step_end                  info, or error if rc != 0  runner
    cache_hit, cache_write    info                       the step
    cache_warning             warning                    the step
    merge                     info                       merge
    warning                   warning                    the step, else runner
    ========================  =========================  ===========================

    ``data["level"]`` and ``data["source"]`` override the table (for example the
    upload runner's linkage report, level "warning" when under half linked). A
    ``warning`` with no ``text`` is written as a msg-less record of its ``data``.
    Thread-safe.
    """

    def __init__(self, path: PathLike):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._dir_ready = False

    @classmethod
    def for_run_dir(cls, run_dir: PathLike) -> "LogWriter":
        """The writer for ``run_dir/logs.jsonl``."""
        return cls(Path(run_dir) / LOGS_FILE)

    def _append(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(dict(record)) + "\n"
        with self._lock:
            if not self._dir_ready:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._dir_ready = True
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)

    def write(self, level: str, source: str, msg: str, *, ts: float | None = None) -> None:
        """One web-format record: ``{"ts", "level", "source", "msg"}``."""
        self._append({"ts": _iso(time.time() if ts is None else ts), "level": level,
                      "source": source, "msg": msg})

    def record(self, *, level: str = "warning", ts: float | None = None, **fields: Any) -> None:
        """A msg-less record ``{"ts": <epoch float>, "level", **fields}``
        (``_merge._emit_log_warning``)."""
        self._append({"ts": time.time() if ts is None else ts, "level": level, **fields})

    def event(self, ev: Event) -> None:
        """Write ``ev`` if its kind is logged (see the class docstring)."""
        spec = LOGGED_KINDS.get(ev.kind)
        if spec is None:
            return
        data = ev.data or {}
        if ev.text is None:
            if ev.kind == "warning" and data:
                fields = {k: v for k, v in data.items() if k not in ("level", "ts")}
                self.record(level=str(data.get("level", "warning")), ts=ev.t, **fields)
            return
        level, source = spec
        if ev.kind == "step_end" and data.get("rc", 0) != 0:
            level = "error"
        if source is None:
            source = ev.step if ev.step is not None else "runner"
        level = str(data.get("level", level))
        source = str(data.get("source", source))
        self.write(level, source, ev.text, ts=ev.t)

    __call__ = event
