"""Run one source's pipeline steps: the web runner's step loop as a function.

:func:`run_source` is the body of the 0.2.7 runners' ``run()`` (web 3991c3b,
``app/experiments/bg_ndi_wi.py:477-600`` and its clones, ``custom.py:595-705``)
without the task bookkeeping: for every step of ``catalog.plan(experiment, keys)``

1. emit ``step_start``;
2. a C3 step already computed earlier in the same link is ``reused`` (an
   identical C3 step runs once per link even when several sources share it);
3. a C3 step is looked up in the weight cache **before** anything is rendered;
   on a hit the cached table is copied to ``output/<step>.parquet``, no YAML is
   written and ``cache hit: <key> — skipping pipeline run`` is logged. A lookup
   that raises logs ``cache check failed ... — running fresh`` and disables the
   write-back, as the web does;
4. otherwise the step is prepared (a custom raster's value table is
   materialised from the C3 weights) and its config written to
   ``pipeline_configs/<step>.yaml``; a failure there is a :class:`RenderError`
   (``render_yaml(<step>) failed: ...``);
5. the step runs through the executor (:func:`~spacescans.orchestrate.execute.execute_step`:
   ``spawning ...``, the output lines, ``step ... exit code ...``); a non-zero
   exit code, or no output parquet, is a :class:`StepFailed`; a cancel is
   :class:`RunCancelled`;
6. a fresh C3 output is stored in the cache (``cache write: <key>.parquet``); a
   store that fails logs ``cache write failed: ... — continuing``.

Every step and every log line is reported as an :class:`~spacescans.orchestrate.events.Event`;
nothing here writes ``logs.jsonl`` itself (pass ``on_event=LogWriter.for_run_dir(...)``
or ``tee`` it with another sink).

:func:`partial` is ``_merge.write_partial``'s table for one source, read from a
run directory; the caller writes ``result_<experiment>.csv`` if it wants one.

Events, errors and records never carry cohort values (ids, dates,
coordinates): only step names, counts, exit codes, timings and cache keys.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from spacescans.orchestrate.events import (
    LOG_CACHE_CHECK_FAILED,
    LOG_CACHE_HIT,
    LOG_CACHE_WRITE,
    LOG_CACHE_WRITE_FAILED,
    LOG_RENDER_FAILED,
    LOG_REUSED,
    Event,
)

if TYPE_CHECKING:
    import pandas as pd

    from spacescans.catalog.recipes import Step
    from spacescans.orchestrate.cache import WeightCache
    from spacescans.orchestrate.context import RunContext
    from spacescans.orchestrate.execute import Executor
    from spacescans.orchestrate.merge import MatchStats

__all__ = [
    "STEP_STATUSES",
    "RenderError",
    "RunCancelled",
    "SourceResult",
    "StepFailed",
    "StepRecord",
    "partial",
    "run_source",
]

#: ``StepRecord.status`` values: run by the executor, served from the weight
#: cache, reused from an identical C3 step earlier in the same link, or the
#: step that failed (recorded on the exception's ``steps``).
STEP_STATUSES: tuple[str, ...] = ("ran", "cache_hit", "reused", "failed")


def _noop(_event: Event) -> None:
    return None


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

class StepFailed(Exception):
    """A pipeline step exited non-zero (or produced no output parquet).

    ``step``, ``rc`` and ``log_tail`` (the step's last output lines, raw:
    show them as a log tail, never inside a user message) as in contract 5.3;
    ``experiment``, ``timed_out`` and ``reason`` add context. ``kind`` is the
    bridge error class. The message holds the step and exit code only.
    """

    kind = "step_failed"

    def __init__(self, step: str, rc: int | None, log_tail: Sequence[str] = (), *,
                 experiment: str | None = None, timed_out: bool = False,
                 reason: str | None = None) -> None:
        self.step = step
        self.rc = rc
        self.log_tail = list(log_tail)
        self.experiment = experiment
        self.timed_out = timed_out
        self.reason = reason or ("timed out" if timed_out else f"exit code {rc}")
        msg = f"step {step} failed: {self.reason}"
        if rc in (-9, 137):
            msg += " (killed: often the machine ran out of memory)"
        super().__init__(msg)

    @property
    def hint(self) -> str:
        if self.rc in (-9, 137):
            return ("the step was killed, most often by the out-of-memory killer; lower "
                    "threads or memory_limit, or use a machine with more memory")
        return f"see the log tail and {self.step}'s lines in logs.jsonl"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": str(self), "hint": self.hint,
                "details": {"step": self.step, "rc": self.rc, "experiment": self.experiment,
                            "timed_out": self.timed_out, "log_tail": list(self.log_tail)},
                "type": type(self).__name__}


class RenderError(Exception):
    """A step's config could not be prepared or written (contract 5.3, kind ``config``)."""

    kind = "config"

    def __init__(self, step: str, cause: BaseException, *, experiment: str | None = None) -> None:
        self.step = step
        self.experiment = experiment
        self.cause = cause
        super().__init__(f"could not render the config of step {step}: {cause}")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": str(self),
                "hint": "check the variable's definition and the data directory",
                "details": {"step": self.step, "experiment": self.experiment,
                            "cause": type(self.cause).__name__},
                "type": type(self).__name__}


class RunCancelled(Exception):
    """The run was cancelled (``cancel`` set); the current step's process group is gone."""

    kind = "interrupted"

    def __init__(self, step: str | None = None, *, experiment: str | None = None) -> None:
        self.step = step
        self.experiment = experiment
        where = f" during step {step}" if step else ""
        super().__init__(f"cancelled{where}")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": str(self),
                "hint": "finished C3 steps stay in the cache; run again to resume",
                "details": {"step": self.step, "experiment": self.experiment},
                "type": type(self).__name__}


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

@dataclass
class StepRecord:
    """One step of a run: ``name``, ``status`` (ran | cache_hit | reused),
    ``seconds``, ``rc``, ``maxrss_bytes`` (the step's peak RSS) and ``cache_key``;
    plus ``experiment`` and ``kind`` (c3 | c4)."""

    name: str
    status: str
    seconds: float
    rc: int | None
    maxrss_bytes: int | None
    cache_key: str | None
    experiment: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceResult:
    """What :func:`run_source` did for one source (experiment)."""

    experiment: str
    variables: list[str]
    steps: list[StepRecord]
    outputs: dict[str, Path]          # variable -> the C4 parquet its merge reads
    seconds: float
    warnings: list[dict] = field(default_factory=list)


def _identity(step: Step, ctx: Any) -> tuple:
    """What makes two C3 steps the same computation within one link (the legacy key's parts)."""
    return (step.name, step.cache_tag, str(ctx.buffer_m),
            str(ctx.raster_res_m) if step.uses_raster_res else None)


# ---------------------------------------------------------------------------
# run_source
# ---------------------------------------------------------------------------

def run_source(experiment: str, keys: Sequence[str], ctx: RunContext, *,
               cache: WeightCache | None, executor: Executor,
               on_event: Callable[[Event], Any] = _noop,
               cancel: threading.Event | None = None,
               reuse: MutableMapping[tuple, StepRecord] | None = None) -> SourceResult:
    """Run the steps that link ``keys`` of one ``experiment`` (the web runner's loop).

    ``ctx.cohort_parquet`` must exist (the pipeline input). Custom variables need
    their definitions in ``ctx.overlay``. ``reuse`` is shared by the sources of
    one link: a C3 step already done there (same step, tag, buffer and
    resolution, output still present) is not run or fetched again.

    Raises :class:`StepFailed`, :class:`RenderError` or :class:`RunCancelled`;
    ``ValueError``/``KeyError`` for a selection the experiment does not run.
    """
    from spacescans import catalog
    from spacescans.orchestrate.execute import execute_step
    from spacescans.orchestrate.render import prepare, write_config

    emit = on_event if on_event is not None else _noop
    keys = list(keys)
    steps = catalog.plan(experiment, keys, overlay=ctx.overlay)
    total = len(steps)
    t_source = time.monotonic()
    emit(Event("plan", experiment, total=total,
               data={"variables": keys, "steps": [s.name for s in steps]}))
    records: list[StepRecord] = []

    try:
        for index, step in enumerate(steps):
            if cancel is not None and cancel.is_set():
                raise RunCancelled(step.name, experiment=experiment)
            common = {"step": step.name, "index": index, "total": total}
            emit(Event("step_start", experiment, **common))
            out = ctx.output_path(step.name)
            ident = _identity(step, ctx) if step.is_c3 else None

            if ident is not None and reuse is not None and ident in reuse and out.is_file():
                prior = reuse[ident]
                rec = StepRecord(step.name, "reused", 0.0, None, None, prior.cache_key,
                                 experiment, step.kind)
                records.append(rec)
                emit(Event("cache_hit", experiment, text=LOG_REUSED.format(step=step.name),
                           data={"status": "reused", "key": prior.cache_key,
                                 "from_experiment": prior.experiment}, **common))
                continue

            store = cache is not None and step.is_c3
            if store:
                t0 = time.monotonic()
                try:
                    key = cache.fetch(step, ctx, out)
                except Exception as exc:  # noqa: BLE001 - the web runs fresh on any lookup error
                    emit(Event("cache_warning", experiment,
                               text=LOG_CACHE_CHECK_FAILED.format(step=step.name, exc=exc),
                               data={"error": type(exc).__name__}, **common))
                    store = False
                else:
                    if key is not None:
                        rec = StepRecord(step.name, "cache_hit", round(time.monotonic() - t0, 3),
                                         None, None, key, experiment, step.kind)
                        records.append(rec)
                        if reuse is not None:
                            reuse[ident] = rec
                        emit(Event("cache_hit", experiment, text=LOG_CACHE_HIT.format(key=key),
                                   data={"status": "cache_hit", "key": key}, **common))
                        continue

            try:
                for record in prepare(step, ctx):
                    level = record.get("level", "info")
                    kind = "line" if level == "info" else "warning"
                    emit(Event(kind, experiment, text=record["msg"],
                               data={"level": level, "source": record.get("source", step.name)},
                               **common))
                config = write_config(step, ctx)
            except Exception as exc:  # noqa: BLE001 - the web logs every render failure the same way
                emit(Event("warning", experiment,
                           text=LOG_RENDER_FAILED.format(step=step.name, exc=exc),
                           data={"level": "error", "source": "runner", "code": "render_failed"},
                           **common))
                raise RenderError(step.name, exc, experiment=experiment) from exc

            run = execute_step(executor, config, experiment=experiment, step=step.name,
                               data_dir=ctx.data_dir, cwd=ctx.run_dir, on_event=emit,
                               cancel=cancel, index=index, total=total)
            if run.cancelled or (cancel is not None and cancel.is_set() and not run.ok):
                raise RunCancelled(step.name, experiment=experiment)
            if not run.ok or not out.is_file():
                records.append(StepRecord(step.name, "failed", round(run.seconds, 3), run.rc,
                                          run.maxrss_bytes, None, experiment, step.kind))
                raise StepFailed(step.name, run.rc, run.tail, experiment=experiment,
                                 timed_out=run.timed_out,
                                 reason=None if not run.ok else "it produced no output parquet")

            rec = StepRecord(step.name, "ran", round(run.seconds, 3), run.rc, run.maxrss_bytes,
                             None, experiment, step.kind)
            records.append(rec)
            if step.is_c3 and reuse is not None:
                reuse[ident] = rec
            if store:
                try:
                    key = cache.store(step, ctx, out, seconds=run.seconds)
                except Exception as exc:  # noqa: BLE001 - a cache failure never fails a run
                    emit(Event("cache_warning", experiment,
                               text=LOG_CACHE_WRITE_FAILED.format(exc=exc),
                               data={"error": type(exc).__name__}, **common))
                else:
                    rec.cache_key = key
                    emit(Event("cache_write", experiment,
                               text=LOG_CACHE_WRITE.format(file=cache.path(key).name),
                               data={"key": key}, **common))
    except (StepFailed, RenderError, RunCancelled) as exc:
        exc.steps = list(records)    # the steps done (and the one that failed), for the caller
        raise

    rec_of = catalog.recipe(experiment)
    outputs = {k: ctx.run_dir / "output" / rec_of.output_for(k) for k in keys}
    return SourceResult(experiment=experiment, variables=keys, steps=records, outputs=outputs,
                        seconds=round(time.monotonic() - t_source, 3))


# ---------------------------------------------------------------------------
# partial
# ---------------------------------------------------------------------------

def _value_cols(keys: Sequence[str], overlay: Mapping[str, Any] | None) -> dict[str, list[str]]:
    from spacescans import catalog

    return {k: list(catalog.variable(k, overlay=overlay).value_cols) for k in keys}


def partial(experiment: str, keys: Sequence[str], ctx: RunContext, *,
            rewrite_in_place: bool = False) -> tuple[pd.DataFrame, MatchStats]:
    """One source's partial table and match statistics (``_merge.write_partial``).

    Reads each variable's C4 output from ``ctx.run_dir/output`` (in ``keys``
    order) and the episode keys (``pid``, ``episode_id``) from
    ``ctx.cohort_parquet``. FAQSD's column renames are applied in memory;
    ``rewrite_in_place`` also writes them back into the C4 parquet, as the
    web's runner does (``faqsd.py:213-216``). The table is what the web writes
    to ``result_<experiment>.csv`` (with ``to_csv(index=False)``).
    """
    import pandas as pd

    from spacescans import catalog
    from spacescans.orchestrate import merge

    rec = catalog.recipe(experiment)
    keys = list(keys)
    out_dir = Path(ctx.run_dir) / "output"
    if rewrite_in_place and rec.renames:
        for k in keys:
            path = out_dir / rec.output_for(k)
            df = pd.read_parquet(path).rename(columns=dict(rec.renames))
            df.to_parquet(path, index=False)
    import pyarrow.parquet as pq

    names = pq.read_schema(ctx.cohort_parquet).names
    id_col = "pid" if "pid" in names else "PATID"    # a legacy web input.parquet may keep PATID
    cohort_keys = merge.episode_keys(pd.read_parquet(ctx.cohort_parquet, columns=[id_col]))
    outputs = [(k, pd.read_parquet(out_dir / rec.output_for(k))) for k in keys]
    return merge.merge_partial(cohort_keys, outputs, _value_cols(keys, ctx.overlay),
                               experiment=experiment, renames=dict(rec.renames) or None)
