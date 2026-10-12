"""spacescans.cohort and spacescans.orchestrate import on a base install.

Like test_pipeline_smoke.test_base_modules_import_without_optional_extras,
this blocks the optional extras in a subprocess, so an accidental top-level
geo import fails here even in an [all] environment. The ported functions
are exercised there too, not just imported.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "web_parity"


def test_cohort_and_orchestrate_need_only_base_dependencies(tmp_path):
    script = textwrap.dedent(
        f"""
        import sys
        from importlib.abc import MetaPathFinder

        _BLOCKED = {{"geopandas", "rasterio", "shapely", "exactextract",
                    "pyreadr", "pyhdf", "xarray", "netCDF4", "pyproj", "fiona",
                    "pyogrio"}}

        class _Blocker(MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name.split(".")[0] in _BLOCKED:
                    raise ModuleNotFoundError(
                        f"No module named {{name!r}} (blocked for base-install test)")
                return None

        sys.meta_path.insert(0, _Blocker())

        from pathlib import Path
        import spacescans.cohort as cohort
        import spacescans.orchestrate as orch
        from spacescans.orchestrate import merge, coverage  # noqa: F401

        fix = Path({str(FIX)!r})
        out = Path({str(tmp_path)!r}) / "input.parquet"
        info = cohort.to_pipeline_parquet(fix / "cohorts" / "iso.csv", out,
                                          pid_as_str=True)
        assert info.rows == 59
        assert cohort.validate(cohort.read_upload_csv(fix / "cohorts" / "iso.csv")).ok
        from spacescans import catalog
        cat = catalog.payload()["variables"]
        cov = orch.compute_coverage(fix / "cohorts" / "iso.csv", ["ndi"], cat)
        assert cov["row_count"] == 59
        leaked = sorted(m for m in sys.modules if m.split(".")[0] in _BLOCKED)
        assert not leaked, leaked
        print("BASE_OK")
        """
    )
    # Import the spacescans this test session uses (installed or src/).
    import spacescans
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(spacescans.__file__).parents[1])]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    r = subprocess.run([sys.executable, "-c", script], capture_output=True,
                       text=True, check=False, env=env)
    assert r.returncode == 0 and "BASE_OK" in r.stdout, (
        f"base-install import failed:\nstdout={r.stdout}\nstderr={r.stderr}"
    )
