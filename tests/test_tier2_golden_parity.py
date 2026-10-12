"""The merged tier-2 modules against the golden web 0.2.7 runs on synth_1k.

The goldens (tests/golden/web-0.2.7, or $SPACESCANS_GOLDEN_DIR, read at import) were captured
by tier2/golden from the frozen web backend. Here the catalog, custom and cohort tracks
reproduce them together:

* A0 render: every ``pipeline_configs/<step>.yaml`` a runner wrote equals the catalog's render
  contract (the reference renderer in tests/catalog/conftest.py) applied to
  ``catalog.plan(...)``; for the custom runs ``custom.render`` gives the same text.
* A2 cache: the legacy key of every C3 step that ran cold names a ``c3_cache/<key>.meta.json``
  whose ``boundary`` and ``raster_res_m`` follow ``Step.meta_boundary``/``meta_raster_res``.
* A1 cohort: ``cohort.csv_to_parquet(inputs/synth_1k.csv)`` equals the runners'
  ``input.parquet``: byte for byte under the golden's pandas version; elsewhere the files
  differ only in the pandas version they embed.
* A3 merge: ``orchestrate.merge`` rebuilds each ``result_<exp>.csv`` byte for byte from the
  golden C4 parquets, and the fan-in over all of them gives ``fanin_all``'s ``result.csv``
  and ``feature_dictionary.csv``. Skipped while the golden ``output/`` directories are absent.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from spacescans import catalog, cohort, custom
from spacescans.catalog import recipes

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


def _render_reference():
    spec = importlib.util.spec_from_file_location(
        "_catalog_conftest_for_golden_parity", REPO / "tests" / "catalog" / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render_reference


@pytest.mark.parametrize("name", RUNS)
def test_runner_yamls_follow_the_catalog_render_contract(name):
    import yaml

    run, config = _run(name)
    keys, task_dir = run["variables"], Path(run["task_dir"])
    buffer_m, res = config["buffer"]["size"], config["buffer"].get("raster_res_m", 25)
    overlay = _overlay(run, config)
    steps = catalog.plan(run["experiment"], keys, overlay=overlay)
    written = {p.stem: p.read_text() for p in (ROOT / name / "pipeline_configs").glob("*.yaml")}
    assert set(written) <= {s.name for s in steps}
    render = _render_reference()
    suffix = f"_task_{task_dir.name[-8:]}"
    for step in steps:
        if step.name not in written:      # a cache hit renders nothing
            continue
        assert render(step, run_dir=task_dir, buffer_m=buffer_m, raster_res_m=res,
                      name_suffix=suffix) == written[step.name], step.name
        if overlay is not None:
            ctx = custom.Context(run_dir=task_dir, cohort_parquet=task_dir / "input.parquet",
                                 buffer_m=buffer_m, raster_res_m=res, name_suffix=suffix)
            cfg = custom.render(step, catalog.template(step.template), ctx, variables=keys,
                                definitions=overlay)
            assert yaml.safe_dump(cfg, sort_keys=False) == written[step.name], step.name


@pytest.mark.parametrize("name", RUNS)
def test_cold_c3_steps_name_their_cache_entries(name):
    run, config = _run(name)
    sha = hashlib.sha256((ROOT / name / "input.parquet").read_bytes()).hexdigest()
    buffer_m, res = config["buffer"]["size"], config["buffer"].get("raster_res_m", 25)
    steps = catalog.plan(run["experiment"], run["variables"], overlay=_overlay(run, config))
    written = {p.stem for p in (ROOT / name / "pipeline_configs").glob("*.yaml")}
    for step in steps:
        if not (step.is_c3 and step.cache_tag) or step.name not in written:
            continue                      # only a cold C3 step renders and writes the cache
        key = recipes.legacy_cache_key(step, sha, buffer_m=buffer_m, raster_res_m=res)
        meta = json.loads((ROOT / name / "c3_cache" / f"{key}.meta.json").read_text())
        assert meta["boundary"] == (step.meta_boundary or step.cache_tag)
        assert ("raster_res_m" in meta) == bool(step.meta_raster_res)


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
