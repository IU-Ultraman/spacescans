"""Record what spacescans-web's own code does, for the catalog parity tests.

    python -B tests/catalog/web_probe.py --web <spacescans-web checkout> [--texts] [--out FILE]

Imports the web's runner modules, ``variable_registry`` and
``feature_dictionary`` from the checkout and calls them: every runner's
``plan()``, ``render_yaml()`` (into a temporary task directory),
``_cache_key()`` and ``merge_results()`` (with ``_merge.write_partial``
captured), over the scenarios below. The checkout is only read: ``-B`` and
``sys.dont_write_bytecode`` keep ``__pycache__`` out of it, and ``app.config``
is replaced by a stub whose paths point into the temporary directory, so no
setting, data root or cache of the checkout is touched.

The output (JSON on stdout or ``--out``) is the golden file
``tests/fixtures/catalog/web_parity.json``; ``--texts`` adds the rendered YAML
text next to each hash (for readable diffs in the live test). Regenerate it
with the package's dev environment (needs ``jsonschema``, which the web's
``variable_registry`` imports, and ``pyreadr``, which its version probe
requires).
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import inspect
import json
import sys
import tempfile
import types
from collections import OrderedDict
from pathlib import Path

sys.dont_write_bytecode = True

TASK_NAME = "task-0123456789abcdef"     # name suffix: _task_89abcdef
RUN_TOKEN = "{RUN}"
INPUT_BYTES = b"spacescans catalog parity probe\n"   # stands in for input.parquet
BUFFERS = ((270, 25), (500, 50), (1000, 25))

# --- custom-exposome definitions (shapes of custom_exposomes.create/create_raster)
_POLY = {
    "experiment": "custom", "geometry": "polygon", "spatial_method": "areal",
    "coverage_region": "CONUS", "variable_type": "continuous", "display_unit": "unitless",
    "description": "probe dataset", "data_source": "Uploaded by user: probe.csv",
}


def _poly(label, boundary, join_col, key_col, cols, *, year_col=None, years=None, path):
    d = dict(_POLY, label=label, boundary=boundary, join_col=join_col, key_col=key_col,
             value_cols=list(cols), year_col=year_col, values_path=path,
             temporal="yearly" if year_col else "static")
    if years is not None:
        d["coverage_years"] = list(years)
    return d


def _raster(label, grid_hash, rasters, cols, *, yearly):
    years = [r[1] for r in rasters if r[1] is not None]
    return {
        **_POLY, "label": label, "geometry": "raster", "spatial_method": "grid",
        "boundary": "Point", "join_col": "grid_id", "key_col": "grid_id",
        "year_col": "year" if yearly else None, "values_path": None,
        "value_cols": list(cols), "bands": list(range(1, len(cols) + 1)),
        "grid_hash": grid_hash,
        "rasters": [{"path": p, "year": y} for p, y in rasters],
        "coverage_years": [min(years), max(years)] if yearly else [2013, 2019],
        "temporal": "yearly" if yearly else "static",
    }


CUSTOM = {
    "custom_tracta": _poly("Tract A", "Tract", "GEOID10", "tract", ["a1", "a2"],
                           path="/srv/custom/user-1/tracta/values.csv"),
    "custom_tractb": _poly("Tract B", "Tract", "GEOID10", "GEOID", ["b1"], year_col="year",
                           years=[2014, 2017], path="/srv/custom/user-1/tractb/values.csv"),
    "custom_bgy": _poly("BG yearly", "BG", "GEOID10", "bg", ["v"], year_col="yr",
                        years=[2013, 2019], path="/srv/custom/user-1/bgy/values.csv"),
    "custom_zip": _poly("ZIP", "ZCTA5", "ZCTA5CE10", "zcta", ["z"],
                        path="/srv/custom/user-2/zip/values.csv"),
    "custom_cty": _poly("County", "County", "GEOID10", "fips", ["c1", "c2", "c3"],
                        year_col="year", path="/srv/custom/user-2/cty/values.csv"),
    "custom_nocov": _poly("No coverage", "Tract", "GEOID10", "t", ["n"],
                          path="/srv/custom/user-2/nocov/values.csv"),
    "custom_rs": _raster("Raster static", "g1aaaa11",
                         [("/srv/custom/user-3/rs/rasters/static.tif", None)], ["px"],
                         yearly=False),
    "custom_ry": _raster("Raster yearly", "g2bbbb22",
                         [("/srv/custom/user-3/ry/rasters/2015.tif", 2015),
                          ("/srv/custom/user-3/ry/rasters/2016.tif", 2016)],
                         ["b1", "b2"], yearly=True),
    "custom_rs2": _raster("Raster same grid", "g1aaaa11",
                          [("/srv/custom/user-3/rs2/rasters/2018.tif", 2018)], ["q"],
                          yearly=True),
    "custom_parcel": dict(_poly("Parcel", "Parcel", "GEOID10", "p", ["x"],
                                path="/srv/custom/user-4/parcel/values.csv")),
}
CUSTOM["custom_nocov"].pop("coverage_years", None)

_SINGLE = ("tiger_proximity", "nhd_bluespace", "noise", "vnl", "temis", "fara_tract",
           "acag", "faqsd")
PLAN_SCENARIOS: list[tuple[str, list[str], object]] = [
    ("bg_ndi_wi", ["ndi"], None),
    ("bg_ndi_wi", ["walkability"], None),
    ("bg_ndi_wi", ["ndi", "walkability"], None),
    ("bg_ndi_wi", ["walkability", "ndi"], None),
    ("bg_ndi_wi", ["ndi", "ndi"], None),
    ("bg_ndi_wi", [], None),
    ("bg_ndi_wi", ["ndi", "pm25"], None),
    ("bg_ndi_wi", ["cbp_zcta5"], None),
    ("zcta5_cbp", ["cbp_zcta5"], None),
    ("zcta5_cbp", ["ndi"], None),
    ("zcta5_cbp", [], None),
    *[(e, [e], None) for e in _SINGLE],
    *[(e, ["ndi"], None) for e in _SINGLE],
    *[(e, [], None) for e in _SINGLE],
    ("custom", ["custom_tracta"], "all"),
    ("custom", ["custom_tracta", "custom_tractb"], "all"),
    ("custom", ["custom_tracta", "custom_bgy"], "all"),
    ("custom", ["custom_bgy", "custom_tracta", "custom_zip", "custom_cty"], "all"),
    ("custom", ["custom_nocov"], "all"),
    ("custom", ["custom_rs"], "all"),
    ("custom", ["custom_ry"], "all"),
    ("custom", ["custom_rs", "custom_rs2"], "all"),
    ("custom", ["custom_rs2", "custom_rs"], "all"),
    ("custom", ["custom_tracta", "custom_ry", "custom_cty"], "all"),
    ("custom", ["custom_tracta", "custom_tracta"], "all"),
    ("custom", [], "all"),
    ("custom", ["custom_missing"], "all"),
    ("custom", ["custom_parcel"], "all"),
    ("custom", ["custom_tracta"], ["not", "a", "mapping"]),
]

GROUP_SCENARIOS: list[tuple[list[str], bool]] = [
    (["ndi"], False),
    (["walkability", "ndi"], False),
    (["faqsd", "acag", "ndi", "fara_tract"], False),
    (["faqsd", "temis", "vnl", "noise", "nhd_bluespace", "tiger_proximity", "cbp_zcta5",
      "walkability", "ndi", "acag", "fara_tract"], False),
    ([], False),
    (["ndi", "pm25", "aaa"], False),
    (["custom_tracta", "ndi", "custom_rs"], True),
    (["custom_rs", "faqsd"], True),
]

FEATURE_SCENARIOS: list[list[str]] = [
    ["pid", "startDate", "endDate", "longitude", "latitude", "ndi", "NatWalkInd"],
    ["faqsd_pm25", "pm25", "dist_pri", "value", "unknown_col", "l50dba_exi"],
    [],
    ["r_religious", "r_civic", "uvief", "LILATracts_1And10", "dist_coast_m", "ss_nbm"],
]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _web_commit(web: Path) -> str:
    git = web / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head
        ref = head[5:]
        loose = git / ref
        if loose.exists():
            return loose.read_text().strip()
        for line in (git / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
    except OSError:
        pass
    return "unknown"


def _install_web(web: Path, tmp: Path) -> dict[str, types.ModuleType]:
    backend = web / "backend"
    sys.path.insert(0, str(backend))
    import app

    data_root = tmp / "data-root"           # empty: every pre-flight short-circuits
    data_root.mkdir()
    stub = types.ModuleType("app.config")
    stub.settings = types.SimpleNamespace(
        SPACESCANS_CONFIG_TEMPLATES_DIR=web / "configs",
        SPACESCANS_DATA_DIR=data_root,
        DATA_DIR=tmp / "app-data",
        TASKS_DIR=tmp / "app-data" / "tasks",
        C3_CACHE_DIR=tmp / "app-data" / "c3_cache",
        SPACESCANS_PIPELINE_PYTHON=Path("/nonexistent/python"),
        SPACESCANS_PIPELINE_CLI=Path("/nonexistent/spacescans"),
        PIPELINE_STEP_TIMEOUT_SECONDS=1800,
    )
    sys.modules["app.config"] = stub
    app.config = stub

    import importlib

    mods = {}
    for name in ("bg_ndi_wi", "zcta5_cbp", *_SINGLE, "custom"):
        mods[name] = importlib.import_module(f"app.experiments.{name}")
    mods["_merge"] = importlib.import_module("app.experiments._merge")
    mods["variable_registry"] = importlib.import_module("app.variable_registry")
    mods["feature_dictionary"] = importlib.import_module("app.feature_dictionary")
    for name, mod in mods.items():
        src = getattr(mod, "__file__", "") or ""
        assert src.startswith(str(backend)), f"{name} imported from {src}, not the checkout"
    return mods


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _custom_overlay(spec):
    if spec == "all":
        return copy.deepcopy(CUSTOM)
    return copy.deepcopy(spec)


def _probe_plans(mods, tmp: Path, texts: bool) -> list[dict]:
    custom = mods["custom"]
    # The raster value table needs real rasters and C3 weights; render only
    # needs its path, which is what the real function returns.
    custom._materialise_raster_values = (
        lambda task_dir, var_key, definition, c3_parquet:
        task_dir / "output" / f"values_{var_key}.parquet"
    )
    input_parquet = tmp / "input.parquet"
    input_parquet.write_bytes(INPUT_BYTES)

    out = []
    for experiment, keys, overlay_spec in PLAN_SCENARIOS:
        mod = mods[experiment]
        config: dict = {"variables": list(keys)}
        if overlay_spec is not None:
            config["custom_variables"] = _custom_overlay(overlay_spec)
        rec = {"experiment": experiment, "keys": list(keys),
               "overlay": overlay_spec if overlay_spec != "all" else "all"}
        try:
            steps = mod.plan(copy.deepcopy(config))
        except Exception as exc:  # noqa: BLE001  the error is the result
            rec["error"] = _err(exc)
            out.append(rec)
            continue
        rec["steps"] = [{"name": s.name, "template": s.template_relpath, "is_c3": s.is_c3}
                        for s in steps]
        rec["renders"], rec["cache_keys"] = {}, {}
        if texts:
            rec["texts"] = {}
        for buf, res in BUFFERS:
            combo = f"{buf}/{res}"
            task_dir = tmp / f"run-{len(out)}-{buf}-{res}" / TASK_NAME
            task_dir.mkdir(parents=True)
            user_config = {**copy.deepcopy(config), "buffer": {"size": buf, "raster_res_m": res}}
            renders, keys_out, step_texts = {}, {}, {}
            for step in steps:
                path = mod.render_yaml(step, task_dir, user_config)
                text = Path(path).read_text().replace(str(task_dir), RUN_TOKEN)
                renders[step.name] = _sha(text.encode())
                step_texts[step.name] = text
                if step.is_c3:
                    if experiment == "custom":
                        tag = custom._boundary_tag_for_step(step)
                        keys_out[step.name] = custom._cache_key(input_parquet, tag, user_config)
                    else:
                        keys_out[step.name] = mod._cache_key(input_parquet, step, user_config)
            rec["renders"][combo] = renders
            rec["cache_keys"][combo] = keys_out
            if texts:
                rec["texts"][combo] = step_texts
        out.append(rec)
    return out


def _function(mod, name: str) -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise LookupError(f"{mod.__name__}.{name} not found")


def _cache_meta(mod, c3_steps) -> dict:
    """Evaluate the boundary= of run()'s _write_cache_meta(...) for each C3 step."""
    calls = [n for n in ast.walk(_function(mod, "run"))
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_write_cache_meta"]
    assert len(calls) == 1, f"{mod.__name__}: expected one _write_cache_meta call"
    kw = {k.arg: k.value for k in calls[0].keywords}
    expr = compile(ast.Expression(kw["boundary"]), mod.__name__, "eval")
    return {
        step.name: {"boundary": eval(expr, vars(mod), {"step": step}),
                    "raster_res_m": "raster_res_m" in kw,
                    "fields": sorted(kw)}
        for step in c3_steps
    }


def _probe_modules(mod) -> list[str]:
    found = []
    tree = ast.parse(inspect.getsource(mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_sanity_check"):
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom) and sub.module:
                    found += [f"{sub.module}.{alias.name}" for alias in sub.names]
    return found


def _cohort_columns(mods, name: str) -> list[str]:
    run = _function(mods[name], "run")
    imports = [n for n in ast.walk(run) if isinstance(n, ast.ImportFrom)
               and n.module == "app.experiments.zcta5_cbp"
               and any(a.name == "csv_to_parquet" for a in n.names)]
    source = mods["zcta5_cbp"] if imports else mods[name]
    return list(source._FIPS_STR_COLS)


def _probe_runners(mods, plans, tmp: Path) -> dict:
    import pandas as pd

    merge = mods["_merge"]
    captured: dict = {}
    original = merge.write_partial
    merge.write_partial = lambda **kw: captured.update(kw) or Path("unused")
    out = {}
    try:
        for name in ("bg_ndi_wi", "zcta5_cbp", *_SINGLE, "custom"):
            mod = mods[name]
            c3_steps = {}
            for rec in plans:
                if rec["experiment"] == name and "steps" in rec:
                    config = {"variables": rec["keys"]}
                    if rec["overlay"] is not None:
                        config["custom_variables"] = _custom_overlay(rec["overlay"])
                    for step in mod.plan(config):
                        if step.is_c3:
                            c3_steps.setdefault(step.name, step)
            keys = {"bg_ndi_wi": ["walkability", "ndi"], "zcta5_cbp": ["cbp_zcta5"],
                    "custom": ["custom_tracta", "custom_rs"]}.get(name, [name])
            task_dir = tmp / f"merge-{name}" / TASK_NAME
            (task_dir / "output").mkdir(parents=True)
            parquet = task_dir / "output" / "c4_tract_faqsd.parquet"
            if name == "faqsd":
                pd.DataFrame({"PATID": ["P1"], "geoid": [0], "o3": [1.0], "pm25": [2.0],
                              "other": [3.0]}).to_parquet(parquet, index=False)
            captured.clear()
            mod.merge_results(task_dir, variables=list(keys))
            rec = {
                "merge_keys": keys,
                "experiment_key": captured["experiment_key"],
                "variables": list(captured["variables"]),
                "parquet_map": dict(captured["parquet_map"]),
                "cohort_str_columns": _cohort_columns(mods, name),
                "episode_grouping_modules": _probe_modules(mod),
                "cache_meta": _cache_meta(mod, list(c3_steps.values())),
            }
            if name == "faqsd":
                rec["faqsd_columns_after_merge"] = list(pd.read_parquet(parquet).columns)
            out[name] = rec
    finally:
        merge.write_partial = original
    return out


def _probe_registry(mods, tmp: Path) -> dict:
    vr = mods["variable_registry"]
    task_dir = tmp / "overlay-task"
    task_dir.mkdir()
    (task_dir / "config.json").write_text(json.dumps({"custom_variables": CUSTOM}))
    out = {"payload": vr.load_variables(force=True),
           "experiments": vr.list_experiments(),
           "overlay_payload": vr.load_variables(task_dir=task_dir),
           "groups": []}
    for keys, with_overlay in GROUP_SCENARIOS:
        rec = {"keys": keys, "overlay": with_overlay}
        try:
            rec["result"] = vr.variables_by_experiment(
                list(keys), task_dir=task_dir if with_overlay else None)
        except Exception as exc:  # noqa: BLE001
            rec["error"] = _err(exc)
        out["groups"].append(rec)
    return out


def _probe_features(mods, tmp: Path) -> list[dict]:
    fd = mods["feature_dictionary"]
    out = []
    for i, cols in enumerate(FEATURE_SCENARIOS):
        path = fd.write_csv(list(cols), tmp / f"features-{i}" / "feature_dictionary.csv")
        out.append({"columns": cols, "rows": fd.rows_for(list(cols)),
                    "csv": path.read_bytes().decode("utf-8")})
    return out


def probe(web: Path, *, texts: bool = False) -> dict:
    with tempfile.TemporaryDirectory(prefix="ss-catalog-probe-") as td:
        tmp = Path(td)
        mods = _install_web(web, tmp)
        plans = _probe_plans(mods, tmp, texts)
        data = web / "backend" / "app" / "data"
        result = OrderedDict(
            web_commit=_web_commit(web),
            input_sha256=_sha(INPUT_BYTES),
            task_name=TASK_NAME,
            buffers=[f"{b}/{r}" for b, r in BUFFERS],
            custom_definitions=CUSTOM,
            files={name: _sha((data / name).read_bytes()) for name in (
                "variable_metadata.json", "variable_metadata.schema.json",
                "feature_dictionary.json")},
            templates={f"{p.parent.name}/{p.name}": _sha(p.read_bytes())
                       for p in sorted((web / "configs").glob("c[34]/*.yaml"))},
            registry=_probe_registry(mods, tmp),
            features=_probe_features(mods, tmp),
            runners=_probe_runners(mods, plans, tmp),
            plans=plans,
        )
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--web", type=Path, required=True, help="spacescans-web checkout")
    ap.add_argument("--texts", action="store_true", help="include rendered YAML text")
    ap.add_argument("--out", type=Path, help="write JSON here instead of stdout")
    args = ap.parse_args(argv)
    text = json.dumps(probe(args.web.resolve(), texts=args.texts), indent=1) + "\n"
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
