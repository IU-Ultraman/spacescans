"""init() runs and registers expected base patterns."""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pandas as pd
import pytest
import yaml

import spacescans as _ss_pkg
from spacescans.pipeline.registry import init, get_pattern


def test_init_succeeds_on_base_install():
    init()


def test_base_patterns_registered():
    init()
    for name in ("yearly_areal", "static_areal", "cbp_fallback", "faqsd_daily_areal",
                 "precomputed_areal", "precomputed_static"):
        assert callable(get_pattern(name)), f"{name} not registered"


def test_unknown_pattern_raises():
    init()
    with pytest.raises(KeyError):
        get_pattern("does_not_exist_pattern")


def test_base_modules_import_without_optional_extras():
    """Regression guard: base modules + registry.init() must work with ONLY base deps.

    The dev/CI [all] env has geopandas/rasterio/etc installed, which masks accidental
    module-top imports of optional packages in base code. We simulate a base-only
    interpreter in a subprocess by blocking those imports via a sys.meta_path finder,
    then import every base module and run registry.init(). Catches the class of bug
    where a [geo]-only package leaks into the base install path.
    """
    script = textwrap.dedent(
        """
        import sys
        from importlib.abc import MetaPathFinder

        _BLOCKED = {"geopandas", "rasterio", "shapely", "exactextract",
                    "pyreadr", "pyhdf", "xarray", "netCDF4"}

        class _Blocker(MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name.split(".")[0] in _BLOCKED:
                    raise ModuleNotFoundError(
                        f"No module named {name!r} (blocked for base-install test)")
                return None

        sys.meta_path.insert(0, _Blocker())

        # init() imports all base modules; optional modules are swallowed.
        from spacescans.pipeline.registry import init, get_pattern
        init()
        for name in ("yearly_areal", "static_areal", "cbp_fallback", "faqsd_daily_areal",
                     "precomputed_areal", "precomputed_static"):
            assert callable(get_pattern(name)), f"{name} not registered on base install"
        print("BASE_OK")
        """
    )
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert r.returncode == 0 and "BASE_OK" in r.stdout, (
        f"base-install import failed:\nstdout={r.stdout}\nstderr={r.stderr}"
    )


def test_shipped_tiger_roads_demo_yaml_declares_episode_grouping():
    """Sprint 5 A2: the in-tree configs/c4/tiger_roads_demo.yaml MUST declare
    output_grouping: episode (spec L66-68 [B3], L646-647). Locked separately
    from the row-count smoke so flipping the YAML in Step 3 is what flips this
    assertion from RED to GREEN.
    """
    # Resolve relative to this test file so the assertion runs against the
    # config in the *same checkout* as the code under test (worktree-safe).
    cfg_path = Path(__file__).resolve().parents[1] / "configs" / "c4" / "tiger_roads_demo.yaml"
    rendered = yaml.safe_load(cfg_path.read_text())
    assert rendered["time"]["output_grouping"] == "episode", (
        f"shipped config must declare output_grouping: episode; "
        f"got time={rendered.get('time')}"
    )


@pytest.mark.geo
@pytest.mark.extras
def test_tiger_roads_demo_episode_branch_row_count(tmp_path):
    """End-to-end smoke for configs/c4/tiger_roads_demo.yaml episode branch.

    Spec ref: 2026-06-16-sprint-5-tiger-proximity-design.md L370-401 (option b)
    and L722-727. Row count must equal count(distinct (PATID, geoid)) and be
    strictly greater than count(distinct PATID); otherwise the dispatch is
    silently collapsing episodes.
    """
    # 5 unique patients; 3 of them have 2 episodes each → 8 cohort rows,
    # 8 distinct (PATID, geoid) pairs, 5 distinct PATIDs.
    cohort = pd.DataFrame({
        "pid":        ["P1", "P1", "P2", "P2", "P3", "P3", "P4", "P5"],
        "startDate":  ["2014-01-01", "2016-01-01", "2014-01-01", "2017-01-01",
                       "2015-01-01", "2018-01-01", "2014-01-01", "2014-01-01"],
        "endDate":    ["2015-12-31", "2017-12-31", "2016-12-31", "2018-12-31",
                       "2017-12-31", "2019-12-31", "2016-12-31", "2016-12-31"],
        "longitude":  [-80.0] * 8,
        "latitude":   [25.0]  * 8,
        "state_fips": [12]    * 8,
        "county_fips":[12086] * 8,
        "tract_geoid":["12086000100"] * 8,
        "bg_geoid":   ["120860001001"] * 8,
        # Distinct geoid per episode — adapter consumes this as `geoid`.
        "episode_id": [10, 11, 20, 21, 30, 31, 40, 50],
    })
    cohort_path = tmp_path / "cohort.parquet"
    cohort.to_parquet(cohort_path, index=False)

    # Minimal C3 exposure: geoids 10/11/20/21/30/31/40/50 × years 2013-2019.
    years = list(range(2013, 2020))
    geoids = [10, 11, 20, 21, 30, 31, 40, 50]
    exposure = pd.DataFrame([
        {"geoid": g, "year": y,
         "dist_pri": 100.0 + g, "dist_sec": 200.0 + g, "dist_prisec": 200.0 + g}
        for g in geoids for y in years
    ])
    exposure_path = tmp_path / "c3_annual_proximity.parquet"
    exposure.to_parquet(exposure_path, index=False)

    output_path = tmp_path / "c4_out.parquet"
    label_path  = tmp_path / "c4_label.parquet"

    cfg = {
        "name": "tiger_roads_demo_smoke",
        "linkage_pattern": "precomputed_areal",
        "geometry_type": "line",
        "source": {"file": str(tmp_path)},
        "buffer": {"patient_file": str(cohort_path),
                   "patient_adapter": "demo_conus"},
        "exposure": {"file": str(exposure_path),
                     "join_col": "geoid",
                     "value_cols": ["dist_pri", "dist_sec", "dist_prisec"]},
        "time": {"years": years,
                 "temporal_resolution": "yearly",
                 "temporal_mode": "yearly",
                 "output_grouping": "episode"},
        "engine": {"backend": "duckdb"},
        "plugin": "tiger_roads",
        "output": {"path": str(output_path),
                   "format": "parquet",
                   "label_path": str(label_path)},
    }
    yaml_path = tmp_path / "tiger_roads_demo_smoke.yaml"
    yaml_path.write_text(yaml.safe_dump(cfg))

    # Force subprocess to import spacescans from THIS checkout's src/ rather
    # than the editable-install location, so A1's dispatch code is exercised
    # even when the worktree's branch hasn't been merged to the install ref.
    # H6: only inject when the editable install resolves elsewhere — on a
    # single-checkout dev box the install IS the worktree and the override
    # is a no-op (env = os.environ.copy()).
    worktree_init = Path(__file__).resolve().parents[1] / "src" / "spacescans" / "__init__.py"
    installed_init = Path(_ss_pkg.__file__).resolve()
    if worktree_init.resolve() != installed_init:
        # worktree differs from editable install — inject worktree onto PYTHONPATH
        worktree_src = str(Path(__file__).resolve().parents[1] / "src")
        env = {**os.environ, "PYTHONPATH": worktree_src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    else:
        env = os.environ.copy()

    r = subprocess.run(
        [sys.executable, "-m", "spacescans.cli", "run", str(yaml_path)],
        capture_output=True, text=True, cwd=str(tmp_path), env=env,
    )
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"

    out = pd.read_parquet(output_path)
    n_rows = len(out)
    n_patid_geoid = out[["PATID", "geoid"]].drop_duplicates().shape[0]
    n_patid = out["PATID"].nunique()

    assert "geoid" in out.columns, f"episode branch must emit geoid; got {out.columns.tolist()}"
    assert n_rows == n_patid_geoid, (
        f"row count {n_rows} != distinct (PATID, geoid) {n_patid_geoid}"
    )
    assert n_rows > n_patid, (
        f"row count {n_rows} must exceed distinct PATID count {n_patid} "
        f"(smoke loses protective value if cohort is 1:1 patient↔geoid)"
    )
    # Exact lock from the fixture above.
    assert n_rows == 8 and n_patid == 5


def test_pythonpath_helper_no_ops_when_paths_match(monkeypatch):
    """H6: when the editable install resolves to the worktree's own
    src/spacescans/__init__.py, the helper must NOT inject PYTHONPATH —
    env must equal os.environ.copy() (worktree-safety affordance is a no-op
    on single-checkout dev boxes).
    """
    import spacescans as _ss_pkg

    worktree_init = Path(__file__).resolve().parents[1] / "src" / "spacescans" / "__init__.py"
    # Force the comparison's installed_init side to match worktree_init exactly.
    monkeypatch.setattr(_ss_pkg, "__file__", str(worktree_init))

    installed_init = Path(_ss_pkg.__file__).resolve()
    if worktree_init.resolve() != installed_init:
        worktree_src = str(Path(__file__).resolve().parents[1] / "src")
        env = {**os.environ, "PYTHONPATH": worktree_src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    else:
        env = os.environ.copy()

    assert env == os.environ.copy(), (
        "paths-match branch must produce env identical to os.environ.copy(); "
        f"got diff keys: {set(env) ^ set(os.environ)}"
    )
    assert env.get("PYTHONPATH") == os.environ.get("PYTHONPATH"), (
        "PYTHONPATH must be untouched when worktree == installed init path"
    )


def test_shipped_nhd_bluespace_demo_yaml_declares_patient_grouping():
    """Sprint 7 A2: the in-tree configs/c4/nhd_bluespace_demo.yaml MUST declare
    output_grouping: patient (spec L74-77 [B3], L218-227 audit, L484-505 rec (a)).
    Locked separately from the row-count smoke so flipping the YAML in Step 3
    is what flips this assertion from RED to GREEN. The shipped 100k cohort is
    1:1 PATID-to-geoid so the v1 CLI default (patient) is what reproducibility
    demands; the web runner overrides to episode at render time.
    """
    cfg_path = Path(__file__).resolve().parents[1] / "configs" / "c4" / "nhd_bluespace_demo.yaml"
    rendered = yaml.safe_load(cfg_path.read_text())
    assert rendered["time"]["output_grouping"] == "patient", (
        f"shipped config must declare output_grouping: patient; "
        f"got time={rendered.get('time')}"
    )


@pytest.mark.geo
@pytest.mark.extras
def test_nhd_bluespace_demo_patient_branch_row_count():
    """End-to-end smoke for configs/c4/nhd_bluespace_demo.yaml patient branch.

    Spec ref: 2026-06-16-sprint-7-nhd-bluespace-design.md L484-505 recommendation
    (a) — the shipped 100k cohort is 1:1 PATID-to-geoid, so the patient branch
    output row count must equal the cohort size (100_000) with PATID unique.
    Multi-episode static-pattern coverage lives in A1's unit-test fixture; no
    CLI episode smoke is shipped for NHD per spec rec (a).
    """
    repo_root = Path(__file__).resolve().parents[1]
    cfg_path = repo_root / "configs" / "c4" / "nhd_bluespace_demo.yaml"
    rendered = yaml.safe_load(cfg_path.read_text())

    gdb_path = repo_root / rendered["source"]["file"]
    cohort_path = repo_root / rendered["buffer"]["patient_file"]
    exposure_path = repo_root / rendered["exposure"]["file"]
    if not gdb_path.exists() or not cohort_path.exists() or not exposure_path.exists():
        pytest.skip(f"NHD demo inputs absent: gdb={gdb_path.exists()} "
                    f"cohort={cohort_path.exists()} exposure={exposure_path.exists()}")

    worktree_init = repo_root / "src" / "spacescans" / "__init__.py"
    installed_init = Path(_ss_pkg.__file__).resolve()
    if worktree_init.resolve() != installed_init:
        worktree_src = str(repo_root / "src")
        env = {**os.environ, "PYTHONPATH": worktree_src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    else:
        env = os.environ.copy()

    r = subprocess.run(
        [sys.executable, "-m", "spacescans.cli", "run", str(cfg_path),
         "--data-dir", str(repo_root), "--output-dir", str(repo_root)],
        capture_output=True, text=True, cwd=str(repo_root), env=env,
    )
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"

    output_path = repo_root / rendered["output"]["path"]
    out = pd.read_parquet(output_path)

    assert len(out) == 100_000, (
        f"patient-branch row count {len(out)} != 100_000 (shipped 100k cohort)"
    )
    assert out["PATID"].nunique() == 100_000, (
        f"PATID must be unique on patient branch; got nunique={out['PATID'].nunique()}"
    )


def test_shipped_vnl_demo_yaml_declares_patient_grouping():
    """Sprint 10 A2: the in-tree configs/c4/vnl_demo.yaml MUST declare
    output_grouping: patient. The shipped 100k cohort is 1:1 PATID-to-geoid,
    so the v1 CLI default (patient) is what reproducibility demands; the web
    runner overrides to episode at render time.
    """
    cfg_path = Path(__file__).resolve().parents[1] / "configs" / "c4" / "vnl_demo.yaml"
    rendered = yaml.safe_load(cfg_path.read_text())
    assert rendered["time"]["output_grouping"] == "patient", (
        f"shipped config must declare output_grouping: patient; "
        f"got time={rendered.get('time')}"
    )


def test_shipped_temis_demo_yaml_declares_patient_grouping():
    """Sprint 10 A2: the in-tree configs/c4/temis_demo.yaml MUST declare
    output_grouping: patient. The shipped 100k cohort is 1:1 PATID-to-geoid,
    so the v1 CLI default (patient) is what reproducibility demands; the web
    runner overrides to episode at render time.
    """
    cfg_path = Path(__file__).resolve().parents[1] / "configs" / "c4" / "temis_demo.yaml"
    rendered = yaml.safe_load(cfg_path.read_text())
    assert rendered["time"]["output_grouping"] == "patient", (
        f"shipped config must declare output_grouping: patient; "
        f"got time={rendered.get('time')}"
    )
