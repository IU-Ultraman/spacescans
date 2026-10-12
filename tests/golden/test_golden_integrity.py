"""The golden references are intact, self-consistent and in canonical order.

These tests check the stored files against manifest.json. They do not re-run anything. The
parity tests that compare a new implementation with the goldens live with that
implementation and use golden_compare.py.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import golden_compare as gc  # noqa: E402

# The goldens are not in git: tests marked golden check the copy SPACESCANS_GOLDEN_DIR names and
# are skipped without it (tests/conftest.py). The helper tests at the end need no goldens.
ROOT = gc.golden_root()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def manifest() -> dict:
    return gc.load_manifest(ROOT)


@pytest.mark.golden
def test_manifest_shape(manifest):
    assert manifest["schema"] == 1
    assert manifest["environment"]["spacescans.__version__"] == "0.2.7"
    assert manifest["environment"]["image"]["frozen_image"]["tag"] == "spacescans-web-backend:frozen-2026-10-12"
    assert manifest["params"] == {
        "buffer_m": 270, "raster_res_m": 25, "buffer_shape": "circle",
        "config_base": {"experiment": "auto", "version": 1, "input_file": "input.csv"}}
    assert manifest["sort_rule"] == gc.SORT_RULE


@pytest.mark.golden
def test_cohort_is_synth_1k(manifest):
    c = manifest["cohort"]
    assert _sha(ROOT / c["path"]) == c["sha256"]
    assert (c["episodes"], c["patients"]) == (1001, 510)


@pytest.mark.golden
def test_every_catalog_variable_is_covered(manifest):
    covered = {v for r in manifest["runs"].values() if r.get("success")
               for v in (r.get("variables") or [])}
    catalog = {"ndi", "walkability", "cbp_zcta5", "tiger_proximity", "nhd_bluespace", "noise",
               "vnl", "temis", "fara_tract", "acag", "faqsd"}
    missing = catalog - covered
    failed = {n: r.get("error") or r.get("rc") for n, r in manifest["runs"].items() if not r.get("success")}
    assert not missing, f"catalog variables without a successful golden: {missing}; failed runs: {failed}"
    custom = [r for r in manifest["runs"].values() if r.get("experiment") == "custom" and r.get("success")]
    assert len(custom) == 2


@pytest.mark.golden
def test_listed_files_match_their_hashes(manifest):
    for name, run in manifest["runs"].items():
        for f in run.get("files", []):
            p = ROOT / name / f["path"]
            assert p.is_file(), f"{name}: {f['path']} is listed but missing"
            assert p.stat().st_size == f["bytes"], f"{name}: {f['path']} size changed"
            assert _sha(p) == f["sha256"], f"{name}: {f['path']} content changed"


@pytest.mark.golden
def test_no_unlisted_files(manifest):
    for name, run in manifest["runs"].items():
        listed = {f["path"] for f in run.get("files", [])}
        on_disk = {str(p.relative_to(ROOT / name)) for p in (ROOT / name).rglob("*") if p.is_file()}
        assert on_disk == listed, f"{name}: unlisted {sorted(on_disk - listed)}, missing {sorted(listed - on_disk)}"


@pytest.mark.golden
def test_partials_are_in_canonical_order(manifest):
    for name, run in manifest["runs"].items():
        for stem, info in (run.get("results") or {}).items():
            written = (ROOT / name / "output" / f"{stem}.as_written.csv").read_text()
            stored = (ROOT / name / "output" / f"{stem}.sorted.csv").read_text()
            assert gc.canonical_result_csv(written) == stored, f"{name}/{stem}: sorted copy is stale"
            assert gc.canonical_result_csv(stored) == stored, f"{name}/{stem}: sort is not idempotent"
            assert written.count("\n") == stored.count("\n")
            assert gc.compare_result_csv(ROOT / name / "output" / f"{stem}.as_written.csv",
                                         ROOT / name / "output" / f"{stem}.sorted.csv") == []


@pytest.mark.golden
def test_rendered_configs_keep_the_web_contract(manifest):
    for name, run in manifest["runs"].items():
        cfg_dir = ROOT / name / "pipeline_configs"
        if not cfg_dir.is_dir():
            continue
        task = run["remote_task_dir"]
        for y in sorted(cfg_dir.glob("*.yaml")):
            cfg = yaml.safe_load(y.read_text())
            assert cfg["output"]["path"] == f"{task}/output/{y.stem}.parquet"
            assert cfg["buffer"]["patient_file"] == f"{task}/input.parquet"
            assert cfg["buffer"]["buffer_m"] == 270
            assert cfg["name"].endswith("_task_" + Path(task).name[-8:])
            if "time" in cfg:
                assert cfg["time"]["output_grouping"] == "episode"


@pytest.mark.golden
def test_cache_hits_render_no_yaml(manifest):
    warm = manifest["runs"].get("bg_ndi_wi_rev")
    if not warm or not warm.get("success"):
        pytest.skip("no warm run recorded")
    hits = [s for s, rec in warm["steps"].items() if rec.get("cache_hit")]
    assert sorted(hits) == ["c3_bg", "c3_bg_2020"]
    rendered = sorted(p.stem for p in (ROOT / "bg_ndi_wi_rev" / "pipeline_configs").glob("*.yaml"))
    assert rendered == ["c4_ndi", "c4_wi"]


# ---------------------------------------------------------------------------
# the comparison helpers themselves
# ---------------------------------------------------------------------------

def test_canonical_sort_is_numeric_on_episode_id_and_keeps_text():
    text = "pid,episode_id,v\nB,10,1.50\nA,2,0.1\nA,2,0.05\nC,1,\n"
    assert gc.canonical_result_csv(text) == "pid,episode_id,v\nC,1,\nA,2,0.05\nA,2,0.1\nB,10,1.50\n"


def test_canonical_sort_handles_a_missing_final_newline():
    assert gc.canonical_result_csv("pid,episode_id\nB,1\nA,0") == "pid,episode_id\nA,0\nB,1\n"


def test_compare_accepts_within_and_rejects_beyond_tolerance(tmp_path):
    exp = tmp_path / "e.csv"
    exp.write_text("pid,episode_id,v,flag\nA,0,1.0,x\nB,1,,y\n")
    ok = tmp_path / "ok.csv"
    ok.write_text("pid,episode_id,v,flag\nB,1,,y\nA,0,1.0000000000001,x\n")
    assert gc.compare_result_csv(exp, ok, tol=gc.SAME_ENV) == []
    far = tmp_path / "far.csv"
    far.write_text("pid,episode_id,v,flag\nA,0,1.001,x\nB,1,,y\n")
    assert any("outside" in p for p in gc.compare_result_csv(exp, far, tol=gc.CROSS_ENV))
    mask = tmp_path / "mask.csv"
    mask.write_text("pid,episode_id,v,flag\nA,0,1.0,x\nB,1,0.0,y\n")
    assert any("mask" in p for p in gc.compare_result_csv(exp, mask))
    key = tmp_path / "key.csv"
    key.write_text("pid,episode_id,v,flag\nA,0,1.0,x\nC,1,,y\n")
    assert any(p.startswith("pid") for p in gc.compare_result_csv(exp, key))
    cols = tmp_path / "cols.csv"
    cols.write_text("pid,episode_id,flag,v\nA,0,x,1.0\nB,1,y,\n")
    assert gc.compare_result_csv(exp, cols)[0].startswith("columns differ")


# ---------------------------------------------------------------------------
# the render-only matrix (A0/A2) agrees with what the runs rendered
# ---------------------------------------------------------------------------

@pytest.mark.golden
def test_render_matrix_matches_the_runs(manifest):
    matrix = ROOT / "render_matrix"
    if not (matrix / "index.json").exists():
        pytest.skip("no render matrix collected")
    index = {c["combo"]: c for c in json.loads((matrix / "index.json").read_text())}
    checked = 0
    for name, run in manifest["runs"].items():
        if name == "fanin_all" or not run.get("success"):
            continue
        label = ("raster" if name == "custom_raster" else "bg") if run["experiment"] == "custom" \
            else "-".join(run["variables"])
        combo = index[f"{run['experiment']}__{label}__b270_r25"]
        for y in sorted((ROOT / name / "pipeline_configs").glob("*.yaml")):
            rendered = matrix / combo["combo"] / y.name
            if not rendered.exists():
                assert y.stem.startswith("c4_custom_"), f"{name}: {y.name} missing from the matrix"
                continue                               # raster C4 is not rendered (reads pixels)
            assert gc.normalise_yaml(y.read_text(), run["remote_task_dir"]) == \
                gc.normalise_yaml(rendered.read_text(), combo["task_dir"]), f"{name}: {y.name}"
            checked += 1
        if name != "bg_ndi_wi_rev":                    # a warm run writes no cache entries
            assert sorted(e["key"] for e in run["c3_cache_entries"]) == \
                sorted(s["cache_key"] for s in combo["steps"] if s.get("cache_key"))
    assert checked >= 25


@pytest.mark.golden
def test_render_matrix_files_match_the_index():
    matrix = ROOT / "render_matrix"
    if not (matrix / "index.json").exists():
        pytest.skip("no render matrix collected")
    index = json.loads((matrix / "index.json").read_text())
    listed = set()
    for c in index:
        for s in c["steps"]:
            if s.get("rendered"):
                p = matrix / c["combo"] / f"{s['name']}.yaml"
                listed.add(p)
                assert _sha(p) == s["sha256"], f"{p} changed"
    on_disk = set(matrix.rglob("*.yaml"))
    assert on_disk == listed


@pytest.mark.golden
def test_runner_log_lines_keep_the_spawn_contract(manifest):
    """Every step either spawned `spacescans run` (and logged its exit code) or was a cache hit."""
    for name, run in manifest["runs"].items():
        if name == "fanin_all":
            continue
        lines = gc.runner_log_lines(ROOT / name / "logs.jsonl", run["remote_task_dir"])
        for step, rec in run["steps"].items():
            if rec.get("cache_hit"):
                assert any(f"|{step}|cache hit: " in x for x in lines), (name, step)
                continue
            spawn = (f"info|runner|spawning {step}: /opt/conda/bin/python /opt/conda/bin/spacescans "
                     f"run --data-dir /project <TASK>/pipeline_configs/{step}.yaml")
            assert spawn in lines, (name, step)
            assert f"info|runner|step {step} exit code 0" in lines, (name, step)
