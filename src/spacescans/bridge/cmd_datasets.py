"""``datasets-register``, ``datasets-list``, ``datasets-get``, ``datasets-update``, ``datasets-remove``.

The registered-dataset API of :mod:`spacescans.custom` for clients that reach
Python through the bridge (the R package). Every command takes an optional
``root`` (an absolute path; default ``$SPACESCANS_DATASETS_DIR``, else the
per-user data directory). A dataset is named by ``name``: its name (any
case), its id or its variable key.

A record in a result is :meth:`DatasetRecord.to_dict` plus ``path`` (the
dataset directory) and ``variable_key``. ``datasets-get`` also returns the
linkable ``definition``: pass ``variables + key`` and ``custom = {key:
definition}`` to ``link``. Registry errors are ``config`` errors whose
message names files, columns and counts, never a data value.

R's jsonlite writes a length-1 vector as a scalar and NULL as ``{}``; every
list argument accepts a scalar, and ``{}``/``[]`` mean "not given".
"""
from __future__ import annotations

from typing import Any

from spacescans.bridge.args import (
    bool_arg,
    mapping_arg,
    path_arg,
    path_list_arg,
    str_arg,
)
from spacescans.bridge.errors import BridgeError

Outcome = tuple[Any, list]


def _absent(value: Any) -> bool:
    return value is None or value == {} or value == []


def _record(rec: Any) -> dict[str, Any]:
    return {**rec.to_dict(), "path": str(rec.directory), "variable_key": rec.variable_key}


def _key(args: dict) -> Any:
    value = args.get("key")
    if _absent(value):
        return None
    if not isinstance(value, (str, dict)):
        raise BridgeError("config", "argument 'key' must be a boundary name or an object")
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if not _absent(v)}
    return value


def _variables(args: dict) -> Any:
    value = args.get("variables")
    if _absent(value):
        return None
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        raise BridgeError("config", "argument 'variables' must be a list of names or objects")
    out = []
    for item in value:
        if isinstance(item, dict):
            item = {k: v for k, v in item.items() if not _absent(v)}
            if isinstance(item.get("band"), float) and item["band"].is_integer():
                item["band"] = int(item["band"])
        out.append(item)
    return out


def _metadata(args: dict) -> dict[str, Any] | None:
    meta = mapping_arg(args, "metadata")
    if meta is None:
        return None
    return {k: (None if _absent(v) else v) for k, v in meta.items()}


def cmd_datasets_register(args: dict, ctx) -> Outcome:
    """Register a dataset: ``files`` (absolute paths), ``name``, ``kind``, ``temporal``;
    optional ``periods`` (parallel to ``files``: a year or a date per file, or null),
    ``key``, ``variables``, ``metadata``, ``root``, ``overwrite``."""
    from spacescans import custom

    files = path_list_arg(args, "files", required=True)
    if not files:
        raise BridgeError("config", "argument 'files' must list at least one file")
    periods = args.get("periods")
    if not _absent(periods):
        if not isinstance(periods, list):
            periods = [periods]
        if len(periods) != len(files):
            raise BridgeError("config", f"'periods' has {len(periods)} entries for "
                                        f"{len(files)} files")
        paths: Any = {f: (None if _absent(p) else p) for f, p in zip(files, periods)}
    else:
        paths = files
    rec = custom.register(
        paths, name=str_arg(args, "name", required=True),
        kind=str_arg(args, "kind", required=True),
        temporal=str_arg(args, "temporal", required=True),
        key=_key(args), variables=_variables(args), metadata=_metadata(args),
        root=path_arg(args, "root"), overwrite=bool_arg(args, "overwrite"))
    return {"dataset": _record(rec), "key": rec.variable_key}, []


def cmd_datasets_list(args: dict, ctx) -> Outcome:
    """Every dataset in the root, oldest first."""
    from spacescans import custom

    root = path_arg(args, "root")
    return {"root": str(custom.datasets_root(root)),
            "datasets": [_record(r) for r in custom.list_datasets(root)]}, []


def cmd_datasets_get(args: dict, ctx) -> Outcome:
    """One dataset (``name``: name, id or variable key), with its linkable definition."""
    from spacescans import custom

    rec = custom.get_dataset(str_arg(args, "name", required=True), path_arg(args, "root"))
    return {"dataset": _record(rec), "key": rec.variable_key,
            "definition": rec.definition()}, []


def cmd_datasets_update(args: dict, ctx) -> Outcome:
    """Change a dataset's ``new_name``, ``metadata`` or variable units/descriptions."""
    from spacescans import custom

    rec = custom.update_dataset(str_arg(args, "name", required=True),
                                name=str_arg(args, "new_name"), metadata=_metadata(args),
                                variables=_variables(args), root=path_arg(args, "root"))
    return {"dataset": _record(rec), "key": rec.variable_key}, []


def cmd_datasets_remove(args: dict, ctx) -> Outcome:
    """Delete a dataset; returns the removed record."""
    from spacescans import custom

    rec = custom.remove_dataset(str_arg(args, "name", required=True), path_arg(args, "root"))
    return {"removed": {**rec.to_dict(), "variable_key": rec.variable_key}}, []


COMMANDS = {
    "datasets-register": cmd_datasets_register,
    "datasets-list": cmd_datasets_list,
    "datasets-get": cmd_datasets_get,
    "datasets-update": cmd_datasets_update,
    "datasets-remove": cmd_datasets_remove,
}
