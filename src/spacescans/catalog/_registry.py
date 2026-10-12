"""The variable registry: ``variables.json``, its schema, and the query helpers.

Ported from spacescans-web ``backend/app/variable_registry.py`` (web commit
3991c3b): ``load_variables`` (382-431), ``_with_overlay`` (434-446),
``get_variable`` (449-454), ``variables_by_experiment`` (457-478) and
``list_experiments`` (481-487).

What did not move, on purpose:

* the eight data-presence pre-flights (72-358) - ``spacescans.data.status()``
  reports data readiness per variable instead of failing the whole catalog;
* the pipeline-version probe (35-61) - ``spacescans.capabilities()`` (U6);
* reading a task's overlay from its ``config.json`` (``_task_overlay``) - the
  caller passes the overlay mapping in.

``variables.json`` is a byte-for-byte copy of the web's
``backend/app/data/variable_metadata.json`` and ``variables.schema.json`` of its
schema. The schema is enforced here with a small stdlib validator so that a base
install (no ``jsonschema``) can load the catalog; the dev test-suite checks that
it agrees with ``jsonschema`` on the shipped file and on broken variants.
"""
from __future__ import annotations

import copy
import json
import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

_PACKAGE = "spacescans.catalog"
VARIABLES_FILE = "variables.json"
SCHEMA_FILE = "variables.schema.json"
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})


class CatalogError(ValueError):
    """The catalog file, or an overlay passed in, is not a valid catalog."""


@dataclass(frozen=True)
class Variable:
    """One catalog entry. ``raw`` is the entry exactly as stored (a copy).

    The typed fields are conveniences derived from ``raw``; anything the
    schema makes optional (or an overlay omits) is ``None``. The web's
    ``/api/variables`` keeps serving ``raw``.
    """

    key: str
    label: str | None
    description: str | None
    experiment: str | None
    spatial_method: str | None
    boundary: str | None
    coverage_years: tuple[int, ...]
    coverage_region: str | None
    temporal: str | None
    value_cols: tuple[str, ...]
    data_source: str | None
    variable_type: str | None
    display_unit: str | None
    ontology_id: str | None
    raw: Mapping[str, Any]

    def __hash__(self) -> int:  # raw is a dict; equal variables share a key
        return hash(self.key)

    @classmethod
    def from_entry(cls, key: str, entry: Mapping[str, Any]) -> Variable:
        years = entry.get("coverage_years") or ()
        return cls(
            key=key,
            label=entry.get("label"),
            description=entry.get("description"),
            experiment=entry.get("experiment"),
            spatial_method=entry.get("spatial_method"),
            boundary=entry.get("boundary"),
            coverage_years=tuple(int(y) for y in years),
            coverage_region=entry.get("coverage_region"),
            temporal=entry.get("temporal"),
            value_cols=tuple(entry.get("value_cols") or ()),
            data_source=entry.get("data_source"),
            variable_type=entry.get("variable_type"),
            display_unit=entry.get("display_unit"),
            ontology_id=entry.get("ontology_id"),
            raw=copy.deepcopy(dict(entry)),
        )


# ---------------------------------------------------------------------------
# validation (mirrors variables.schema.json; see module docstring)
# ---------------------------------------------------------------------------

_KEY_RE = r"^[a-z][a-z0-9_]*$"
_UNIT_RE = r"^[\x20-\x7E]+$"
_REQUIRED = (
    "label", "description", "boundary", "coverage_years", "coverage_region",
    "experiment", "variable_type", "display_unit", "value_cols",
)
_ALLOWED = frozenset(_REQUIRED) | {"spatial_method", "ontology_id", "data_source", "temporal"}
_ENUMS = {
    "boundary": ("Point", "BG", "ZCTA5", "Tract", "County"),
    "spatial_method": ("areal", "grid", "proximity"),
    "coverage_region": ("CONUS", "US", "AK_HI"),
    "temporal": ("static", "yearly"),
    "variable_type": ("categorical", "continuous"),
}


def _is_int(value: Any) -> bool:
    # JSON Schema "integer": no booleans; a float with an integral value counts.
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


def _string(where: str, value: Any, *, min_len: int = 0, max_len: int | None = None,
            pattern: str | None = None) -> None:
    if not isinstance(value, str):
        raise CatalogError(f"{where}: expected a string, got {type(value).__name__}")
    if len(value) < min_len:
        raise CatalogError(f"{where}: shorter than {min_len} character(s)")
    if max_len is not None and len(value) > max_len:
        raise CatalogError(f"{where}: longer than {max_len} characters")
    if pattern is not None and re.search(pattern, value) is None:
        raise CatalogError(f"{where}: {value!r} does not match {pattern}")


def _validate_entry(key: str, entry: Any) -> None:
    where = f"variables.{key}"
    if not isinstance(entry, dict):
        raise CatalogError(f"{where}: expected an object")
    missing = [f for f in _REQUIRED if f not in entry]
    if missing:
        raise CatalogError(f"{where}: missing required field(s) {missing}")
    extra = sorted(set(entry) - _ALLOWED)
    if extra:
        raise CatalogError(f"{where}: unknown field(s) {extra}")
    for field_name, allowed in _ENUMS.items():
        if field_name in entry:
            value = entry[field_name]
            if not isinstance(value, str):
                raise CatalogError(f"{where}.{field_name}: expected a string")
            if value not in allowed:
                raise CatalogError(f"{where}.{field_name}: {value!r} not one of {list(allowed)}")
    _string(f"{where}.label", entry["label"], min_len=1, max_len=80)
    _string(f"{where}.description", entry["description"], min_len=1, max_len=400)
    _string(f"{where}.experiment", entry["experiment"], pattern=_KEY_RE)
    _string(f"{where}.display_unit", entry["display_unit"], max_len=50, pattern=_UNIT_RE)
    for optional in ("ontology_id", "data_source"):
        if optional in entry:
            _string(f"{where}.{optional}", entry[optional], min_len=1)
    years = entry["coverage_years"]
    if not isinstance(years, list) or len(years) != 2:
        raise CatalogError(f"{where}.coverage_years: expected a list of two years")
    for year in years:
        if not _is_int(year) or not 1900 <= year <= 2100:
            raise CatalogError(f"{where}.coverage_years: {year!r} is not a year in 1900-2100")
    cols = entry["value_cols"]
    if not isinstance(cols, list) or not cols:
        raise CatalogError(f"{where}.value_cols: expected a non-empty list")
    for col in cols:
        _string(f"{where}.value_cols[]", col, min_len=1)


def validate_payload(payload: Any, *, experiments: Sequence[str] | None = None) -> None:
    """Raise :class:`CatalogError` unless ``payload`` is a valid catalog.

    Checks what the web checked at load time: the JSON schema, the
    ``schema_version`` gate, and that every variable names an experiment with
    a recipe (the web checked for a runner module; ``experiments`` overrides
    the list, default: :func:`spacescans.catalog.recipes.all_recipes`).
    """
    if not isinstance(payload, dict):
        raise CatalogError("catalog: expected a JSON object")
    missing = [f for f in ("schema_version", "variables") if f not in payload]
    if missing:
        raise CatalogError(f"catalog: missing required field(s) {missing}")
    extra = sorted(set(payload) - {"schema_version", "variables"})
    if extra:
        raise CatalogError(f"catalog: unknown top-level field(s) {extra}")
    version = payload["schema_version"]
    if not _is_int(version) or version not in SUPPORTED_SCHEMA_VERSIONS:
        raise CatalogError(
            f"unsupported schema_version: {version!r} "
            f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )
    variables = payload["variables"]
    if not isinstance(variables, dict) or not variables:
        raise CatalogError("catalog: 'variables' must be a non-empty object")
    for key, entry in variables.items():
        if not isinstance(key, str) or re.search(_KEY_RE, key) is None:
            raise CatalogError(f"catalog: variable key {key!r} does not match {_KEY_RE}")
        _validate_entry(key, entry)
    if experiments is None:
        from spacescans.catalog.recipes import all_recipes
        experiments = list(all_recipes())
    known = set(experiments)
    for key, entry in variables.items():
        if entry["experiment"] not in known:
            raise CatalogError(
                f"variable {key!r} references unknown experiment "
                f"{entry['experiment']!r} (known: {sorted(known)})"
            )


# ---------------------------------------------------------------------------
# loading and queries
# ---------------------------------------------------------------------------

def _read_json(name: str) -> Any:
    text = resources.files(_PACKAGE).joinpath(name).read_text(encoding="utf-8")
    return json.loads(text, object_pairs_hook=OrderedDict)


@lru_cache(maxsize=1)
def _shipped() -> OrderedDict[str, Any]:
    payload = _read_json(VARIABLES_FILE)
    validate_payload(payload)
    return payload


def schema() -> dict:
    """The JSON schema of ``variables.json`` (a fresh copy)."""
    return _read_json(SCHEMA_FILE)


def _overlay_entries(overlay: Mapping[str, Any] | None) -> OrderedDict[str, dict]:
    if not overlay:
        return OrderedDict()
    if not isinstance(overlay, Mapping):
        raise CatalogError("overlay must map variable keys to definitions")
    out: OrderedDict[str, dict] = OrderedDict()
    for key, entry in overlay.items():
        if not isinstance(entry, Mapping):
            raise CatalogError(f"overlay entry {key!r} is not a mapping")
        out[key] = copy.deepcopy(dict(entry))
    return out


def payload(*, overlay: Mapping[str, Any] | None = None) -> OrderedDict[str, Any]:
    """The catalog as the web's ``load_variables`` returns it (a fresh copy).

    ``overlay`` (custom-exposome definitions keyed by variable key, the web's
    ``config.json['custom_variables']``) is merged after the shipped entries
    with ``dict.update`` semantics, exactly like ``_with_overlay``. Overlay
    entries are not validated against the schema (the web never did: they are
    a documented superset of it).
    """
    merged = copy.deepcopy(_shipped())
    merged["variables"].update(_overlay_entries(overlay))
    return merged


def variables(*, overlay: Mapping[str, Any] | None = None) -> OrderedDict[str, Variable]:
    """Every variable, in catalog file order, then the overlay's."""
    entries = payload(overlay=overlay)["variables"]
    return OrderedDict((key, Variable.from_entry(key, entry)) for key, entry in entries.items())


def variable(key: str, *, overlay: Mapping[str, Any] | None = None) -> Variable:
    """One variable; ``KeyError(key)`` when no catalog (or overlay) knows it."""
    entries = payload(overlay=overlay)["variables"]
    try:
        entry = entries[key]
    except KeyError:
        raise KeyError(key) from None
    return Variable.from_entry(key, entry)


def _key_list(keys: Sequence[str]) -> list[str]:
    if isinstance(keys, (str, bytes)):
        raise TypeError("keys must be a sequence of variable keys, not a single string")
    return list(keys)


def group(keys: Sequence[str], *, overlay: Mapping[str, Any] | None = None
          ) -> OrderedDict[str, list[str]]:
    """Group ``keys`` by experiment, in catalog file order (the dispatcher's order).

    Raises ``KeyError`` naming every key no catalog knows (the web used to drop
    them silently; 3991c3b raises, and so does this).
    """
    keys = _key_list(keys)
    entries = payload(overlay=overlay)["variables"]
    out: OrderedDict[str, list[str]] = OrderedDict()
    for var_key, entry in entries.items():
        if var_key not in keys:
            continue
        out.setdefault(entry["experiment"], []).append(var_key)
    unknown = [k for k in keys if k not in entries]
    if unknown:
        raise KeyError(f"selection names variable(s) no catalog knows: {sorted(unknown)}")
    return out


def experiments(*, overlay: Mapping[str, Any] | None = None) -> list[str]:
    """Experiments in catalog file order, each once.

    Without an overlay this is the web's ``list_experiments()``; ``custom``
    appears only when an overlay brings custom variables.
    """
    seen: list[str] = []
    for entry in payload(overlay=overlay)["variables"].values():
        if entry["experiment"] not in seen:
            seen.append(entry["experiment"])
    return seen
