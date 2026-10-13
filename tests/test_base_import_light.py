"""The tier-2 packages import, and run their base-install path, without any optional extra.

catalog, cohort, orchestrate, data, custom and bridge serve the R bridge and the web API,
where the [geo], [rda], [hdf4] and [nc] extras may be missing. A package may import an
extra inside the function that needs it, never at import time.

This is the only import-light test. It replaces the per-track copies from phase A:
tests/test_tier2_base_imports.py, tests/catalog/test_import_light.py, and the import tests
that were in tests/custom/test_custom_base_install.py, tests/datamod/test_data_cli.py and
tests/test_bridge.py. Functional base-install tests (the custom CSV path, the bridge's
base-only commands, the registry) stay with their packages.

Each package is checked in a fresh interpreter, so modules imported earlier in this
session cannot hide an import:

1. Import the package and every submodule. Submodules are found by walking the package,
   so a module another track adds is checked as soon as it lands. Then sys.modules must
   hold no optional extra, whether or not the extras are installed. It must also hold none
   of the base dependencies that the package promises to avoid: catalog and data stay free
   of the dataframe stack, and custom stays free of pandas.
2. Hide the extras, as on a base install, and run a short script through the package's
   base-install path. The avoided packages must still be absent afterwards.

A package that is not on this branch is skipped.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

# Top-level packages that only the optional extras install.
OPTIONAL_PACKAGES = (
    "geopandas", "rasterio", "shapely", "exactextract", "pyogrio", "pyproj", "fiona",
    "pyreadr", "pyhdf", "xarray", "netCDF4",
)
DATAFRAME_STACK = ("pandas", "numpy", "duckdb", "pyarrow")

FIX = Path(__file__).resolve().parent / "fixtures" / "web_parity"


@dataclass(frozen=True)
class Case:
    walk: bool = True        # import every submodule too
    avoid: tuple = ()        # base dependencies the package must not load either
    smoke: str = ""          # base-install path, run with the extras hidden (FIX, TMP given)


CASES = {
    "spacescans": Case(walk=False),
    "spacescans.catalog": Case(avoid=DATAFRAME_STACK, smoke="""
        import spacescans.catalog as c
        c.variables(); c.group(["ndi", "faqsd"]); c.experiments(); c.features()
        c.feature_rows(["ndi"]); c.template("c4/bg_ndi_demo.yaml"); c.template_hashes()
        for e in c.all_recipes():
            for k in c.recipe(e).variables:
                c.plan(e, [k])
        """),
    "spacescans.cohort": Case(walk=False, smoke="""
        import spacescans.cohort as cohort
        csv = FIX / "cohorts" / "iso.csv"
        info = cohort.to_pipeline_parquet(csv, TMP / "input.parquet", pid_as_str=True)
        assert info.rows == 59, info.rows
        assert cohort.validate(cohort.read_upload_csv(csv)).ok
        """),
    "spacescans.orchestrate": Case(smoke="""
        import spacescans.orchestrate as orch
        from spacescans import catalog
        cov = orch.compute_coverage(FIX / "cohorts" / "iso.csv", ["ndi"], catalog.variables())
        assert cov["row_count"] == 59, cov["row_count"]
        """),
    "spacescans.data": Case(avoid=DATAFRAME_STACK + ("pydantic", "yaml"), smoke="""
        import spacescans.data as d
        from spacescans.data.cli import add_data_parser, cmd_data
        r = d.status(["ndi", "acag"], data_dir=TMP)
        assert r.variables[0].status in ("missing", "action_required"), r.variables[0].status
        d.plan(["noise"], data_dir=TMP)
        """),
    "spacescans.custom": Case(avoid=("pandas",)),
    "spacescans.bridge": Case(),
}

# argv: package, case as JSON, TMP, FIX. Prints one JSON report on its last line.
SCRIPT = textwrap.dedent("""
    import importlib, importlib.util, json, pkgutil, sys, textwrap, traceback
    from importlib.abc import MetaPathFinder
    from pathlib import Path

    package, case = sys.argv[1], json.loads(sys.argv[2])
    TMP, FIX = Path(sys.argv[3]), Path(sys.argv[4])
    forbidden = set(case["optional"]) | set(case["avoid"])
    report = {"modules": [], "import_errors": {}, "light": {}, "smoke_error": None,
              "after_smoke": []}

    def loaded():
        return {m.split(".")[0] for m in sys.modules} & forbidden

    # 1. Import the package and its submodules; note which module first loads what.
    names = [package]
    try:
        root = importlib.import_module(package)
    except Exception:
        report["import_errors"][package] = traceback.format_exc()
        root = None
    if root is not None and case["walk"] and hasattr(root, "__path__"):
        def onerror(name):
            report["import_errors"][name] = traceback.format_exc()
        names += [m.name for m in pkgutil.walk_packages(root.__path__, package + ".",
                                                        onerror=onerror)
                  if m.name.rsplit(".", 1)[-1] != "__main__"]
    seen = set()
    for name in names:
        try:
            importlib.import_module(name)
        except Exception:
            report["import_errors"][name] = traceback.format_exc()
            continue
        report["modules"].append(name)
        new = sorted(loaded() - seen)
        if new:
            report["light"][name] = new
            seen.update(new)

    # 2. Hide the extras as on a base install and run the base-install path.
    if case["smoke"] and not report["import_errors"] and not report["light"]:
        hidden = set(case["optional"])

        class Hide(MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in hidden:
                    raise ModuleNotFoundError(f"No module named {name!r} (hidden: base install)",
                                              name=name.split(".")[0])
                return None

        sys.meta_path.insert(0, Hide())
        real_find_spec = importlib.util.find_spec
        importlib.util.find_spec = lambda n, *a, **k: (
            None if n.split(".")[0] in hidden else real_find_spec(n, *a, **k))
        try:
            exec(textwrap.dedent(case["smoke"]), {"FIX": FIX, "TMP": TMP})
        except Exception:
            report["smoke_error"] = traceback.format_exc()
        report["after_smoke"] = sorted(loaded())

    print(json.dumps(report))
""")


@pytest.mark.parametrize("package", sorted(CASES))
def test_package_imports_and_runs_without_optional_extras(package, tmp_path):
    if importlib.util.find_spec(package) is None:
        pytest.skip(f"{package} is not on this branch")
    case = CASES[package]
    spec = {"walk": case.walk, "avoid": list(case.avoid), "smoke": case.smoke,
            "optional": list(OPTIONAL_PACKAGES)}
    # The spacescans this session imports: this checkout's src/ (pytest's pythonpath) or the
    # installed package.
    import spacescans
    src = str(Path(spacescans.__file__).resolve().parents[1])
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join(p for p in (src, os.environ.get("PYTHONPATH")) if p)}
    proc = subprocess.run([sys.executable, "-c", SCRIPT, package, json.dumps(spec),
                           str(tmp_path), str(FIX)],
                          capture_output=True, text=True, env=env, cwd=str(tmp_path),
                          timeout=300, check=False)
    assert proc.returncode == 0, f"checker crashed:\n{proc.stderr}"
    report = json.loads(proc.stdout.strip().splitlines()[-1])

    assert not report["import_errors"], "import failed:\n" + "\n".join(
        f"{name}:\n{tb}" for name, tb in report["import_errors"].items())
    assert report["light"] == {}, (
        "importing loads packages it must not (optional extras, or "
        f"{list(case.avoid) or 'nothing else'}): {report['light']}")
    assert report["smoke_error"] is None, f"base-install path failed:\n{report['smoke_error']}"
    assert report["after_smoke"] == [], (
        f"the base-install path loaded {report['after_smoke']}")
    assert package in report["modules"]
