"""The merged tier-2 modules against the golden web 0.2.7 runs on synth_1k.

The goldens (tests/golden/web-0.2.7, or $SPACESCANS_GOLDEN_DIR, read at import) were captured
by tier2/golden from the frozen web backend. Here the catalog, orchestrate, custom and cohort
modules reproduce them together:

* A0 render: every ``pipeline_configs/<step>.yaml`` a runner wrote, and every YAML of the
  render matrix (126 runner x variables x buffer x raster combinations, 306 files), equals
  ``orchestrate.render`` of ``catalog.plan(...)``: byte for byte in the golden's own task dir,
  and after the A0 normalisation in another run dir. The catalog's reference renderer
  (tests/catalog/conftest.py) and, for the custom runs, ``custom.render`` give the same text.
* A2 cache: ``WeightCache(key_format="legacy-v1")`` names all 14 cold C3 entries
  (``c3_cache/<key>.meta.json``) and every key of the render matrix; storing the golden C3
  output writes that meta file byte for byte (its ``created_at`` and the step's seconds taken
  from the golden) and the cached parquet the manifest records, and fetching it back returns
  the same bytes. The store needs the golden ``output/`` directories.
* A1 cohort: ``cohort.csv_to_parquet(inputs/synth_1k.csv)`` equals the runners'
  ``input.parquet``: byte for byte under the golden's pandas version; elsewhere the files
  differ only in the pandas version they embed.
* A3 merge: ``orchestrate.merge`` rebuilds each ``result_<exp>.csv`` byte for byte from the
  golden C4 parquets, and the fan-in over all of them gives ``fanin_all``'s ``result.csv``
  and ``feature_dictionary.csv``. Skipped while the golden ``output/`` directories are absent.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from spacescans import catalog, cohort, custom
from spacescans.orchestrate import cache as orch_cache
from spacescans.orchestrate.cache import WeightCache
from spacescans.orchestrate.context import RunContext
from spacescans.orchestrate.render import render_text

REPO = Path(__file__).resolve().parents[1]


def _load_golden_compare():
    """tests/golden/golden_compare.py, the golden track's comparison helpers."""
    spec = importlib.util.spec_from_file_location(
        "golden_compare", REPO / "tests" / "golden" / "golden_compare.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("golden_compare", module)
    spec.loader.exec_module(module)
    return module


gc = _load_golden_compare()
# Golden tests (tests/conftest.py): the goldens are not in git, so this module is skipped unless
# SPACESCANS_GOLDEN_DIR names a copy; CI deselects the golden marker.
pytestmark = pytest.mark.golden
if gc.golden_dir() is None:
    pytest.skip(gc.golden_unavailable_reason(), allow_module_level=True)
ROOT = gc.golden_root()
MANIFEST = gc.load_manifest(ROOT)
COHORT_CSV = ROOT / "inputs" / "synth_1k.csv"
RUNS = [name for name in sorted(MANIFEST["runs"])
        if "experiment" in json.loads((ROOT / name / "run.json").read_text())]
HAVE_OUTPUTS = all((ROOT / name / "output").is_dir() for name in MANIFEST["runs"])
needs_outputs = pytest.mark.skipif(
    not HAVE_OUTPUTS, reason="golden output/ directories are not in this checkout")


def _run(name: str) -> tuple[dict, dict]:
    return (json.loads((ROOT / name / "run.json").read_text()),
            json.loads((ROOT / name / "config.json").read_text()))


def _overlay(run: dict, config: dict):
    return config.get("custom_variables") if run["experiment"] == "custom" else None


def _ctx(run: dict, config: dict, task_dir: Path, **kw) -> RunContext:
    """The context the web runner had: its task dir layout and its config's buffer values."""
    return RunContext.from_task_dir(task_dir, data_dir="/project",
                                    buffer_m=config["buffer"]["size"],
                                    raster_res_m=config["buffer"].get("raster_res_m", 25),
                                    overlay=_overlay(run, config), **kw)


def _render_reference():
    spec = importlib.util.spec_from_file_location(
        "_catalog_conftest_for_golden_parity", REPO / "tests" / "catalog" / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render_reference


# ---------------------------------------------------------------------------------------------
# A0: render
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", RUNS)
def test_render_reproduces_the_runner_yamls(name, tmp_path):
    import yaml

    run, config = _run(name)
    keys, task_dir = run["variables"], Path(run["task_dir"])
    ctx = _ctx(run, config, task_dir)
    elsewhere = _ctx(run, config, tmp_path / "tasks" / f"task-{name[-8:]:0>8}")
    steps = catalog.plan(run["experiment"], keys, overlay=ctx.overlay)
    written = {p.stem: p.read_text() for p in (ROOT / name / "pipeline_configs").glob("*.yaml")}
    assert written and set(written) <= {s.name for s in steps}
    reference = _render_reference()
    for step in steps:
        if step.name not in written:      # a cache hit renders nothing
            continue
        text = render_text(step, ctx)
        assert text == written[step.name], step.name
        assert gc.normalise_yaml(render_text(step, elsewhere), str(elsewhere.run_dir)) == \
            gc.normalise_yaml(written[step.name], str(task_dir)), step.name
        assert reference(step, run_dir=task_dir, buffer_m=ctx.buffer_m,
                         raster_res_m=ctx.raster_res_m, name_suffix=ctx.name_suffix) == text
        if ctx.overlay is not None:
            cfg = custom.render(step, catalog.template(step.template), ctx, variables=keys,
                                definitions=ctx.overlay)
            assert yaml.safe_dump(cfg, sort_keys=False) == text, step.name


MATRIX = ROOT / "render_matrix"
COMBOS = (json.loads((MATRIX / "index.json").read_text())
          if (MATRIX / "index.json").exists() else [])
needs_matrix = pytest.mark.skipif(not COMBOS, reason="no golden render matrix")


def _matrix_overlay(combo: dict):
    """The upload definitions tests/golden/tools/render_matrix.py used: the two real ones, plus
    the Tract, ZCTA5, County and yearly ones it synthesised from the BG one."""
    if combo["experiment"] != "custom":
        return None
    bg = json.loads((ROOT / "custom_bg_csv" / "custom_definition.json").read_text())
    raster = json.loads((ROOT / "custom_raster" / "custom_definition.json").read_text())

    def synth(boundary: str, n: int, yearly: bool = False) -> dict:
        d = copy.deepcopy(bg)
        d["dataset_id"] = f"0000000{n}"
        d["variable_key"] = custom.variable_key(d["dataset_id"])
        d["boundary"] = boundary
        d["join_col"] = custom.BOUNDARY_SPEC[boundary]["join_col"]
        d["values_path"] = f"/app/data/custom_exposomes/user-9997/{d['dataset_id']}/values.csv"
        if yearly:
            d["year_col"], d["coverage_years"], d["temporal"] = "year", [2014, 2017], "yearly"
        return d

    defs = {"bg": bg, "tract": synth("Tract", 1), "zcta5": synth("ZCTA5", 2),
            "county": synth("County", 3), "bg_yearly": synth("BG", 4, yearly=True),
            "tract_yearly": synth("Tract", 5, yearly=True), "raster": raster}
    overlay = {defs[p]["variable_key"]: defs[p] for p in combo["combo"].split("__")[1].split("-")}
    assert list(overlay) == combo["variables"]
    return overlay


@needs_matrix
def test_render_matrix_is_complete():
    assert len(COMBOS) == 126
    assert sum(1 for c in COMBOS for s in c["steps"] if s.get("rendered")) == 306


@needs_matrix
@pytest.mark.parametrize("combo", COMBOS, ids=[c["combo"] for c in COMBOS])
def test_render_reproduces_the_render_matrix(combo, tmp_path):
    overlay = _matrix_overlay(combo)
    task_dir = Path(combo["task_dir"])
    kw = {"data_dir": "/project", "buffer_m": combo["buffer_m"],
          "raster_res_m": combo["raster_res_m"], "overlay": overlay}
    ctx = RunContext.from_task_dir(task_dir, **kw)
    elsewhere = RunContext.from_task_dir(tmp_path / task_dir.name, **kw)
    steps = catalog.plan(combo["experiment"], combo["variables"], overlay=overlay)
    assert [(s.name, s.template, s.is_c3) for s in steps] == \
        [(s["name"], s["template"], s["is_c3"]) for s in combo["steps"]]
    rendered = 0
    for step, rec in zip(steps, combo["steps"]):
        if not rec.get("rendered"):       # the raster upload's C4 (rendering it reads pixels)
            continue
        golden = (MATRIX / combo["combo"] / f"{step.name}.yaml").read_text()
        assert render_text(step, ctx) == golden, step.name
        assert gc.normalise_yaml(render_text(step, elsewhere), str(elsewhere.run_dir)) == \
            gc.normalise_yaml(golden, str(task_dir)), step.name
        rendered += 1
    assert rendered == sum(1 for s in combo["steps"] if s.get("rendered"))


# ---------------------------------------------------------------------------------------------
# A2: the legacy-v1 cache
# ---------------------------------------------------------------------------------------------

@needs_matrix
@pytest.mark.parametrize("combo", COMBOS, ids=[c["combo"] for c in COMBOS])
def test_legacy_keys_match_the_render_matrix(combo):
    overlay = _matrix_overlay(combo)
    # render_matrix.py keyed every combination on the shared noise/input.parquet
    ctx = RunContext(run_dir=Path(combo["task_dir"]), cohort_parquet=ROOT / "noise" / "input.parquet",
                     data_dir="/project", buffer_m=combo["buffer_m"],
                     raster_res_m=combo["raster_res_m"], overlay=overlay)
    legacy = WeightCache(Path("/nonexistent-cache"), key_format="legacy-v1")
    steps = catalog.plan(combo["experiment"], combo["variables"], overlay=overlay)
    expected = [rec["cache_key"] for rec in combo["steps"] if "cache_key" in rec]
    assert [legacy.key(s, ctx) for s, rec in zip(steps, combo["steps"]) if "cache_key" in rec] \
        == expected


def _cold_c3(name: str) -> list:
    run, config = _run(name)
    steps = catalog.plan(run["experiment"], run["variables"], overlay=_overlay(run, config))
    written = {p.stem for p in (ROOT / name / "pipeline_configs").glob("*.yaml")}
    return [s for s in steps if s.is_c3 and s.cache_tag and s.name in written]  # cold C3 only


GOLDEN_KEYS = sorted((name, p.name[: -len(".meta.json")]) for name in RUNS
                     for p in (ROOT / name / "c3_cache").glob("*.meta.json"))


def test_there_are_fourteen_golden_cache_entries():
    assert len(GOLDEN_KEYS) == 14


@pytest.mark.parametrize("name", RUNS)
def test_legacy_keys_name_the_runner_cache_entries(name, tmp_path):
    run, config = _run(name)
    task = tmp_path / "tasks" / f"task-{name[-8:]:0>8}"
    task.mkdir(parents=True)
    shutil.copyfile(ROOT / name / "input.parquet", task / "input.parquet")
    ctx = _ctx(run, config, task)
    legacy = WeightCache(tmp_path / "c3_cache", key_format="legacy-v1")
    assert sorted(legacy.key(step, ctx) for step in _cold_c3(name)) == \
        sorted(k for n, k in GOLDEN_KEYS if n == name)
    for step in _cold_c3(name):
        meta = json.loads((ROOT / name / "c3_cache" / f"{legacy.key(step, ctx)}.meta.json")
                          .read_text())
        assert meta["boundary"] == (step.meta_boundary or step.cache_tag)
        assert ("raster_res_m" in meta) == bool(step.meta_raster_res)


@needs_outputs
@pytest.mark.parametrize("name,key", GOLDEN_KEYS, ids=[k for _n, k in GOLDEN_KEYS])
def test_legacy_store_writes_the_runner_entry_byte_for_byte(name, key, tmp_path, monkeypatch):
    run, config = _run(name)
    task = tmp_path / "tasks" / f"task-{name[-8:]:0>8}"
    (task / "output").mkdir(parents=True)
    shutil.copyfile(ROOT / name / "input.parquet", task / "input.parquet")
    assert MANIFEST["runs"][name]["input_csv_is_cohort"]
    shutil.copyfile(COHORT_CSV, task / "input.csv")      # input_row_count counts its lines
    ctx = _ctx(run, config, task)
    cache = WeightCache(tmp_path / "c3_cache", key_format="legacy-v1")
    (step,) = [s for s in _cold_c3(name) if cache.key(s, ctx) == key]
    src = ctx.output_path(step.name)
    shutil.copyfile(ROOT / name / "output" / src.name, src)

    golden_meta = ROOT / name / "c3_cache" / f"{key}.meta.json"
    recorded = json.loads(golden_meta.read_text())
    monkeypatch.setattr(orch_cache, "_utc_now_iso", lambda: recorded["created_at"])
    assert cache.fetch(step, ctx, tmp_path / "miss.parquet") is None
    assert cache.store(step, ctx, src, seconds=recorded["wall_clock_seconds"] + 0.6) == key
    assert cache.meta_path(key).read_bytes() == golden_meta.read_bytes()
    (entry,) = [e for e in MANIFEST["runs"][name]["c3_cache_entries"] if e["key"] == key]
    assert hashlib.sha256(cache.path(key).read_bytes()).hexdigest() == entry["sha256"]

    again = tmp_path / "again" / "output" / src.name
    assert cache.fetch(step, ctx, again) == key
    assert again.read_bytes() == src.read_bytes()


# ---------------------------------------------------------------------------------------------
# A1 cohort, A3 merge
# ---------------------------------------------------------------------------------------------



@pytest.mark.parametrize("variant", cohort.VARIANTS)
def test_cohort_parquet_matches_the_runners_input(variant, tmp_path):
    import pandas as pd
    import pyarrow.parquet as pq

    # The cohort track's rule: bytes need the golden's pandas (and pyarrow), the arrow schema
    # needs its pandas major version, and the values must always match.
    golden = ROOT / "noise" / "input.parquet"
    golden_pandas = MANIFEST["environment"]["packages"]["pandas"]
    info = cohort.csv_to_parquet(COHORT_CSV, tmp_path / "input.parquet", variant=variant)
    if pd.__version__ == golden_pandas:
        assert info.sha256 == hashlib.sha256(golden.read_bytes()).hexdigest()
        return
    same_major = pd.__version__.split(".")[0] == golden_pandas.split(".")[0]
    if same_major:
        ours, theirs = pq.read_schema(tmp_path / "input.parquet"), pq.read_schema(golden)
        assert ours.equals(theirs, check_metadata=False)
        meta = [json.loads(s.metadata[b"pandas"]) for s in (ours, theirs)]
        for m in meta:
            m.pop("pandas_version")
        assert meta[0] == meta[1]
    pd.testing.assert_frame_equal(pd.read_parquet(tmp_path / "input.parquet"),
                                  pd.read_parquet(golden), check_exact=True,
                                  check_dtype=same_major)


def _partial(name: str, work: Path):
    from spacescans.orchestrate import merge

    run, config = _run(name)
    exp, keys = run["experiment"], run["variables"]
    rec = catalog.recipe(exp)
    pmap = rec.output_map(keys) if not rec.dynamic else {k: f"c4_{k}.parquet" for k in keys}
    (work / "output").mkdir(parents=True)
    shutil.copyfile(COHORT_CSV, work / "input.csv")
    for fname in pmap.values():
        shutil.copyfile(ROOT / name / "output" / fname, work / "output" / fname)
    value_cols = {k: list(v.value_cols)
                  for k, v in catalog.variables(overlay=_overlay(run, config)).items()}
    merged, stats = merge.partial_from_dir(work, exp, keys, pmap, value_cols,
                                           renames=dict(rec.renames) or None,
                                           rewrite_in_place=rec.rename_in_place)
    return exp, merged, stats


@needs_outputs
@pytest.mark.parametrize("name", RUNS)
def test_merge_rebuilds_the_runner_partial(name, tmp_path):
    from spacescans.orchestrate import merge

    exp, merged, stats = _partial(name, tmp_path)
    out = tmp_path / "output" / merge.partial_csv_name(exp)
    merged.to_csv(out, index=False)
    golden = ROOT / name / "output" / out.name.replace(".csv", ".as_written.csv")
    assert out.read_bytes() == golden.read_bytes()
    assert stats.cohort_n == 1001


@needs_outputs
def test_fan_in_rebuilds_the_task_result_and_feature_dictionary(tmp_path):
    from spacescans.orchestrate import merge

    run = json.loads((ROOT / "fanin_all" / "run.json").read_text())
    (tmp_path / "output").mkdir()
    shutil.copyfile(COHORT_CSV, tmp_path / "input.csv")
    experiments = []
    for source in run["sources"]:
        exp = json.loads((ROOT / source / "run.json").read_text())["experiment"]
        name = merge.partial_csv_name(exp)
        shutil.copyfile(ROOT / source / "output" / name.replace(".csv", ".as_written.csv"),
                        tmp_path / "output" / name)
        experiments.append(exp)
    result = merge.fan_in_from_dir(tmp_path, experiments)
    result.to_csv(tmp_path / "result.csv", index=False)
    assert (tmp_path / "result.csv").read_bytes() == (
        ROOT / "fanin_all" / "output" / "result.csv").read_bytes()
    catalog.write_feature_csv(list(result.columns), tmp_path / "feature_dictionary.csv")
    assert (tmp_path / "feature_dictionary.csv").read_bytes() == (
        ROOT / "fanin_all" / "output" / "feature_dictionary.csv").read_bytes()
