"""``dataset-register``, ``dataset-list``, ``dataset-get`` and ``dataset-remove``.

The registered-dataset registry of :mod:`spacescans.custom` (phase D,
``register`` / ``list_datasets`` / ``get_dataset`` / ``remove_dataset``) for
clients that do not import spacescans in-process: the R package's
``ss_register_dataset()``, ``ss_datasets()``, ``ss_dataset()`` and
``ss_remove_dataset()``. Each command is one call into the registry:

* ``dataset-register``: ``register(paths, name=, kind=, temporal=, key=,
  variables=, metadata=, root=, overwrite=, **options)``;
* ``dataset-list``: ``list_datasets(root=)``;
* ``dataset-get``: ``get_dataset(name_or_id, root=)``;
* ``dataset-remove``: ``remove_dataset(name_or_id, root=)`` (the record is read
  first, so the value names what was removed).

Values carry records as plain JSON objects (the ``DatasetRecord`` fields) and
``root``: the registry directory by the phase-D rule (the argument, else
``$SPACESCANS_DATASETS_DIR``, else ``platformdirs.user_data_dir("spacescans")
/ "datasets"``); the registry's own resolution decides where files go.

``link`` and ``coverage`` take registered names or ids in a ``datasets``
argument next to the in-memory ``custom`` definitions: :func:`registered_names`
reads it and :func:`link_custom` builds the ``custom`` value that
``orchestrate.link(custom=[...])`` takes.

Errors: the registry's own exceptions declare their ``kind`` (for example
``custom.CustomExposomeError``, ``config``) and pass through unchanged; an
unknown name or id (a ``LookupError`` without a kind, or ``None`` from
``get_dataset``) is ``config``; arguments ``register()`` does not take are
``config``. A spacescans without the registry functions answers ``internal``
with ``details.missing``. No message quotes data values.

``spacescans.custom`` is imported inside the commands (it loads numpy), so the
other bridge commands do not pay for it.
"""
from __future__ import annotations

import dataclasses
import inspect
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from spacescans.bridge.args import (
    bool_arg,
    mapping_arg,
    path_arg,
    path_list_arg,
    str_arg,
    str_list_arg,
)
from spacescans.bridge.errors import BridgeError
from spacescans.bridge.protocol import to_jsonable

Outcome = tuple[Any, list]

#: The registry functions the commands call (PHASE_D.md 2.2).
REGISTRY_API = ("register", "list_datasets", "get_dataset", "remove_dataset")
#: Environment variable naming the registry directory (PHASE_D.md 2.2).
DATASETS_DIR_ENV = "SPACESCANS_DATASETS_DIR"
_REGISTER_ARGS = ("name", "kind", "temporal", "key", "variables", "metadata", "root", "overwrite")


# --- the registry seam -------------------------------------------------------------

def registry_api() -> Any:
    """The module with the registry functions, ``spacescans.custom``.

    Tests replace this function with one returning a fake.
    """
    from spacescans import custom

    missing = [n for n in REGISTRY_API if not callable(getattr(custom, n, None))]
    if missing:
        raise BridgeError("internal", "this spacescans install has no dataset registry "
                          f"(spacescans.custom lacks {', '.join(missing)})",
                          hint="install spacescans 0.3.0 or later, which registers datasets",
                          details={"missing": missing})
    return custom


def resolved_root(root: str | None) -> str | None:
    """The registry directory by the phase-D rule, for the value (None when unknown)."""
    if root:
        return root
    env = os.environ.get(DATASETS_DIR_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    try:
        import platformdirs
    except ImportError:
        return None
    return str(Path(platformdirs.user_data_dir("spacescans")) / "datasets")


def record_dict(rec: Any) -> dict[str, Any]:
    """A ``DatasetRecord`` (dataclass, pydantic model, mapping or object) as plain JSON."""
    to_dict = getattr(rec, "to_dict", None)
    if callable(to_dict):
        d = to_dict()
    elif dataclasses.is_dataclass(rec) and not isinstance(rec, type):
        d = dataclasses.asdict(rec)
    elif hasattr(rec, "model_dump"):
        d = rec.model_dump(mode="json")
    elif isinstance(rec, Mapping):
        d = dict(rec)
    else:
        d = dict(vars(rec))
    return to_jsonable(d)


# --- arguments --------------------------------------------------------------------

def _variables_arg(args: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """``variables``: names, or objects with ``name`` (and ``unit``, ``description``)."""
    value = args.get("variables")
    if value is None:
        return None
    if isinstance(value, (str, Mapping)):
        value = [value]
    if not isinstance(value, list) or not value:
        raise BridgeError("config", "argument 'variables' must be a non-empty list of names or "
                                    "{name, unit, description} objects")
    out = []
    for v in value:
        if isinstance(v, str):
            v = {"name": v}
        if not isinstance(v, Mapping) or not isinstance(v.get("name"), str) or not v["name"]:
            raise BridgeError("config", "every variable needs a non-empty 'name'")
        unknown = sorted(set(v) - {"name", "unit", "description"})
        if unknown:
            raise BridgeError("config", f"unknown variable field(s): {', '.join(unknown)}",
                              details={"fields": unknown})
        out.append({k: v[k] for k in ("name", "unit", "description") if v.get(k) is not None})
    return out


def _bind(fn: Any, *args: Any, **kwargs: Any) -> None:
    """Refuse arguments ``fn`` does not take, as a ``config`` error, before calling it."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):     # a builtin without a signature: let the call decide
        return
    try:
        sig.bind(*args, **kwargs)
    except TypeError as e:
        raise BridgeError("config", f"{getattr(fn, '__name__', 'register')}() does not accept these "
                                    f"arguments: {e}", details={"arguments": sorted(kwargs)}) from None


def _unknown(ref: str, root: str | None) -> BridgeError:
    return BridgeError("config", f"no registered dataset named or with id {ref!r}",
                       hint="dataset-list shows the registered datasets",
                       details={"name_or_id": ref, "root": resolved_root(root)})


def _get(api: Any, ref: str, root: str | None) -> Any:
    try:
        rec = api.get_dataset(ref, root=root)
    except LookupError as e:
        if isinstance(getattr(e, "kind", None), str):
            raise                       # the registry's own error, with its kind
        raise _unknown(ref, root) from None
    if rec is None:
        raise _unknown(ref, root)
    return rec


# --- commands ---------------------------------------------------------------------

def cmd_dataset_register(args: dict, ctx: Any) -> Outcome:
    """Validate and prepare data files once; store them and ``dataset.json`` in the registry."""
    api = registry_api()
    paths = path_list_arg(args, "paths", required=True)
    if not paths:
        raise BridgeError("config", "argument 'paths' must name at least one file")
    name = str_arg(args, "name", required=True)
    kind = str_arg(args, "kind", required=True)
    temporal = str_arg(args, "temporal", required=True)
    key = args.get("key")
    if key is not None and not isinstance(key, (str, Mapping)):
        raise BridgeError("config", "argument 'key' must be a string or an object")
    metadata = mapping_arg(args, "metadata") or None
    root = path_arg(args, "root")
    options = mapping_arg(args, "options") or {}
    clash = sorted(set(options) & set(_REGISTER_ARGS))
    if clash:
        raise BridgeError("config", f"options repeat the argument(s) {', '.join(clash)}",
                          details={"options": clash})
    kwargs = {"name": name, "kind": kind, "temporal": temporal, "key": key,
              "variables": _variables_arg(args), "metadata": metadata, "root": root,
              "overwrite": bool_arg(args, "overwrite", False), **options}
    _bind(api.register, paths, **kwargs)
    ctx.log(f"[spacescans.bridge] dataset-register: {kind}, {temporal}, {len(paths)} file(s)")
    rec = api.register(paths, **kwargs)
    return {"dataset": record_dict(rec), "root": resolved_root(root)}, []


def cmd_dataset_list(args: dict, ctx: Any) -> Outcome:
    """Every registered dataset of the registry."""
    api = registry_api()
    root = path_arg(args, "root")
    return {"datasets": [record_dict(r) for r in api.list_datasets(root=root)],
            "root": resolved_root(root)}, []


def cmd_dataset_get(args: dict, ctx: Any) -> Outcome:
    """One registered dataset, by name or id."""
    api = registry_api()
    ref = str_arg(args, "name_or_id", required=True)
    root = path_arg(args, "root")
    return {"dataset": record_dict(_get(api, ref, root)), "root": resolved_root(root)}, []


def cmd_dataset_remove(args: dict, ctx: Any) -> Outcome:
    """Remove a registered dataset (its record and prepared files; not the user's originals)."""
    api = registry_api()
    ref = str_arg(args, "name_or_id", required=True)
    root = path_arg(args, "root")
    rec = _get(api, ref, root)
    try:
        removed = api.remove_dataset(ref, root=root)
    except LookupError as e:
        if isinstance(getattr(e, "kind", None), str):
            raise
        raise _unknown(ref, root) from None
    out = removed if removed is not None and not isinstance(removed, bool) else rec
    return {"dataset": record_dict(out), "root": resolved_root(root)}, []


# --- registered names in link and coverage -------------------------------------------

def registered_names(args: Mapping[str, Any]) -> list[str]:
    """The ``datasets`` argument of ``link`` and ``coverage``: registered names or ids,
    each once, in the order given (a single string counts as a list of one)."""
    return list(dict.fromkeys(str_list_arg(args, "datasets") or []))


def link_custom(definitions: Mapping[str, Any] | None, names: Sequence[str]) -> Any:
    """The ``custom`` value for ``orchestrate.link`` (and the coverage estimate).

    Without registered ``names`` it is ``definitions`` unchanged (variable key ->
    definition, as before phase D). With names it is the list form of phase D
    (``custom=[...]``): the registered names or ids, then the in-memory
    definitions, each of which carries its own ``variable_key``.
    """
    if not names:
        return definitions
    return [*names, *(definitions or {}).values()]


COMMANDS = {
    "dataset-register": cmd_dataset_register,
    "dataset-list": cmd_dataset_list,
    "dataset-get": cmd_dataset_get,
    "dataset-remove": cmd_dataset_remove,
}
