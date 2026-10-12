"""spacescans.catalog works on a base install: no geo stack, no optional extras."""
from __future__ import annotations

import json
import os
import subprocess
import sys

BLOCKED = ("geopandas", "rasterio", "shapely", "exactextract", "pyogrio", "fiona", "pyproj",
           "pyreadr", "pyhdf", "netCDF4", "xarray")
HEAVY = ("pandas", "numpy", "duckdb", "pyarrow")

SCRIPT = f"""
import importlib.abc, json, sys

BLOCKED = {BLOCKED!r}

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"blocked for this test: {{name}}")
        return None

sys.meta_path.insert(0, Block())

import spacescans.catalog as c
c.variables(); c.group(["ndi", "faqsd"]); c.experiments(); c.features()
c.feature_rows(["ndi"]); c.template("c4/bg_ndi_demo.yaml"); c.template_hashes()
for e in c.all_recipes():
    r = c.recipe(e)
    for k in r.variables:
        c.plan(e, [k])
loaded = sorted({{m.split(".")[0] for m in sys.modules}})
print(json.dumps(loaded))
"""


def _run() -> list[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True,
                          env=env, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_catalog_needs_no_optional_extra_and_no_dataframe_stack():
    loaded = _run()
    assert not set(loaded) & set(BLOCKED)
    assert not set(loaded) & set(HEAVY), (
        f"catalog pulled in {sorted(set(loaded) & set(HEAVY))}; keep it import-light")
