"""spacescans.custom works with the base install.

CSV validation, definitions, planning, rendering and the boundary check need
nothing beyond the base dependencies; only the raster functions need the
``[geo]`` extra, and without it they raise MissingExtraError (an ImportError)
with the install hint, which the bridge reports as ``missing_extra``.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import numpy as np
import pytest

from spacescans import custom
from spacescans._extras import MissingExtraError

GEO = ("rasterio", "geopandas", "shapely", "pyproj", "exactextract", "fiona", "pyogrio")


def _run(code: str) -> str:
    proc = subprocess.run([sys.executable, "-c", textwrap.dedent(code)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_the_csv_path_runs_when_geo_libraries_cannot_be_imported(tmp_path):
    out = _run(f"""
        import sys
        class Block:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in {GEO!r}:
                    raise ImportError("blocked " + name)
                return None
        sys.meta_path.insert(0, Block())

        from pathlib import Path
        from spacescans import custom
        csv = b"tract,year,v\\n12073001100,2015,0.4\\n12073001200,2016,0.5\\n"
        assert custom.preview_csv(csv)["row_count"] == 2
        d = custom.define_polygon(csv, label="x", boundary="Tract", key_col="tract",
                                  value_cols=["v"], year_col="year",
                                  values_path="/data/values.csv")
        key = d["variable_key"]
        steps = custom.plan([key], {{key: d}})
        run = Path({str(tmp_path)!r})
        ctx = custom.Context(run_dir=run, cohort_parquet=run / "input.parquet")
        template = {{"name": "custom_areal", "linkage_pattern": "static_areal",
                     "source": {{}}, "buffer": {{}}, "exposure": {{}}, "output": {{}}}}
        cfg = custom.render(steps[1], template, ctx, variables=[key], definitions={{key: d}})
        assert cfg["linkage_pattern"] == "yearly_areal" and cfg["time"]["years"] == [2015, 2016]
        assert custom.prepare_step(steps[1], ctx, definitions={{key: d}}) == []
        assert [b["available"] for b in custom.provisioned_boundaries(run)] == [False] * 4
        print("ok")
    """)
    assert out == "ok"


@pytest.fixture
def no_rasterio(monkeypatch):
    """Make the extras gate see no rasterio, as on a base install."""
    import importlib.util
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: None if name == "rasterio" else real(name, *a, **k))


def test_raster_functions_need_the_geo_extra(no_rasterio):
    with pytest.raises(MissingExtraError, match=r"spacescans-pipeline\[geo\]"):
        custom.inspect_raster(b"not read")
    with pytest.raises(MissingExtraError):
        custom.preview_raster(b"not read")
    with pytest.raises(MissingExtraError):
        custom.extract_bands("x.tif", np.array([0]), bands=[1])
    with pytest.raises(MissingExtraError):
        custom.define_raster([("a.tif", b"not read", None)], label="x")
    assert issubclass(MissingExtraError, ImportError)
