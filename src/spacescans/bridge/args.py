"""Checking command arguments that arrive as JSON (from R in particular).

R's jsonlite writes a length-1 vector as a scalar, so every list argument
also accepts a single value (``variables = "ndi"`` means ``["ndi"]``). Paths
must be absolute: the bridge runs with its cwd set to the client's run
directory, so a relative path would silently mean something else.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from spacescans.bridge.errors import BridgeError


def bool_arg(args: Mapping[str, Any], name: str, default: bool = False) -> bool:
    value = args.get(name, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise BridgeError("config", f"argument '{name}' must be true or false")
    return value


def int_arg(args: Mapping[str, Any], name: str, *, minimum: int | None = None,
            default: int | None = None) -> int | None:
    value = args.get(name)
    if value is None:
        return default
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BridgeError("config", f"argument '{name}' must be a whole number, got {value!r}")
    if minimum is not None and value < minimum:
        raise BridgeError("config", f"argument '{name}' must be at least {minimum}, got {value}")
    return value


def number_arg(args: Mapping[str, Any], name: str, *, minimum: float | None = None,
               maximum: float | None = None, default: float | None = None) -> float | None:
    value = args.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BridgeError("config", f"argument '{name}' must be a number, got {value!r}")
    value = float(value)
    if math.isnan(value):
        raise BridgeError("config", f"argument '{name}' must be a number, got NaN")
    if minimum is not None and value < minimum:
        raise BridgeError("config", f"argument '{name}' must be at least {minimum:g}, got {value:g}")
    if maximum is not None and value > maximum:
        raise BridgeError("config", f"argument '{name}' must be at most {maximum:g}, got {value:g}")
    return value


def str_arg(args: Mapping[str, Any], name: str, *, required: bool = False) -> str | None:
    value = args.get(name)
    if value is None:
        if required:
            raise BridgeError("config", f"argument '{name}' is required")
        return None
    if not isinstance(value, str) or not value:
        raise BridgeError("config", f"argument '{name}' must be a non-empty string")
    return value


def choice_arg(args: Mapping[str, Any], name: str, choices: Sequence[str], *,
               default: str) -> str:
    value = args.get(name)
    if value is None:
        return default
    if value not in choices:
        raise BridgeError("config", f"argument '{name}' must be one of {', '.join(map(repr, choices))}, "
                          f"got {value!r}", details={"argument": name, "choices": list(choices)})
    return value


def str_list_arg(args: Mapping[str, Any], name: str, *, required: bool = False,
                 allow_empty: bool = True) -> list[str] | None:
    """A list of non-empty strings; a single string counts as a list of one."""
    value = args.get(name)
    if value is None:
        if required:
            raise BridgeError("config", f"argument '{name}' is required")
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise BridgeError("config", f"argument '{name}' must be a list of non-empty strings")
    if not value and not allow_empty:
        raise BridgeError("config", f"argument '{name}' must not be empty")
    return list(value)


def abs_path(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise BridgeError("config", f"argument '{name}' must be a path string")
    if not Path(value).is_absolute():
        raise BridgeError("config", f"argument '{name}' must be an absolute path, got {value!r}",
                          details={"argument": name, "value": value})
    return value


def path_arg(args: Mapping[str, Any], name: str, *, required: bool = False) -> str | None:
    value = args.get(name)
    if value is None:
        if required:
            raise BridgeError("config", f"argument '{name}' is required")
        return None
    return abs_path(value, name)


def path_list_arg(args: Mapping[str, Any], name: str, *, required: bool = False) -> list[str] | None:
    value = args.get(name)
    if value is None:
        if required:
            raise BridgeError("config", f"argument '{name}' is required")
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise BridgeError("config", f"argument '{name}' must be a list of absolute paths")
    return [abs_path(v, name) for v in value]


def mapping_arg(args: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    """An object; ``[]`` (jsonlite's empty named list) counts as empty."""
    value = args.get(name)
    if value is None or value == []:
        return None if value is None else {}
    if not isinstance(value, dict):
        raise BridgeError("config", f"argument '{name}' must be an object")
    return value
