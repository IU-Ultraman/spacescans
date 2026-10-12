"""Error kinds: turning Python exceptions into ``{"kind", "message", ...}``."""
from __future__ import annotations

import traceback
from typing import Any

from spacescans.bridge.protocol import EXIT_ERROR


class BridgeError(Exception):
    """An error the bridge reports in the result envelope.

    ``kind`` is one of protocol.ERROR_KINDS. ``hint`` is client-neutral advice;
    ``details`` is machine-readable (clients build their own hints from it).
    """

    def __init__(self, kind: str, message: str, *, hint: str | None = None,
                 details: dict[str, Any] | None = None, exit_code: int = EXIT_ERROR):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint
        self.details = details or {}
        self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        cause = self.__cause__ if self.__cause__ is not None else self
        return {
            "kind": self.kind,
            "message": self.message,
            "hint": self.hint,
            "details": self.details,
            "type": type_name(cause),
            "traceback": format_tb(cause),
        }


def type_name(exc: BaseException) -> str:
    cls = type(exc)
    mod = cls.__module__
    return cls.__qualname__ if mod in ("builtins", "__main__") else f"{mod}.{cls.__qualname__}"


def format_tb(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


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


def classify(exc: BaseException) -> BridgeError:
    """Map an exception raised by a command to a BridgeError (cause attached)."""
    if isinstance(exc, BridgeError):
        return exc
    from spacescans._extras import MissingExtraError
    from spacescans.config_resolution import ConfigResolutionError

    msg = str(exc).strip() or type(exc).__name__
    err: BridgeError
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
        err = BridgeError("data", msg, hint=f"check that {path} exists and is readable" if path else None,
                          details={"path": path})
    elif isinstance(exc, SystemExit):
        err = BridgeError("runtime", f"the pipeline called sys.exit({exc.code!r})")
    else:
        err = BridgeError("runtime", f"{type(exc).__name__}: {msg}")
    err.__cause__ = exc
    return err
