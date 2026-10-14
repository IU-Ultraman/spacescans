"""Measured memory needs per source and cohort size (gate M), for the out-of-memory hint.

``memory_table.csv`` next to this module is written by ``tools/memory_gate.py report``
from the gate-M measurements (``docs/memory.md`` and ``docs/memory-<date>.csv`` in the
repository hold the same numbers in full). One row per source (experiment), measured
case and cohort size: the peak resident set size of the run in GiB, the step that
peaked, the wall time, whether the run completed (``ok``) or was killed by the kernel's
out-of-memory killer (``oom``), and the container's memory.

:func:`oom_note` is what a ``step_failed`` with exit code -9/137 adds to its hint: how
much memory the source needed at the nearest measured cohort size. Only the standard
library is used, so the bridge can call it on the error path without loading pandas.
"""
from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

__all__ = ["TABLE_FILE", "MemoryRow", "nearest", "oom_note", "table"]

#: The packaged table (``tools/memory_gate.py report`` rewrites it).
TABLE_FILE = "memory_table.csv"
_PACKAGE = "spacescans.orchestrate"
_UPLOAD_HASH = re.compile(r"_[0-9a-f]{8}$")


def _family(step: str) -> str:
    """A step name without an upload's hash: ``c3_customgrid_3940345e`` -> ``c3_customgrid``."""
    return _UPLOAD_HASH.sub("", step)


@dataclass(frozen=True)
class MemoryRow:
    """One measured (source, case, cohort size)."""

    experiment: str
    case: str
    episodes: int
    peak_gib: float
    peak_step: str
    wall_s: float | None
    outcome: str          # "ok" | "oom" | "failed"
    container_gib: float
    source: str           # what produced the number (orchestrate.link, web-0.2.7 runner)
    measured: str         # date


def _float(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=1)
def table() -> tuple[MemoryRow, ...]:
    """Every row of the packaged table (empty when the file is missing or unreadable)."""
    try:
        text = resources.files(_PACKAGE).joinpath(TABLE_FILE).read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError):
        return ()
    rows = []
    for r in csv.DictReader(text.splitlines()):
        try:
            rows.append(MemoryRow(
                experiment=r["experiment"], case=r.get("case") or r["experiment"],
                episodes=int(r["episodes"]), peak_gib=float(r["peak_gib"]),
                peak_step=r.get("peak_step") or "", wall_s=_float(r.get("wall_s", "")),
                outcome=r.get("outcome") or "ok",
                container_gib=_float(r.get("container_gib", "")) or 0.0,
                source=r.get("source") or "", measured=r.get("measured") or ""))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(rows)


def nearest(experiment: str, episodes: int | None = None) -> tuple[MemoryRow, ...]:
    """The rows of ``experiment`` at the measured cohort size nearest to ``episodes``.

    Nearest on a log scale (50,000 episodes is closer to 100,000 than to 10,000); with
    ``episodes`` unknown, the largest measured size. Empty when the source was not measured.
    """
    rows = [r for r in table() if r.experiment == experiment and r.episodes > 0]
    if not rows:
        return ()
    sizes = sorted({r.episodes for r in rows})
    if episodes is None or episodes <= 0:
        pick = sizes[-1]
    else:
        pick = min(sizes, key=lambda n: (abs(math.log(n) - math.log(episodes)), -n))
    return tuple(r for r in rows if r.episodes == pick)


def oom_note(experiment: str | None, episodes: int | None = None, *,
             step: str | None = None) -> str | None:
    """One sentence on the measured memory need of ``experiment`` near ``episodes``.

    ``None`` when nothing was measured for the source. ``step`` (the step that was
    killed) picks, among several measured cases of one source (the two upload cases of
    ``custom``), the one whose peak was in a step of the same kind (``c3_customgrid_*``,
    ``c3_bg``); it also sharpens the wording when it is not the step that peaked there.
    """
    if not experiment:
        return None
    rows = nearest(experiment, episodes)
    if not rows:
        return None
    if step:
        rows = tuple(r for r in rows if r.peak_step and _family(r.peak_step) == _family(step)) or rows
    worst = max(rows, key=lambda r: r.peak_gib)
    n = f"{worst.episodes:,}"
    where = f" in step {worst.peak_step}" if worst.peak_step else ""
    if worst.outcome == "oom":
        note = (f"measured: {experiment} exceeded a {worst.container_gib:.0f} GiB container on "
                f"{n} episodes (killed{where} at about {worst.peak_gib:.1f} GiB)")
    else:
        note = (f"measured: {experiment} peaked at about {worst.peak_gib:.1f} GiB on {n} episodes"
                f"{where}")
    if episodes and episodes != worst.episodes:
        note += f"; this cohort has {episodes:,} episodes"
    if step and worst.peak_step and _family(step) != _family(worst.peak_step):
        note += f" (the step that failed here, {step}, is not the one that peaked there)"
    return note
