"""Protocol constants, the result envelope and JSON helpers."""
from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import json
import math
import os
import platform
import sys
import tempfile
from pathlib import Path, PurePath
from typing import Any

PROTOCOL = 1
# Protocol versions this bridge can speak. Additive changes (new optional
# fields, events or commands) keep the number; removing or renaming a field
# bumps it.
PROTOCOL_RANGE = (1, 1)

EXIT_OK = 0
EXIT_ERROR = 1           # handled error, result written with ok = false
EXIT_USAGE = 2           # protocol / usage error (bad JSON, protocol, command)
EXIT_NO_RESULT = 3       # the result file could not be written
EXIT_SIGINT = 130
EXIT_SIGTERM = 143

ERROR_KINDS = (
    "protocol",       # bad request, unsupported protocol, unknown command
    "config",         # invalid arguments or pipeline config
    "data",           # an input file is missing or unreadable
    "missing_extra",  # a pattern/reader needs an extra that is not installed
    "runtime",        # the pipeline raised
    "interrupted",    # SIGINT / SIGTERM
    "internal",       # a bug in the bridge itself
)


def runtime_info() -> dict[str, Any]:
    """The ``spacescans`` block carried by every result."""
    import spacescans
    from spacescans._capabilities import code_fingerprint

    mac = platform.mac_ver()[0]
    plat = (f"macOS-{mac}-{platform.machine()}" if mac
            else f"{platform.system()}-{platform.release()}-{platform.machine()}")
    return {
        "version": spacescans.__version__,
        "code_fp": code_fingerprint(),
        "python": platform.python_version(),
        "executable": sys.executable,
        "platform": plat,
        "pid": os.getpid(),
    }


def to_jsonable(obj: Any) -> Any:
    """Convert to plain JSON types: NaN/inf -> None, numpy scalars, paths, dates, enums."""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return [to_jsonable(v) for v in sorted(obj, key=str)]
    if isinstance(obj, (PurePath,)):
        return str(obj)
    if isinstance(obj, enum.Enum):
        return to_jsonable(obj.value)
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return to_jsonable(dataclasses.asdict(obj))
    if hasattr(obj, "model_dump"):  # pydantic
        return to_jsonable(obj.model_dump(mode="json"))
    if hasattr(obj, "item") and callable(obj.item):  # numpy / pandas scalars
        try:
            return to_jsonable(obj.item())
        except Exception:
            pass
    if hasattr(obj, "tolist") and callable(obj.tolist):  # numpy arrays
        return to_jsonable(obj.tolist())
    return str(obj)


def dumps(obj: Any, *, indent: int | None = None) -> str:
    return json.dumps(to_jsonable(obj), ensure_ascii=False, allow_nan=False, indent=indent)


def write_json_atomic(path: Path, obj: Any) -> None:
    """Write JSON next to ``path`` and rename it into place, so readers never see half a file."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dumps(obj, indent=1))
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def envelope(command: str, *, ok: bool, value: Any = None, warnings: list | None = None,
             error: dict | None = None, elapsed_s: float = 0.0) -> dict[str, Any]:
    try:
        info = runtime_info()
    except Exception as e:  # never lose the result because of the info block
        info = {"version": None, "code_fp": None, "error": f"{type(e).__name__}: {e}"}
    return {
        "protocol": PROTOCOL,
        "ok": ok,
        "command": command,
        "spacescans": info,
        "elapsed_s": round(elapsed_s, 3),
        "value": value if ok else None,
        "warnings": warnings or [],
        "error": error,
    }
