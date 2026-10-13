"""Link a cohort to catalog and custom variables: the whole run, as one call.

:func:`link` is what the web's dispatcher and runners do for one task, for R
(through the bridge) and other new callers (design section 2.3, contract 5.3):

1. **Check the request.** Arguments are validated; every custom definition goes
   through ``custom.check_definition(d, key=k, deep=True)`` (its files exist and
   still have the recorded sha256).
2. **Group** the variables by source (experiment) in catalog order, as the
   dispatcher does (``catalog.group``); within a source the request order is
   kept, as a runner called with ``--variables`` keeps it (``walkability,ndi``
   puts ``NatWalkInd`` before ``ndi``).
3. **Data readiness** (``data.status``) for the catalog variables and the
   boundary layers of custom polygon datasets: anything missing raises
   ``data.DataMissingError`` with the next command, before any step runs.
4. **Cohort**, converted once to ``run_dir/input.parquet``: the five cohort
   columns plus ``episode_id`` (the input row number), ``pid`` as text
   (``cohort.to_pipeline_parquet(pid_as_str=True, columns="required")``).
5. **Run** each source with :func:`~spacescans.orchestrate.run.run_source`
   (cache before render, as the web), sharing one record of finished C3 steps
   so an identical C3 step runs once per link. ``on_error="stop"`` (the
   dispatcher's rule) stops at the first failing source and skips the rest;
   ``"continue"`` runs them all. Completed sources only are merged.
6. **Merge** each source into its partial (``_merge.write_partial``'s table;
   the upload path's linkage report first), then **fan in** onto the cohort's
   episodes: a column whose name is taken by the cohort, by
   ``reserved_names`` or by an earlier source gets ``_<experiment>_dup``.

The run directory is laid out like a web task directory (``input.parquet``,
``pipeline_configs/<step>.yaml``, ``output/<step>.parquet``) and step configs
carry the web's name suffix ``_task_<last 8 characters of the run dir name>``,
so rendered YAML and log lines read exactly like a web run's. ``link`` writes
no ``logs.jsonl`` itself: pass ``on_event=LogWriter.for_run_dir(run_dir)`` (or
``tee`` it with your own sink) for the web's log file.

Nothing here puts cohort values (ids, dates, coordinates) into events,
warnings, errors or provenance; they hold counts, row numbers, step names,
exit codes, timings and fingerprints.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from spacescans.orchestrate.events import LOG_MERGE_FAILED, Event
from spacescans.orchestrate.run import (
    RenderError,
    RunCancelled,
    StepFailed,
    StepRecord,
    partial,
    run_source,
)

if TYPE_CHECKING:
    import pandas as pd

    from spacescans.orchestrate.cache import WeightCache
    from spacescans.orchestrate.execute import Executor

__all__ = ["COHORT_FILE", "LinkConfigError", "LinkResult", "coverage_table", "link",
           "web_name_suffix"]

#: The pipeline input inside the run directory (the web's task file name).
COHORT_FILE = "input.parquet"
_RUN_DIR_MODE = 0o700


class LinkConfigError(ValueError):
    """A ``link`` argument is unusable (kind ``config`` for the bridge)."""

    kind = "config"

    def __init__(self, message: str, *, hint: str | None = None,
                 details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "hint": self.hint,
                "details": self.details, "type": type(self).__name__}


@dataclass
class LinkResult:
    """What :func:`link` produced (contract 5.3, plus ``partials`` and ``errors``).

    ``exposures``: ``episode_id`` (0..n-1, input order) and the exposure
    columns, one row per input row. ``features``: feature-dictionary rows of
    those columns. ``coverage``: per linked variable ``variable``,
    ``experiment``, ``episodes``, ``with_value``, ``pct``, ``patients``,
    ``patients_with_value`` (``episodes``/``with_value`` count rows).
    ``steps``: every step's :class:`~spacescans.orchestrate.run.StepRecord`.
    ``failed``/``skipped``: source (experiment) keys, as the web dispatcher
    reports them. ``warnings``: dicts with ``code`` and ``message``.
    ``provenance``: versions, fingerprints, data receipts, cache keys, engine
    settings, platform; no cohort values. ``partials``: each completed
    source's partial table (what the web writes to ``result_<exp>.csv``).
    ``errors``: one dict per failed source (``kind``, ``experiment``,
    ``variables``, ``step``, ``rc``, ``message``, ``log_tail``; the tail is
    raw pipeline output, to show as a log, not inside a message).
    """

    exposures: pd.DataFrame
    features: pd.DataFrame
    coverage: pd.DataFrame
    steps: list[StepRecord]
    failed: list[str]
    skipped: list[str]
    warnings: list[dict]
    provenance: dict
    run_dir: Path
    partials: dict[str, pd.DataFrame] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Every requested source completed."""
        return not self.failed and not self.skipped


def web_name_suffix(run_dir: str | os.PathLike) -> str:
    """The web runners' config-name suffix for a task dir: ``_task_<last 8 chars>``."""
    return f"_task_{Path(run_dir).name[-8:]}"


# ---------------------------------------------------------------------------
# argument checks
# ---------------------------------------------------------------------------

def _variables(variables: Sequence[str]) -> list[str]:
    if isinstance(variables, (str, bytes)):
        raise LinkConfigError("variables must be a list of variable keys, not a single string",
                              details={"variables": "a string"})
    keys = list(dict.fromkeys(variables))
    if not keys:
        raise LinkConfigError("at least one variable must be selected")
    bad = [k for k in keys if not isinstance(k, str) or not k]
    if bad:
        raise LinkConfigError("variable keys must be non-empty strings")
    return keys


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LinkConfigError(f"{name} must be a positive whole number of metres, "
                              f"got {value!r}", details={name: repr(value)})
    return value


def _custom_overlay(custom: Mapping[str, Any] | None, keys: list[str]
                    ) -> tuple[dict[str, dict], list[dict]]:
    """Requested custom definitions, checked deeply (#15); unrequested ones are dropped."""
    if custom is None:
        return {}, []
    if not isinstance(custom, Mapping):
        raise LinkConfigError("custom must map variable keys to dataset definitions")
    from spacescans.custom import check_definition

    overlay: dict[str, dict] = {}
    warnings: list[dict] = []
    for key, definition in custom.items():
        if key not in keys:
            warnings.append({"code": "custom_unused", "variable": key,
                             "message": f"{key}: a definition was given but the variable "
                                        "was not selected; it was ignored"})
            continue
        overlay[key] = check_definition(definition, key=key, deep=True)
    return overlay, warnings


def _groups(keys: list[str], overlay: Mapping[str, dict]) -> OrderedDict[str, list[str]]:
    """Sources in catalog order, each with its variables in request order."""
    from spacescans import catalog

    try:
        grouped = catalog.group(keys, overlay=overlay or None)
    except KeyError as exc:
        raise LinkConfigError(str(exc.args[0]) if exc.args else str(exc),
                              hint="list the catalog variables with catalog(); custom "
                                   "variables need their definition in custom=",
                              details={"variables": keys}) from None
    return OrderedDict((exp, [k for k in keys if k in members])
                       for exp, members in grouped.items())


# ---------------------------------------------------------------------------
# data readiness (#17)
# ---------------------------------------------------------------------------

def _check_data(groups: Mapping[str, list[str]], overlay: Mapping[str, dict],
                data_dir: Path) -> tuple[dict[str, Any], list[dict]]:
    """``data.status`` for the catalog variables and the custom boundary layers.

    Returns a summary for the provenance and the status warnings; raises
    ``data.DataMissingError`` when anything needed is not usable.
    """
    from spacescans import data
    from spacescans.custom import is_raster

    catalog_keys = [k for exp, ks in groups.items() if exp != "custom" for k in ks]
    boundary_of = data.boundary_datasets()
    layers = sorted({boundary_of[d["boundary"]] for d in overlay.values()
                     if not is_raster(d) and d.get("boundary") in boundary_of})
    summary: dict[str, Any] = {"data_dir": str(data_dir), "data_release": None,
                               "variables": {}, "datasets": {}}
    warnings: list[dict] = []
    missing_vars: dict[str, str] = {}
    missing_ds: dict[str, str] = {}
    next_steps: list[str] = []
    reports = []
    if catalog_keys:
        reports.append(data.status(catalog_keys, data_dir=data_dir))
    if layers:
        reports.append(data.status(datasets=layers, data_dir=data_dir))
    for report in reports:
        summary["data_release"] = report.data_release
        for w in report.warnings:
            if {"code": "data_dir", "message": w} not in warnings:
                warnings.append({"code": "data_dir", "message": w})
        for v in report.variables:
            summary["variables"][v.variable] = {"status": v.status, "datasets": list(v.chosen)}
            if not v.usable:
                missing_vars[v.variable] = v.status
                if v.next:
                    next_steps.append(v.next)
        for d in report.datasets:
            summary["datasets"][d.dataset] = {"status": d.status}
            if d.dataset in report.requested_datasets and not d.usable:
                missing_ds[d.dataset] = d.status
                if d.next:
                    next_steps.append(d.next)
    if missing_vars or missing_ds:
        parts = [f"{k} ({s})" for k, s in missing_vars.items()]
        parts += [f"boundary dataset {k} ({s})" for k, s in missing_ds.items()]
        hint = "; ".join(dict.fromkeys(next_steps)) or "spacescans data status"
        raise data.DataMissingError(
            f"data not ready in {data_dir}: {', '.join(parts)}",
            hint=hint,
            details={"data_dir": str(data_dir), "variables": missing_vars,
                     "datasets": missing_ds})
    return summary, warnings


# ---------------------------------------------------------------------------
# run directory, cohort, cache, executor
# ---------------------------------------------------------------------------

def _run_dir(run_dir: str | os.PathLike | None) -> Path:
    if run_dir is None:
        return Path(tempfile.mkdtemp(prefix="spacescans-link-"))   # mode 0700
    path = Path(os.fspath(run_dir)).expanduser().absolute()
    if path.exists():
        if not path.is_dir():
            raise LinkConfigError(f"run_dir {path} is not a directory",
                                  details={"run_dir": str(path)})
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=_RUN_DIR_MODE)
    os.chmod(path, _RUN_DIR_MODE)
    return path


def _cohort_columns(cohort_in: Any) -> list[str]:
    """The caller's cohort column names (PATID read as pid): taken for the fan-in."""
    import pandas as pd

    from spacescans.cohort import find_id_alias

    if isinstance(cohort_in, pd.DataFrame):
        cols = [str(c) for c in cohort_in.columns]
    else:
        if not Path(cohort_in).is_file():
            raise LinkConfigError(f"cohort file not found: {cohort_in}",
                                  details={"cohort": str(cohort_in)})
        cols = [str(c) for c in pd.read_csv(cohort_in, nrows=0).columns]
    if "pid" not in cols:
        alias = find_id_alias(cols)
        cols = ["pid" if c == alias else c for c in cols]
    return cols


def _weight_cache(cache: Any) -> WeightCache | None:
    from spacescans.orchestrate.cache import WeightCache

    if cache is None or cache is False:
        return None
    if isinstance(cache, WeightCache):
        return cache
    if isinstance(cache, (str, os.PathLike)):
        return WeightCache(cache, key_format="v2")
    raise LinkConfigError("cache must be None, a WeightCache or a cache directory",
                          details={"cache": type(cache).__name__})


def _executor_name(executor: Any) -> str:
    return executor if isinstance(executor, str) else type(executor).__name__


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

def coverage_table(partials: Mapping[str, pd.DataFrame], groups: Mapping[str, Sequence[str]],
                   cohort_keys: pd.DataFrame, overlay: Mapping[str, dict] | None = None
                   ) -> pd.DataFrame:
    """Per variable: episodes, episodes with any value, pct, and the same for patients.

    ``cohort_keys`` holds ``pid`` and ``episode_id`` for every input row;
    ``partials`` maps a completed source to its partial table. A variable's
    columns are its catalog ``value_cols`` after the source's renames (FAQSD).
    """
    import pandas as pd

    from spacescans import catalog

    rows = []
    episodes = len(cohort_keys)
    patients = int(cohort_keys["pid"].nunique())
    for exp, part in partials.items():
        renames = dict(catalog.recipe(exp).renames)
        for key in groups[exp]:
            cols = [renames.get(c, c) for c in catalog.variable(key, overlay=overlay or None).value_cols]
            cols = [c for c in cols if c in part.columns]
            if cols:
                joined = cohort_keys.merge(part[["pid", "episode_id"] + cols],
                                           on=["pid", "episode_id"], how="left")
                has = joined[cols].notna().any(axis=1)
                with_value = int(has.sum())
                patients_with = int(joined.loc[has.to_numpy(), "pid"].nunique())
            else:
                with_value = patients_with = 0
            rows.append({"variable": key, "experiment": exp, "episodes": episodes,
                         "with_value": with_value,
                         "pct": round(100.0 * with_value / episodes, 2) if episodes else 0.0,
                         "patients": patients, "patients_with_value": patients_with})
    return pd.DataFrame(rows, columns=["variable", "experiment", "episodes", "with_value", "pct",
                                       "patients", "patients_with_value"])


def _fan_in(cohort_keys: pd.DataFrame, partials: Mapping[str, pd.DataFrame],
            taken: Sequence[str]) -> pd.DataFrame:
    """``episode_id`` plus every source's columns, one row per input row, input order."""
    from spacescans.orchestrate import merge

    reserved = sorted({c for c in taken if c not in merge.KEYS})
    fanned = merge.fan_in(cohort_keys[["pid"]], list(partials.items()), reserved=reserved,
                          drop_episode_id=False)
    if len(fanned) != len(cohort_keys):
        raise RuntimeError(f"fan-in produced {len(fanned)} rows for {len(cohort_keys)} episodes "
                           "(a source returned duplicate episode rows)")
    cols = [c for c in fanned.columns if c not in merge.KEYS]
    return fanned[["episode_id"] + cols].reset_index(drop=True)


def _features(columns: Sequence[str]) -> pd.DataFrame:
    import pandas as pd

    from spacescans import catalog

    return pd.DataFrame(catalog.feature_rows(columns), columns=list(catalog.FEATURE_FIELDNAMES))


def _error_entry(exc: BaseException, exp: str, keys: list[str]) -> dict[str, Any]:
    entry: dict[str, Any] = {"kind": getattr(exc, "kind", "internal"), "experiment": exp,
                             "variables": list(keys), "message": str(exc)}
    if isinstance(exc, StepFailed):
        entry.update(step=exc.step, rc=exc.rc, timed_out=exc.timed_out,
                     log_tail=list(exc.log_tail), hint=exc.hint)
    elif isinstance(exc, RenderError):
        entry.update(step=exc.step, cause=type(exc.cause).__name__)
    return entry


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------

def link(cohort: pd.DataFrame | str | os.PathLike, variables: Sequence[str], *,
         data_dir: str | os.PathLike | None = None,
         run_dir: str | os.PathLike | None = None,
         buffer_m: int = 270, raster_res_m: int = 25,
         custom: Mapping[str, dict] | None = None,
         cache: WeightCache | str | os.PathLike | None = None,
         executor: Executor | Literal["subprocess", "inprocess"] = "subprocess",
         threads: int | None = None, memory_limit: str | None = None,
         on_error: Literal["stop", "continue"] = "stop",
         reserved_names: Sequence[str] = (),
         on_event: Callable[[Event], Any] | None = None,
         cancel: threading.Event | None = None) -> LinkResult:
    """Link ``cohort`` to ``variables``; see the module docstring for the steps.

    ``cohort``: a table or a CSV path with ``pid`` (or a PATID alias),
    ``startDate``, ``endDate``, ``longitude``, ``latitude``; only these five
    columns are written to disk. ``data_dir``: the data root (default
    ``$SPACESCANS_DATA_DIR``). ``run_dir``: where the run writes (default: a
    new private temp dir; created 0700 when missing). ``custom``: definitions
    of the selected custom variables, keyed by variable key. ``cache``: None
    (no cross-run cache), a :class:`~spacescans.orchestrate.cache.WeightCache`
    (either key format), or a directory (a ``v2`` cache). ``threads`` and
    ``memory_limit`` cap DuckDB in every step (subprocess: through the
    rendered configs; in-process: through the executor too).

    Raises ``LinkConfigError``, ``cohort.CohortError``,
    ``custom.CustomExposomeError``, ``data.DataError`` subclasses
    (``DataMissingError`` before any step runs) or
    :class:`~spacescans.orchestrate.run.RunCancelled`. A failing source is
    not raised: it is listed in ``failed`` (and ``errors``), as the web
    dispatcher reports a partial task.
    """
    import pandas as pd

    from spacescans import catalog
    from spacescans import cohort as _cohort
    from spacescans import data as _data
    from spacescans.orchestrate import provenance as _provenance
    from spacescans.orchestrate.context import RunContext
    from spacescans.orchestrate.execute import make_executor

    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    emit = on_event if on_event is not None else (lambda _ev: None)

    keys = _variables(variables)
    buffer_m = _positive_int("buffer_m", buffer_m)
    raster_res_m = _positive_int("raster_res_m", raster_res_m)
    if on_error not in ("stop", "continue"):
        raise LinkConfigError(f"on_error must be 'stop' or 'continue', got {on_error!r}")
    if threads is not None:
        _positive_int("threads", threads)
    if isinstance(reserved_names, (str, bytes)):
        raise LinkConfigError("reserved_names must be a list of column names")
    cohort_in = cohort if isinstance(cohort, pd.DataFrame) else Path(os.fspath(cohort))
    taken = list(_cohort_columns(cohort_in)) + [str(c) for c in reserved_names]
    overlay, warnings = _custom_overlay(custom, keys)
    groups = _groups(keys, overlay)
    root = _data.resolve_data_dir(data_dir)
    data_summary, data_warnings = _check_data(groups, overlay, root)
    warnings.extend(data_warnings)
    exe = make_executor(executor, threads=threads, memory_limit=memory_limit)
    weight_cache = _weight_cache(cache)

    rdir = _run_dir(run_dir)
    info = _cohort.to_pipeline_parquet(cohort_in, rdir / COHORT_FILE, variant="zcta5",
                                       pid_as_str=True, columns="required")
    if info.episode_id_overwritten:
        warnings.append({"code": "episode_id_overwritten",
                         "message": "the cohort carried an episode_id column; it was replaced "
                                    "by the input row number"})
    emit(Event("cohort", "link", data={"rows": info.rows, "patients": info.patients}))

    engine = {k: v for k, v in (("threads", threads), ("memory_limit", memory_limit))
              if v is not None} or None
    ctx = RunContext(run_dir=rdir, cohort_parquet=rdir / COHORT_FILE, data_dir=root,
                     buffer_m=buffer_m, raster_res_m=raster_res_m,
                     name_suffix=web_name_suffix(rdir), overlay=overlay or None, engine=engine)

    planned = {exp: catalog.plan(exp, ks, overlay=overlay or None) for exp, ks in groups.items()}
    distinct = {(s.name, s.cache_tag) if s.is_c3 else (s.name, exp)
                for exp, steps in planned.items() for s in steps}
    emit(Event("plan", "link", total=len(distinct),
               data={"total_steps": len(distinct),
                     "experiments": {exp: list(ks) for exp, ks in groups.items()}}))

    steps: list[StepRecord] = []
    partials: dict[str, pd.DataFrame] = {}
    failed: list[str] = []
    skipped: list[str] = []
    errors: list[dict] = []
    reuse: dict[tuple, StepRecord] = {}
    stopped = False
    for exp, exp_keys in groups.items():
        if stopped:
            skipped.append(exp)
            continue
        try:
            result = run_source(exp, exp_keys, ctx, cache=weight_cache, executor=exe,
                                on_event=emit, cancel=cancel, reuse=reuse)
        except RunCancelled as exc:
            steps.extend(getattr(exc, "steps", []))
            raise
        except (StepFailed, RenderError) as exc:
            steps.extend(getattr(exc, "steps", []))
            failed.append(exp)
            errors.append(_error_entry(exc, exp, exp_keys))
            warnings.append({"code": exc.kind, "experiment": exp, "message": str(exc)})
            emit(Event("done", exp, data={"ok": False, "kind": exc.kind}))
            stopped = on_error == "stop"
            continue
        steps.extend(result.steps)

        try:
            if catalog.recipe(exp).linkage_report:
                from spacescans.custom import diagnostics

                for record in diagnostics(rdir, exp_keys, overlay):
                    emit(Event("merge", exp, text=record["msg"],
                               data={"level": record["level"], "source": record["source"]}))
                    if record["level"] != "info":
                        warnings.append({"code": "custom_linkage", "experiment": exp,
                                         "message": record["msg"]})
            table, stats = partial(exp, exp_keys, ctx)
        except Exception as exc:  # noqa: BLE001 - the web fails the source on a merge error
            emit(Event("warning", exp, text=LOG_MERGE_FAILED.format(exc=exc),
                       data={"level": "error", "source": "runner", "code": "merge_failed"}))
            failed.append(exp)
            errors.append({"kind": "internal", "experiment": exp, "variables": list(exp_keys),
                           "message": f"merging {exp} failed: {type(exc).__name__}"})
            emit(Event("done", exp, data={"ok": False, "kind": "merge"}))
            stopped = on_error == "stop"
            continue
        if stats.low:
            emit(Event("warning", exp, data={**stats.as_log_fields(), "level": "warning"}))
            warnings.append({"code": "low_match", "experiment": exp, "pct": stats.match_pct,
                             "matched": stats.matched_n, "episodes": stats.cohort_n,
                             "message": f"{exp}: {stats.match_pct}% of episodes got a value "
                                        f"({stats.matched_n}/{stats.cohort_n})"})
        partials[exp] = table
        emit(Event("done", exp, data={"ok": True, "rows": len(table),
                                      "matched": stats.matched_n, "pct": stats.match_pct}))

    cohort_keys = pd.read_parquet(rdir / COHORT_FILE, columns=["pid", "episode_id"])
    exposures = _fan_in(cohort_keys, partials, taken)
    columns = [c for c in exposures.columns if c != "episode_id"]
    coverage = coverage_table(partials, groups, cohort_keys, overlay)
    ended = datetime.now(timezone.utc)
    prov = _provenance.build(
        ctx=ctx, groups=groups, overlay=overlay, steps=steps, cache=weight_cache,
        executor=_executor_name(executor), threads=threads, memory_limit=memory_limit,
        on_error=on_error, data_summary=data_summary, cohort_info=info,
        started=started, ended=ended, seconds=time.monotonic() - t0,
        failed=failed, skipped=skipped, columns=columns)
    return LinkResult(exposures=exposures, features=_features(columns), coverage=coverage,
                      steps=steps, failed=failed, skipped=skipped, warnings=warnings,
                      provenance=prov, run_dir=rdir, partials=partials, errors=errors)
