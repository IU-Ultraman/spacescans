"""``catalog``, ``coverage`` and ``custom-validate``.

``catalog`` lists the variables with what each needs (datasets from the data
manifest, templates and linkage patterns from the recipes, extras missing on
this install) and the feature dictionary; with ``data_dir`` it adds each
variable's data status. ``coverage`` is the pre-run estimate
(:func:`spacescans.orchestrate.compute_coverage`); its counts are episodes
(cohort rows). ``custom-validate`` builds a custom (upload-path) dataset
definition from a CSV or GeoTIFFs, or re-checks one that came back from the
client, and returns it unchanged in shape.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from spacescans.bridge.args import (
    bool_arg,
    choice_arg,
    int_arg,
    mapping_arg,
    path_arg,
    path_list_arg,
    str_arg,
    str_list_arg,
)
from spacescans.bridge.errors import BridgeError

Outcome = tuple[Any, list]


def custom_definitions(args: dict, *, files: bool, name: str = "custom") -> dict[str, dict] | None:
    """The ``custom`` argument: {variable_key: definition}, each repaired and checked.

    ``custom.check_definition`` undoes JSON round-trip damage (scalars for
    one-element lists, ``[]`` for ``{}``) and raises CustomExposomeError
    (kind ``config``) for anything the steps cannot use.
    """
    defs = mapping_arg(args, name)
    if not defs:
        return None
    from spacescans import custom
    return {key: custom.check_definition(d, key=key, files=files) for key, d in defs.items()}


def _walk_plugins(obj: Any, out: list[str]) -> None:
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if k in ("plugin", "reader") and isinstance(v, str):
                out.append(v)
            else:
                _walk_plugins(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_plugins(v, out)


def _unavailable_extras() -> dict[tuple[str, str], str | None]:
    """(kind, name) of every pattern/reader this install cannot load -> the extra it needs."""
    from spacescans.pipeline import registry
    out: dict[tuple[str, str], str | None] = {}
    for info in registry.available().get("unavailable", {}).values():
        provides = info.get("provides") or []
        if len(provides) == 2 and all(isinstance(x, str) for x in provides):
            out[(provides[0], provides[1])] = info.get("extra")
    return out


def _variable_row(key: str, var: Any, overlay: Mapping[str, Any] | None, manifest: Any,
                  boundary_datasets: Mapping[str, str], unavailable: Mapping) -> dict[str, Any]:
    from spacescans import catalog
    raw = dict(var.raw)
    is_custom = bool(overlay) and key in overlay
    steps = catalog.plan(var.experiment, [key], overlay=overlay)
    templates = [s.template for s in steps]
    patterns: list[str] = []
    plugins: list[str] = []
    for t in templates:
        cfg = catalog.template(t) or {}
        if cfg.get("linkage_pattern"):
            patterns.append(cfg["linkage_pattern"])
        _walk_plugins(cfg, plugins)
    patterns = list(dict.fromkeys(patterns))
    plugins = list(dict.fromkeys(plugins))
    missing = sorted({x for x in ([unavailable.get(("pattern", p)) for p in patterns if ("pattern", p) in unavailable]
                                  + [unavailable.get(("reader", r)) for r in plugins if ("reader", r) in unavailable])
                      if x}) if unavailable else []
    unloadable = [p for p in patterns if ("pattern", p) in unavailable] + \
                 [r for r in plugins if ("reader", r) in unavailable]
    if key in manifest.variables:
        requires = [list(req.any_of) for req in manifest.variables[key].requires]
    elif is_custom and raw.get("geometry") == "polygon" and raw.get("boundary") in boundary_datasets:
        requires = [[boundary_datasets[raw["boundary"]]]]
    else:
        requires = []
    renames = {} if is_custom else dict(catalog.recipe(var.experiment).renames)
    return {
        "key": key, "label": var.label, "description": var.description,
        "experiment": var.experiment, "spatial_method": var.spatial_method,
        "boundary": var.boundary, "coverage_years": list(var.coverage_years),
        "coverage_region": var.coverage_region, "temporal": var.temporal,
        "value_cols": list(var.value_cols),
        "columns": [renames.get(c, c) for c in var.value_cols],
        "data_source": var.data_source, "variable_type": var.variable_type,
        "display_unit": var.display_unit, "ontology_id": var.ontology_id,
        "custom": is_custom,
        "requires": requires,
        "datasets": list(dict.fromkeys(d for alt in requires for d in alt)),
        "steps": [s.name for s in steps], "templates": templates,
        "patterns": patterns, "plugins": plugins,
        "missing_extras": missing, "runnable_here": not unloadable,
    }


def cmd_catalog(args: dict, ctx) -> Outcome:
    """Variables, their requirements and the feature dictionary; data status with ``data_dir``."""
    from spacescans import catalog, data
    from spacescans._capabilities import catalog_info

    overlay = custom_definitions(args, files=False)
    keys = str_list_arg(args, "variables")
    data_dir = path_arg(args, "data_dir")
    all_vars = catalog.variables(overlay=overlay)
    if keys is not None:
        unknown = [k for k in keys if k not in all_vars]
        if unknown:
            raise BridgeError("config", f"unknown variable(s): {', '.join(unknown)}",
                              details={"unknown": unknown, "known": list(all_vars)})
        selected = {k: all_vars[k] for k in dict.fromkeys(keys)}
    else:
        selected = dict(all_vars)
    manifest = data.load_manifest()
    boundary_ds = data.boundary_datasets()
    try:
        unavailable = _unavailable_extras()
    except Exception:  # pragma: no cover - registry trouble must not hide the catalog
        unavailable = {}
    rows = [_variable_row(k, v, overlay, manifest, boundary_ds, unavailable) for k, v in selected.items()]
    value: dict[str, Any] = {
        "variables": rows,
        "experiments": catalog.experiments(overlay=overlay),
        "features": catalog.feature_rows(list(catalog.features())),
        "catalog": catalog_info(),
        "data_release": manifest.data_release,
    }
    warns: list = []
    if data_dir is not None:
        var_keys = [r["key"] for r in rows if r["key"] in manifest.variables]
        extra_ds = list(dict.fromkeys(d for r in rows if r["custom"] for d in r["datasets"]))
        st = data.status(var_keys, datasets=extra_ds, data_dir=data_dir)
        st_dict = st.to_dict()
        by_var = {v["variable"]: v for v in st_dict["variables"]}
        by_ds = {d["dataset"]: d for d in st_dict["datasets"]}
        for r in rows:
            if r["key"] in by_var:
                r["status"], r["usable"] = by_var[r["key"]]["status"], by_var[r["key"]]["usable"]
                r["next"] = by_var[r["key"]]["next"]
            elif r["datasets"]:
                ds = [by_ds[d] for d in r["datasets"] if d in by_ds]
                r["usable"] = all(d["usable"] for d in ds)
                r["status"] = "ready" if r["usable"] else next(d["status"] for d in ds if not d["usable"])
                r["next"] = "" if r["usable"] else ds[0].get("next", "")
            else:
                r["status"], r["usable"], r["next"] = "ready", True, ""
        value["status"] = st_dict
        warns = [{"code": "data_warning", "message": str(t)} for t in st_dict.get("warnings") or []]
    return value, warns


def cmd_coverage(args: dict, ctx) -> Outcome:
    """Pre-run coverage per variable: episodes inside the years and the region."""
    from spacescans import catalog
    from spacescans.bridge.cohorts import read_cohort
    from spacescans.orchestrate import compute_coverage

    path = path_arg(args, "cohort", required=True)
    keys = str_list_arg(args, "variables", required=True, allow_empty=False)
    overlay = custom_definitions(args, files=False)
    entries = catalog.variables(overlay=overlay)
    unknown = [k for k in keys if k not in entries]
    if unknown:
        raise BridgeError("config", f"unknown variable(s): {', '.join(unknown)}",
                          details={"unknown": unknown, "known": list(entries)})
    co = read_cohort(path)
    res = compute_coverage(co.table, list(dict.fromkeys(keys)), entries)
    out = []
    for key, v in res["variables"].items():
        out.append({"variable": key, "coverage_years": v["coverage_years"], "episodes": res["row_count"],
                    "episodes_in_time_window": v["patients_in_time_window"],
                    "episodes_in_region": v["patients_in_region"],
                    "episodes_covered": v["patients_covered"], "pct": v["coverage_pct"],
                    "warnings": v["warnings"], "boundary": v["boundary"],
                    "display_unit": v["display_unit"], "temporal": v["temporal"]})
    warns = list(co.warnings)
    for row in out:
        for text in row["warnings"]:
            warns.append({"code": "coverage", "variable": row["variable"], "message": f"{row['variable']}: {text}"})
    return {"episodes": co.rows, "patients": co.patients, "variables": out}, warns


def _options(args: dict) -> dict[str, Any]:
    opts = mapping_arg(args, "options") or {}
    merged = {**opts, **{k: v for k, v in args.items() if k not in ("options", "kind", "files", "definition", "key", "deep")}}
    return merged


def _str_map(opts: dict, name: str) -> dict[str, str] | None:
    value = opts.get(name)
    if value is None or value == []:
        return None
    if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
        raise BridgeError("config", f"option '{name}' must map value columns to strings")
    return dict(value)


def cmd_custom_validate(args: dict, ctx) -> Outcome:
    """Build a custom dataset definition (``kind`` polygon or raster), or check one (``check``).

    polygon: ``files`` = [CSV]; options ``boundary``, ``key_col``, ``value_cols``, ``year_col``,
    ``name`` (or ``label``), ``description``, ``labels``, ``units``.
    raster: ``files`` = GeoTIFFs; options ``years`` (parallel to files; omit for one static
    file), ``bands``, ``value_cols``, ``name``, ``description``, ``labels``, ``units``.
    check: ``definition`` (+ ``key``, ``deep``) as returned earlier.
    """
    from spacescans import custom
    kind = choice_arg(args, "kind", ("polygon", "raster", "check"), default="polygon")
    if kind == "check":
        d = mapping_arg(args, "definition")
        if d is None:
            raise BridgeError("config", "argument 'definition' is required for kind 'check'")
        out = custom.check_definition(d, key=str_arg(args, "key"), deep=bool_arg(args, "deep"))
        return {"kind": "check", "definition": out, "key": out.get("variable_key")}, []
    files = path_list_arg(args, "files", required=True)
    if not files:
        raise BridgeError("config", "argument 'files' must list at least one file")
    opts = _options(args)
    label = opts.get("name", opts.get("label"))
    if label is not None and not isinstance(label, str):
        raise BridgeError("config", "option 'name' must be a string")
    common = {"label": label, "labels": _str_map(opts, "labels"), "units": _str_map(opts, "units")}
    if opts.get("description") is not None:
        common["description"] = str(opts["description"])
    if kind == "polygon":
        if len(files) != 1:
            raise BridgeError("config", "a polygon dataset is defined from exactly one CSV file")
        d = custom.define_polygon(
            files[0], boundary=str_arg(opts, "boundary", required=True),
            key_col=str_arg(opts, "key_col", required=True),
            value_cols=str_list_arg(opts, "value_cols", required=True, allow_empty=False),
            year_col=str_arg(opts, "year_col"), **common)
    else:
        years = opts.get("years")
        if years is None or years == []:
            years = [None] * len(files)
        elif not isinstance(years, list):
            years = [years]
        if len(years) != len(files):
            raise BridgeError("config", f"'years' has {len(years)} entries for {len(files)} files")
        clean_years = [int_arg({"year": y}, "year") for y in years]
        bands = opts.get("bands")
        if bands is not None and not isinstance(bands, list):
            bands = [bands]
        if bands is not None:
            bands = [int_arg({"band": b}, "band", minimum=1) for b in bands]
        d = custom.define_raster(list(zip(files, clean_years)), bands=bands,
                                 value_cols=str_list_arg(opts, "value_cols"), **common)
    return {"kind": kind, "definition": d, "key": d["variable_key"]}, []


COMMANDS = {
    "catalog": cmd_catalog,
    "coverage": cmd_coverage,
    "custom-validate": cmd_custom_validate,
}
