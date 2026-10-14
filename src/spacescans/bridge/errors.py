"""Error kinds: turning Python exceptions into ``{"kind", "message", ...}``.

The kinds are design section 3.5's (see ``protocol.ERROR_KINDS``):

* an exception that carries its own ``kind`` (and usually ``to_dict()``) keeps
  it: the ``spacescans.data`` errors (``data_missing``, ``data_corrupt``,
  ``network``, ``disk_space``, ``locked``, ``terms_required``, ``config``),
  ``cohort.CohortError`` (``cohort``), ``custom.CustomExposomeError``
  (``config``) and the orchestrator's ``StepFailed`` (``step_failed``) and
  ``RenderError`` (``config``);
* pydantic validation errors, YAML errors and unresolvable config paths are
  ``config``; a missing optional extra is ``missing_extra``; a file that is not
  there or not readable is ``data_missing`` (``details.path``);
* anything else is ``internal``: a bug, reported with its traceback.

Cohort privacy: when an error's ``to_dict()`` message differs from ``str(exc)``
(``CohortError`` drops the sample value an unrecognised date quotes), the
traceback is rebuilt from the frames and the safe message, so no cohort value
reaches the result, the log or the client.
"""
from __future__ import annotations

import re
import traceback
from collections.abc import Mapping
from typing import Any

from spacescans.bridge.protocol import ERROR_KINDS, EXIT_ERROR

LOG_TAIL_LINES = 40
OOM_EXIT_CODES = (-9, 137)
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class BridgeError(Exception):
    """An error the bridge reports in the result envelope.

    ``kind`` is one of protocol.ERROR_KINDS. ``hint`` is client-neutral advice;
    ``details`` is machine-readable (clients build their own hints from it).
    ``safe_message``, when set, replaces ``str(cause)`` in the traceback.
    """

    def __init__(self, kind: str, message: str, *, hint: str | None = None,
                 details: dict[str, Any] | None = None, exit_code: int = EXIT_ERROR):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint
        self.details = details or {}
        self.exit_code = exit_code
        self.safe_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        cause = self.__cause__ if self.__cause__ is not None else self
        return {
            "kind": self.kind,
            "message": self.message,
            "hint": self.hint,
            "details": self.details,
            "type": type_name(cause),
            "traceback": format_tb(cause, message=self.safe_message if cause is not self else None),
        }


def type_name(exc: BaseException) -> str:
    cls = type(exc)
    mod = cls.__module__
    return cls.__qualname__ if mod in ("builtins", "__main__") else f"{mod}.{cls.__qualname__}"


def format_tb(exc: BaseException, *, message: str | None = None) -> str:
    """The traceback text. With ``message``, the exception line uses it instead
    of ``str(exc)`` and chained exceptions are left out (they may quote values)."""
    if message is None:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    frames = "".join(traceback.format_tb(exc.__traceback__))
    head = "Traceback (most recent call last):\n" if frames else ""
    return f"{head}{frames}{type_name(exc)}: {message}\n"


def _validation_details(e) -> dict[str, Any]:
    errors = []
    for err in e.errors()[:50]:
        inp = repr(err.get("input"))
        errors.append({
            "loc": ".".join(str(p) for p in err.get("loc", ())),
            "msg": err.get("msg"),
            "type": err.get("type"),
            "input": inp if len(inp) <= 200 else inp[:197] + "...",
        })
    return {"errors": errors, "count": e.error_count()}


def _own_kind(exc: BaseException) -> str | None:
    """The ``kind`` an exception declares, if the bridge should honour it.

    Honoured for exceptions defined in spacescans, and for any exception
    whose kind is one of the protocol's kinds.
    """
    kind = getattr(exc, "kind", None)
    if not (isinstance(kind, str) and _KIND_RE.match(kind)):
        return None
    mod = type(exc).__module__ or ""
    if mod == "spacescans" or mod.startswith("spacescans.") or kind in ERROR_KINDS:
        return kind
    return None


def _log_tail(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        lines = value.splitlines()
    else:
        try:
            lines = [str(v) for v in value]
        except TypeError:
            lines = [str(value)]
    return lines[-LOG_TAIL_LINES:]


def _memory_note(experiment: Any, episodes: int | None, step: Any) -> str | None:
    """The measured memory need of the source (gate M table), or None; never raises."""
    try:
        from spacescans.orchestrate.memory import oom_note

        return oom_note(experiment, episodes, step=step)
    except Exception:  # noqa: BLE001 - the note only sharpens a hint on the error path
        return None


def _step_failed(exc: BaseException, message: str, hint: str | None,
                 details: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
    step = getattr(exc, "step", None) or details.get("step")
    rc = getattr(exc, "rc", None)
    if rc is None:
        rc = details.get("rc")
    tail = _log_tail(getattr(exc, "log_tail", None) or details.get("log_tail"))
    experiment = getattr(exc, "experiment", None) or details.get("experiment")
    timed_out = bool(getattr(exc, "timed_out", False) or details.get("timed_out"))
    # A step the executor killed itself after its timeout is not out of memory.
    oom = rc in OOM_EXIT_CODES and not timed_out
    details = {**details, "step": step, "rc": rc, "log_tail": tail, "oom": oom}
    if experiment is not None:
        details["experiment"] = experiment
    if not message or message == type(exc).__name__:
        message = f"pipeline step {step} failed with exit code {rc}"
    if oom:
        hint = (f"step {step} was killed (exit code {rc}), which usually means the machine ran out "
                "of memory: use fewer threads or a lower memory_limit, or a smaller cohort")
        episodes = getattr(exc, "episodes", None) or details.get("episodes")
        details["episodes"] = episodes
        note = _memory_note(experiment, episodes if isinstance(episodes, int) else None, step)
        details["memory_note"] = note           # R's own hint quotes it (conditions.R)
        if note:
            hint = f"{hint}; {note}"
    elif hint is None:
        hint = "details.log_tail has the end of the step's output; logs.jsonl in the run directory has all of it"
    return message, hint, details


def _from_own_kind(exc: BaseException, kind: str) -> BridgeError:
    d: Any = {}
    to_dict = getattr(exc, "to_dict", None)
    if callable(to_dict):
        try:
            d = to_dict()
        except Exception:  # a broken to_dict must not hide the real error
            d = {}
    if not isinstance(d, Mapping):
        d = {}
    if isinstance(d.get("kind"), str) and _KIND_RE.match(d["kind"]):
        kind = d["kind"]
    message = d.get("message") or getattr(exc, "message", None) or str(exc).strip() or type(exc).__name__
    message = str(message)
    hint = d["hint"] if "hint" in d else getattr(exc, "hint", None)
    raw_details = d["details"] if "details" in d else getattr(exc, "details", None)
    details = dict(raw_details) if isinstance(raw_details, Mapping) else {}
    if kind == "step_failed":
        message, hint, details = _step_failed(exc, message, hint, details)
    report = getattr(exc, "report", None)  # data.fetch attaches what it did before failing
    if report is not None and "report" not in details and callable(getattr(report, "to_dict", None)):
        try:
            details["report"] = report.to_dict()
        except Exception:
            pass
    err = BridgeError(kind, message, hint=None if hint is None else str(hint), details=details)
    if message != str(exc):
        err.safe_message = message
    return err


def classify(exc: BaseException) -> BridgeError:
    """Map an exception raised by a command to a BridgeError (cause attached)."""
    if isinstance(exc, BridgeError):
        return exc
    kind = _own_kind(exc)
    if kind is not None:
        err = _from_own_kind(exc, kind)
        err.__cause__ = exc
        return err

    from spacescans._extras import MissingExtraError
    from spacescans.config_resolution import ConfigResolutionError

    msg = str(exc).strip() or type(exc).__name__
    try:
        from pydantic import ValidationError
    except ImportError:  # pragma: no cover - pydantic is a base dependency
        ValidationError = ()  # type: ignore[assignment]
    try:
        import yaml
        yaml_error: tuple = (yaml.YAMLError,)
    except ImportError:  # pragma: no cover
        yaml_error = ()

    if ValidationError and isinstance(exc, ValidationError):
        err = BridgeError("config", f"Invalid pipeline config: {msg}", details=_validation_details(exc))
    elif isinstance(exc, ConfigResolutionError):
        err = BridgeError("config", msg)
    elif yaml_error and isinstance(exc, yaml_error):
        err = BridgeError("config", f"Invalid YAML: {msg}")
    elif isinstance(exc, MissingExtraError):
        extra = getattr(exc, "extra", None)
        missing = list(getattr(exc, "missing", ()) or ())
        hint = (f"install the '{extra}' extra: pip install 'spacescans-pipeline[{extra}]'"
                if extra else None)
        err = BridgeError("missing_extra", msg, hint=hint, details={"extra": extra, "missing": missing})
    elif isinstance(exc, KeyError) and any(
            s in msg for s in ("Unknown linkage pattern", "Unknown reader plugin", "Unknown prep plugin")):
        err = BridgeError("config", msg.strip("'\""))
    elif isinstance(exc, (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError)):
        path = getattr(exc, "filename", None)
        err = BridgeError("data_missing", msg,
                          hint=f"check that {path} exists and is readable" if path else None,
                          details={"path": path, "reason": type(exc).__name__})
    elif isinstance(exc, SystemExit):
        err = BridgeError("internal", f"the pipeline called sys.exit({exc.code!r})")
    else:
        err = BridgeError("internal", f"{type(exc).__name__}: {msg}",
                          hint="this looks like a bug in spacescans; error.traceback shows where")
    err.__cause__ = exc
    return err
