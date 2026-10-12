"""Modules used without the geo stack must not import an optional extra.

The tier-2 modules catalog, cohort, data and bridge must work with the base
install only, so the R bridge and the web API can use them where no [geo],
[rda], [hdf4] or [nc] extra is installed. They may import an extra lazily,
inside the function that needs it, but not at import time.

Each module is imported in a fresh interpreter, so modules imported earlier in
this session cannot hide an import. In the base jobs this also fails when a
module needs an extra just to import. A module not on this branch is skipped.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BASE_ONLY_MODULES = (
    "spacescans",
    "spacescans.catalog",
    "spacescans.cohort",
    "spacescans.data",
    "spacescans.bridge",
)

# Top-level packages that only the optional extras install.
OPTIONAL_PACKAGES = (
    "geopandas", "rasterio", "shapely", "exactextract", "pyogrio", "pyproj",
    "fiona", "pyreadr", "pyhdf", "xarray", "netCDF4",
)

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.mark.parametrize("module", BASE_ONLY_MODULES)
def test_module_does_not_import_optional_extras(module):
    if importlib.util.find_spec(module) is None:
        pytest.skip(f"{module} is not on this branch")
    code = (
        "import importlib, json, sys\n"
        f"importlib.import_module({module!r})\n"
        f"print(json.dumps(sorted(m for m in {OPTIONAL_PACKAGES!r} if m in sys.modules)))\n"
    )
    # Import this checkout's sources, as pytest's pythonpath setting does.
    path = [str(SRC), os.environ.get("PYTHONPATH", "")]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in path if p)}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=env, check=False)
    assert r.returncode == 0, f"import {module} failed:\n{r.stderr}"
    imported = json.loads(r.stdout.strip().splitlines()[-1])
    assert imported == [], f"import {module} also imports {imported}"
