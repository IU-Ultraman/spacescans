"""Pipeline configs arriving over JSON: load, normalise, find unknown keys.

JSON clients (R in particular) cannot always say "a list of one": R's
length-1 vectors serialise as scalars. ``coerce_scalars`` therefore wraps a
scalar wherever the DatasetConfig schema allows only a list (``value_cols``,
``years``, ``depends_on`` ...). Fields that accept a scalar too
(``source.file: str | list[str]``) are left alone.
"""
from __future__ import annotations

import copy
import types
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from spacescans.bridge.errors import BridgeError

TOP_LEVEL_EXTRA_KEYS = ("base_dir", "base_output_dir")  # consumed by the loader


def _dataset_model():
    from spacescans.models.config import DatasetConfig
    return DatasetConfig


def _is_model(t: Any) -> bool:
    from pydantic import BaseModel
    return isinstance(t, type) and issubclass(t, BaseModel)


def _options(ann: Any) -> list[Any] | None:
    """Non-None members of a Union annotation, or None if ``ann`` is not a Union."""
    if get_origin(ann) in (Union, types.UnionType):
        return [a for a in get_args(ann) if a is not type(None)]
    return None


def _unwrap(ann: Any) -> Any:
    while get_origin(ann) is Annotated:
        ann = get_args(ann)[0]
    return ann


def _pick_model(models: list[Any], value: dict) -> Any:
    if len(models) == 1:
        return models[0]
    tag = value.get("type")
    for m in models:
        f = m.model_fields.get("type")
        if f is not None and get_origin(f.annotation) is Literal and tag in get_args(f.annotation):
            return m
    return None


def _coerce(ann: Any, value: Any) -> Any:
    ann = _unwrap(ann)
    if value is None:
        return None
    opts = _options(ann)
    if opts is not None:
        opts = [_unwrap(o) for o in opts]
        if len(opts) == 1:
            return _coerce(opts[0], value)
        if isinstance(value, list):
            lists = [o for o in opts if get_origin(o) is list]
            return _coerce(lists[0], value) if len(lists) == 1 else value
        if isinstance(value, dict):
            m = _pick_model([o for o in opts if _is_model(o)], value)
            return _coerce_model(m, value) if m is not None else value
        if opts and all(get_origin(o) is list for o in opts):
            return _coerce(opts[0], [value])
        return value
    if get_origin(ann) is list:
        args = get_args(ann)
        items = value if isinstance(value, list) else [value]
        return [_coerce(args[0], v) for v in items] if args else items
    if _is_model(ann) and isinstance(value, dict):
        return _coerce_model(ann, value)
    return value


def _coerce_model(model: Any, data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for name, field in model.model_fields.items():
        key = field.alias or name
        if key in out:
            out[key] = _coerce(field.annotation, out[key])
    return out


def coerce_scalars(raw: dict) -> dict:
    """Wrap scalars into one-element lists where the config schema needs a list."""
    return _coerce_model(_dataset_model(), raw)


def _walk_unknown(ann: Any, value: Any, path: str, out: list[str]) -> None:
    ann = _unwrap(ann)
    if value is None:
        return
    opts = _options(ann)
    if opts is not None:
        opts = [_unwrap(o) for o in opts]
        if isinstance(value, dict):
            m = _pick_model([o for o in opts if _is_model(o)], value)
            if m is not None:
                _walk_model_unknown(m, value, path, out)
        elif isinstance(value, list):
            lists = [o for o in opts if get_origin(o) is list]
            if len(lists) == 1:
                _walk_unknown(lists[0], value, path, out)
        return
    if get_origin(ann) is list and isinstance(value, list):
        args = get_args(ann)
        if args:
            for i, v in enumerate(value):
                _walk_unknown(args[0], v, f"{path}[{i}]", out)
        return
    if _is_model(ann) and isinstance(value, dict):
        _walk_model_unknown(ann, value, path, out)


def _walk_model_unknown(model: Any, data: dict, path: str, out: list[str],
                        allowed: tuple[str, ...] = ()) -> None:
    fields = {(f.alias or n): f for n, f in model.model_fields.items()}
    for key, value in data.items():
        where = f"{path}.{key}" if path else str(key)
        if key in fields:
            _walk_unknown(fields[key].annotation, value, where, out)
        elif key not in allowed:
            out.append(where)


def unknown_keys(raw: dict) -> list[str]:
    """Dotted paths of keys the config schema does not know (pydantic drops them silently)."""
    out: list[str] = []
    if isinstance(raw, dict):
        _walk_model_unknown(_dataset_model(), raw, "", out, allowed=TOP_LEVEL_EXTRA_KEYS)
    return out


def absolute_path_arg(args: dict, name: str, *, required: bool = False) -> str | None:
    value = args.get(name)
    if value is None:
        if required:
            raise BridgeError("config", f"argument '{name}' is required")
        return None
    if not isinstance(value, str) or not value:
        raise BridgeError("config", f"argument '{name}' must be a path string")
    if not Path(value).is_absolute():
        raise BridgeError("config", f"argument '{name}' must be an absolute path, got {value!r}",
                          details={"argument": name, "value": value})
    return value


def load_raw_config(config: Any) -> tuple[dict, dict]:
    """``config`` is a mapping or an absolute path to a YAML file.

    Returns a deep copy of the mapping and ``{"source": "dict" | "yaml", "path"}``.
    """
    if isinstance(config, dict):
        return copy.deepcopy(config), {"source": "dict", "path": None}
    if isinstance(config, str):
        path = Path(config)
        if not path.is_absolute():
            raise BridgeError("config", f"config path must be absolute, got {config!r}")
        if not path.is_file():
            raise BridgeError("config", f"config file not found: {config}", details={"path": config})
        import yaml
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise BridgeError("config", f"cannot parse {config}: {e}", details={"path": config}) from e
        if not isinstance(raw, dict):
            raise BridgeError("config", f"{config} does not hold a mapping at the top level",
                              details={"path": config})
        return raw, {"source": "yaml", "path": str(path)}
    raise BridgeError("config", "argument 'config' must be a mapping or an absolute YAML path",
                      details={"got": type(config).__name__})
