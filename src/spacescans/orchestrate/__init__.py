"""Orchestration shared by the web app and the R bridge.

* :func:`link` - a cohort and variables in, exposures out: the whole run
  (``spacescans.orchestrate.link``).
* :func:`run_source` / :func:`partial` - one source's steps and its partial
  table, the web runner's loop (``spacescans.orchestrate.run``).
* :class:`RunContext`, :class:`WeightCache` (``legacy-v1`` and ``v2``),
  ``render`` / :func:`write_config` - one step's context, cache and config
  (``.context``, ``.cache``, ``.render``).
* :class:`Event`, :class:`LogWriter` and the web's ``LOG_*`` strings;
  :class:`SubprocessExecutor`, :class:`InProcessExecutor`, :func:`execute_step`;
  :class:`RunLock` (``.events``, ``.execute``, ``.locks``).
* :func:`fan_in`, :func:`merge_partial` and friends (``.merge``) and the
  pre-run coverage estimate :func:`compute_coverage` (``.coverage``).

Two names here are also submodule names: the function :func:`link` hides the
``link`` module as an attribute of this package, so import from submodules
with ``from spacescans.orchestrate.link import LinkResult`` (not
``import spacescans.orchestrate.link as m``). ``render`` is not exported for
the same reason: use ``from spacescans.orchestrate.render import render``.

Importing this package needs pandas only (the base install); the geo stack
loads inside the step subprocesses.
"""
from __future__ import annotations

from spacescans.orchestrate.cache import KEY_FORMATS, LEGACY, V2, WeightCache
from spacescans.orchestrate.context import RunContext
from spacescans.orchestrate.coverage import (
    CONUS_BBOX,
    compute_coverage,
    read_coverage_csv,
)
from spacescans.orchestrate.events import (
    EVENT_KINDS,
    LOG_CACHE_CHECK_FAILED,
    LOG_CACHE_HIT,
    LOG_CACHE_WRITE,
    LOG_CACHE_WRITE_FAILED,
    LOG_COHORT_FAILED,
    LOG_EXIT,
    LOG_MERGE_FAILED,
    LOG_RENDER_FAILED,
    LOG_REUSED,
    LOG_SIGTERM,
    LOG_SPAWN,
    Event,
    LogWriter,
    parse_progress,
    tee,
)
from spacescans.orchestrate.execute import (
    Executor,
    InProcessExecutor,
    StepRun,
    SubprocessExecutor,
    execute_step,
    make_executor,
)
from spacescans.orchestrate.link import LinkConfigError, LinkResult, link
from spacescans.orchestrate.locks import RunLock, RunLockBusy
from spacescans.orchestrate.merge import (
    KEYS,
    LOW_MATCH_EVENT,
    LOW_MATCH_PCT,
    MatchStats,
    episode_keys,
    fan_in,
    fan_in_from_dir,
    match_stats,
    merge_partial,
    partial_csv_name,
    partial_from_dir,
    select_output,
)
from spacescans.orchestrate.render import write_config
from spacescans.orchestrate.run import (
    RenderError,
    RunCancelled,
    SourceResult,
    StepFailed,
    StepRecord,
    partial,
    run_source,
)

__all__ = [
    "CONUS_BBOX",
    "EVENT_KINDS",
    "KEYS",
    "KEY_FORMATS",
    "LEGACY",
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
    "LOW_MATCH_EVENT",
    "LOW_MATCH_PCT",
    "V2",
    "Event",
    "Executor",
    "InProcessExecutor",
    "LinkConfigError",
    "LinkResult",
    "LogWriter",
    "MatchStats",
    "RenderError",
    "RunCancelled",
    "RunContext",
    "RunLock",
    "RunLockBusy",
    "SourceResult",
    "StepFailed",
    "StepRecord",
    "StepRun",
    "SubprocessExecutor",
    "WeightCache",
    "compute_coverage",
    "episode_keys",
    "execute_step",
    "fan_in",
    "fan_in_from_dir",
    "link",
    "make_executor",
    "match_stats",
    "merge_partial",
    "parse_progress",
    "partial",
    "partial_csv_name",
    "partial_from_dir",
    "read_coverage_csv",
    "run_source",
    "select_output",
    "tee",
    "write_config",
]
