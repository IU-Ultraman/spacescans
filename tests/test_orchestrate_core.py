"""spacescans.orchestrate.context, .render and .cache without goldens.

The golden parity of render (A0) and the legacy cache (A2) is in test_tier2_golden_parity.py.
Here: the run context, render's purity and engine option, write_config, the legacy-v1 cache's
format and hit rule, and the v2 cache: deterministic keys, every component in the key, reads
that need every recorded component, atomic stores, the per-key lock, private permissions,
info and clear. Base install only (pandas, pyarrow, pyyaml).
"""
from __future__ import annotations

import dataclasses
import json
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

import spacescans
from spacescans import catalog, custom
from spacescans.orchestrate import cache as orch_cache
from spacescans.orchestrate.cache import WeightCache
from spacescans.orchestrate.context import RunContext
from spacescans.orchestrate.render import prepare, render, render_text, write_config

NOISE_C3 = catalog.recipe("noise").steps["c3_noise"]            # grid tag, no __r
BG_C3 = catalog.recipe("bg_ndi_wi").steps["c3_bg"]              # areal tag, __r
BG2020_C3 = catalog.recipe("bg_ndi_wi").steps["c3_bg_2020"]     # meta says "BG"
TRACT_C3 = catalog.recipe("fara_tract").steps["c3_tract_us"]    # areal, meta without raster_res


def _cohort(path: Path, *, lon_shift: float = 0.0, pids=None) -> Path:
    n = 6
    pd.DataFrame({
        "pid": pids or [f"P{i // 2}" for i in range(n)],
        "startDate": pd.to_datetime(["2015-01-01"] * n), "endDate": pd.to_datetime(["2016-01-01"] * n),
        "longitude": [-86.1 + i * 0.01 + lon_shift for i in range(n)],
        "latitude": [39.7 + i * 0.01 for i in range(n)],
        "episode_id": list(range(n)),
    }).to_parquet(path, index=False)
    return path


@pytest.fixture
def run(tmp_path):
    """A run dir with a cohort, a data dir holding the noise grid, and a C3 output."""
    run_dir = tmp_path / "runs" / "task-0123abcd"
    (run_dir / "output").mkdir(parents=True)
    data = tmp_path / "data"
    (data / "Noise" / "C3").mkdir(parents=True)
    (data / "Noise" / "C3" / "CONUS_L50dBA_sumDay_exi.tif").write_bytes(b"grid" * 64)
    ctx = RunContext(run_dir=run_dir, cohort_parquet=_cohort(run_dir / "input.parquet"),
                     data_dir=data, name_suffix="_task_0123abcd")
    out = run_dir / "output" / "c3_noise.parquet"
    pd.DataFrame({"geoid": [0, 1, 2], "grid_id": [5, 6, 7], "weight": [1.0, 0.5, 0.5]}) \
        .to_parquet(out, index=False)
    return SimpleNamespace(ctx=ctx, out=out, data=data, tmp=tmp_path)


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


# ---------------------------------------------------------------------------------------------
# RunContext
# ---------------------------------------------------------------------------------------------

def test_run_context_has_the_contract_fields_and_defaults(tmp_path):
    ctx = RunContext(run_dir=str(tmp_path), cohort_parquet=str(tmp_path / "c.parquet"),
                     data_dir=str(tmp_path / "d"))
    assert [f.name for f in dataclasses.fields(RunContext)] == [
        "run_dir", "cohort_parquet", "data_dir", "buffer_m", "raster_res_m", "name_suffix",
        "overlay", "engine"]
    assert (ctx.buffer_m, ctx.raster_res_m, ctx.name_suffix, ctx.overlay, ctx.engine) == \
        (270, 25, "", None, None)
    assert isinstance(ctx.run_dir, Path) and isinstance(ctx.data_dir, Path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.buffer_m = 500


def test_run_context_satisfies_customs_render_context(tmp_path):
    """Mismatch #4: custom.render / prepare_step accept a RunContext."""
    ctx = RunContext.from_task_dir(tmp_path / "tasks" / "task-1234abcd", data_dir=tmp_path)
    assert isinstance(ctx, custom.RenderContext)
    assert ctx.cohort_parquet == tmp_path / "tasks" / "task-1234abcd" / "input.parquet"
    assert ctx.name_suffix == "_task_1234abcd"
    assert ctx.output_path("c3_bg") == ctx.run_dir / "output" / "c3_bg.parquet"
    assert ctx.config_path("c3_bg") == ctx.run_dir / "pipeline_configs" / "c3_bg.yaml"


def test_run_context_refuses_unknown_engine_options(tmp_path):
    with pytest.raises(ValueError, match="unknown key"):
        RunContext(run_dir=tmp_path, cohort_parquet=tmp_path, data_dir=tmp_path,
                   engine={"cores": 2})


# ---------------------------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------------------------

def test_render_is_pure_and_fills_the_run_fields(run):
    before = catalog.template(BG_C3.template)
    cfg = render(BG_C3, run.ctx)
    assert catalog.template(BG_C3.template) == before
    assert cfg["name"] == "bg_us_demo_task_0123abcd"
    assert cfg["buffer"]["patient_file"] == str(run.ctx.cohort_parquet)
    assert (cfg["buffer"]["buffer_m"], cfg["buffer"]["raster_res_m"]) == (270, 25)
    assert cfg["output"]["path"] == str(run.ctx.output_path("c3_bg"))
    assert "time" not in cfg            # bg_ndi_wi's c3_bg has no time block
    assert render(BG_C3, run.ctx) == cfg


def test_render_binds_c4_inputs_to_this_runs_c3_outputs(run):
    cfg = render(catalog.recipe("bg_ndi_wi").steps["c4_ndi"], run.ctx)
    assert cfg["source"]["file"] == str(run.ctx.output_path("c3_bg"))
    assert cfg["source_2020"]["file"] == str(run.ctx.output_path("c3_bg_2020"))
    assert cfg["time"]["output_grouping"] == "episode"


def test_engine_none_keeps_the_yaml_and_engine_options_are_written(run):
    plain = render_text(NOISE_C3, run.ctx)
    same = dataclasses.replace(run.ctx, engine={"threads": None})
    assert render_text(NOISE_C3, same) == plain
    capped = dataclasses.replace(run.ctx, engine={"threads": 2, "memory_limit": "4GB",
                                                  "temp_directory": "/tmp/x"})
    cfg = render(NOISE_C3, capped)
    assert cfg["engine"] == {"backend": "duckdb", "threads": 2, "memory_limit": "4GB",
                             "temp_directory": "/tmp/x"}
    from spacescans.models.config import EngineConfig
    EngineConfig(**cfg["engine"])       # the pipeline accepts it


def test_write_config_writes_the_webs_file(run):
    path = write_config(BG_C3, run.ctx)
    assert path == run.ctx.run_dir / "pipeline_configs" / "c3_bg.yaml"
    assert path.read_text() == yaml.safe_dump(render(BG_C3, run.ctx), sort_keys=False)
    other = write_config(NOISE_C3, run.ctx, config={"name": "given"})
    assert yaml.safe_load(other.read_text()) == {"name": "given"}


def test_render_reports_a_template_without_the_bound_mapping(run):
    broken = dataclasses.replace(catalog.recipe("noise").steps["c4_noise"],
                                 bind={"nowhere.file": "c3_noise"})
    with pytest.raises(ValueError, match="no mapping 'nowhere'"):
        render(broken, run.ctx)


def test_prepare_is_a_no_op_except_for_raster_values(run):
    assert prepare(BG_C3, run.ctx) == []
    assert prepare(catalog.recipe("noise").steps["c4_noise"], run.ctx) == []


def test_custom_render_is_orchestrate_render_of_the_planned_step(run, tmp_path):
    values = tmp_path / "v.csv"
    values.write_text("GEOID10,v\n180970001001,1.0\n", encoding="utf-8")
    d = custom.define_polygon(values, boundary="BG", key_col="GEOID10", value_cols=["v"],
                              label="x")
    key = d["variable_key"]
    ctx = dataclasses.replace(run.ctx, overlay={key: d})
    steps = catalog.plan("custom", [key], overlay=ctx.overlay)
    assert custom.plan([key], ctx.overlay) == steps
    for step in steps:
        bare = custom.c4_step_for(key) if not step.is_c3 else step
        assert custom.render(bare, catalog.template(step.template), ctx, variables=[key],
                             definitions=ctx.overlay) == render(step, ctx)


# ---------------------------------------------------------------------------------------------
# legacy-v1
# ---------------------------------------------------------------------------------------------

def test_legacy_key_format(run):
    import hashlib
    sha8 = hashlib.sha256(run.ctx.cohort_parquet.read_bytes()).hexdigest()[:8]
    legacy = WeightCache(run.tmp / "c3_cache", key_format="legacy-v1")
    assert legacy.key(NOISE_C3, run.ctx) == f"{sha8}__BG_NOISE__b270m"
    assert legacy.key(BG_C3, run.ctx) == f"{sha8}__BG__b270m__r25m"
    assert legacy.key(BG_C3, dataclasses.replace(run.ctx, buffer_m=270.0)) == \
        f"{sha8}__BG__b270.0m__r25m"
    sha = hashlib.sha256(run.ctx.cohort_parquet.read_bytes()).hexdigest()
    for step in (NOISE_C3, BG_C3):        # the catalog wrapper agrees
        assert catalog.legacy_cache_key(step, sha, buffer_m=270, raster_res_m=25) == \
            legacy.key(step, run.ctx)
    # custom's web wrapper takes a tag (it knows only the upload tags, as the web runner does)
    assert custom.legacy_cache_key(run.ctx.cohort_parquet, "BG", buffer_m=270, raster_res_m=25) \
        == legacy.key(BG_C3, run.ctx)
    with pytest.raises(ValueError, match="not a cached C3 step"):
        legacy.key(catalog.recipe("noise").steps["c4_noise"], run.ctx)


@pytest.mark.parametrize("step,fields", [
    (BG2020_C3, ["sha_full", "boundary", "buffer_m", "raster_res_m", "input_row_count",
                 "wall_clock_seconds", "file_size_bytes", "created_at"]),
    (TRACT_C3, ["sha_full", "boundary", "buffer_m", "input_row_count", "wall_clock_seconds",
                "file_size_bytes", "created_at"]),
])
def test_legacy_meta_has_the_webs_fields_and_format(run, step, fields):
    (run.ctx.run_dir / "input.csv").write_text("pid,x\nA,1\nB,2\nC,3\n")
    legacy = WeightCache(run.tmp / "c3_cache", key_format="legacy-v1")
    key = legacy.store(step, run.ctx, run.out, seconds=12.9)
    text = legacy.meta_path(key).read_text()
    meta = json.loads(text)
    assert list(meta) == fields
    assert text == json.dumps(meta, indent=2)            # no trailing newline, as the web
    assert meta["boundary"] == ("BG" if step is BG2020_C3 else "TRACT_FARA")
    assert (meta["input_row_count"], meta["wall_clock_seconds"]) == (3, 12)
    assert meta["file_size_bytes"] == run.out.stat().st_size
    assert legacy.path(key).read_bytes() == run.out.read_bytes()


def test_legacy_hit_needs_a_readable_parquet_of_100_bytes(run):
    legacy = WeightCache(run.tmp / "c3_cache", key_format="legacy-v1")
    key = legacy.key(NOISE_C3, run.ctx)
    dst = run.tmp / "dst.parquet"
    assert legacy.fetch(NOISE_C3, run.ctx, dst) is None
    legacy.root.mkdir()
    legacy.path(key).write_bytes(b"x" * 99)
    assert legacy.fetch(NOISE_C3, run.ctx, dst) is None
    legacy.path(key).write_bytes(b"not a parquet file" * 20)
    assert legacy.fetch(NOISE_C3, run.ctx, dst) is None
    assert legacy.store(NOISE_C3, run.ctx, run.out, seconds=1.0) == key   # parquet + the web's meta
    assert legacy.fetch(NOISE_C3, run.ctx, dst) == key
    assert dst.read_bytes() == run.out.read_bytes()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root may search a mode-000 directory")
def test_legacy_lookup_raises_what_the_webs_lookup_raised(run):
    """Phase-C review F1: only a missing entry is a miss. The web's check called
    Path.exists() outside its try, so a cache directory the process may not search
    raised PermissionError, and the runner logged "cache check failed" instead."""
    dst = run.tmp / "dst.parquet"
    (run.tmp / "c3_file").write_text("not a directory\n")      # ENOTDIR: absent, a miss
    assert WeightCache(run.tmp / "c3_file", key_format="legacy-v1") \
        .fetch(NOISE_C3, run.ctx, dst) is None
    legacy = WeightCache(run.tmp / "c3_cache", key_format="legacy-v1")
    legacy.root.mkdir()
    legacy.path(legacy.key(NOISE_C3, run.ctx)).write_bytes(run.out.read_bytes())
    os.chmod(legacy.root, 0)
    try:
        with pytest.raises(PermissionError):
            legacy.fetch(NOISE_C3, run.ctx, dst)
    finally:
        os.chmod(legacy.root, 0o700)
    assert not dst.exists()


def _same_prefix_other_hash(sha: str) -> str:
    """Another full sha256 with the same 8-digit prefix (what two colliding cohorts give)."""
    return sha[:8] + ("1" if sha[8] == "0" else "0") + sha[9:]


def test_legacy_hit_is_refused_when_its_meta_records_another_input(run):
    """Phase-C review PRIV-1: the key holds 8 hex digits of the input's sha256, so another
    cohort can share it. An entry whose meta records a different sha_full is that cohort's
    and is a miss; one whose meta records this input's hash is a hit."""
    legacy = WeightCache(run.tmp / "c3_cache", key_format="legacy-v1")
    key = legacy.store(NOISE_C3, run.ctx, run.out, seconds=1.0)
    meta_path = legacy.meta_path(key)
    meta = json.loads(meta_path.read_text())
    dst = run.tmp / "dst.parquet"
    assert legacy.fetch(NOISE_C3, run.ctx, dst) == key          # its own input: a hit
    dst.unlink()
    meta_path.write_text(json.dumps({**meta, "sha_full": _same_prefix_other_hash(meta["sha_full"])},
                                    indent=2))
    assert legacy.fetch(NOISE_C3, run.ctx, dst) is None and not dst.exists()


def _legacy_entry_with_meta(run, case: str):
    """A stored legacy entry for ``run``'s input whose meta is then made ``case``."""
    legacy = WeightCache(run.tmp / "c3_cache", key_format="legacy-v1")
    key = legacy.store(NOISE_C3, run.ctx, run.out, seconds=1.0)
    meta_path = legacy.meta_path(key)
    meta = json.loads(meta_path.read_text())
    assert meta["sha_full"] == orch_cache.sha256_file(run.ctx.cohort_parquet)
    if case == "missing":
        meta_path.unlink()
    elif case == "unreadable":
        meta_path.write_text('{"sha_full": "' + meta["sha_full"])   # cut short: not JSON
    elif case == "unreadable-mode-000":
        os.chmod(meta_path, 0)
    elif case == "no-sha_full":
        meta_path.write_text(json.dumps({k: v for k, v in meta.items() if k != "sha_full"},
                                        indent=2))
    elif case == "sha_full-null":
        meta_path.write_text(json.dumps({**meta, "sha_full": None}, indent=2))
    elif case == "not-a-mapping":
        meta_path.write_text(json.dumps([meta["sha_full"]]))
    else:
        raise AssertionError(case)
    return legacy, key


@pytest.mark.parametrize("case", [
    "missing",
    "unreadable", pytest.param("unreadable-mode-000", marks=pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads a mode-000 file")),
    "no-sha_full", "sha_full-null", "not-a-mapping",
])
def test_legacy_entry_without_its_inputs_hash_in_the_meta_is_a_miss(run, case):
    """Project-lead decision on PRIV-1: an entry whose meta is missing, unreadable or has no
    sha_full cannot show whose weights it holds, so it is a miss (recomputed), never a hit.
    The parquet is the right one here: only the meta decides."""
    legacy, key = _legacy_entry_with_meta(run, case)
    dst = run.tmp / "dst.parquet"
    try:
        assert legacy.fetch(NOISE_C3, run.ctx, dst) is None and not dst.exists()
    finally:
        if legacy.meta_path(key).exists():
            os.chmod(legacy.meta_path(key), 0o600)
    assert legacy.store(NOISE_C3, run.ctx, run.out, seconds=1.0) == key   # the fresh run's store
    assert legacy.fetch(NOISE_C3, run.ctx, dst) == key                  # ... repairs the entry


def test_a_failed_legacy_store_leaves_no_meta_of_another_input(run, monkeypatch):
    """The parquet and its meta are two writes. A store that fails between them (or is
    killed) must not leave the new parquet beside the old meta: under a shared key that
    meta can name another cohort, which would then be served these weights."""
    legacy = WeightCache(run.tmp / "c3_cache", key_format="legacy-v1")
    key = legacy.store(NOISE_C3, run.ctx, run.out, seconds=1.0)
    meta = json.loads(legacy.meta_path(key).read_text())
    other = _same_prefix_other_hash(meta["sha_full"])
    legacy.meta_path(key).write_text(json.dumps({**meta, "sha_full": other}, indent=2))

    def no_meta(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(legacy, "legacy_meta", no_meta)
    with pytest.raises(OSError):
        legacy.store(NOISE_C3, run.ctx, run.out, seconds=1.0)
    assert legacy.path(key).read_bytes() == run.out.read_bytes()
    assert not legacy.meta_path(key).exists()          # not the other input's meta
    assert legacy.fetch(NOISE_C3, run.ctx, run.tmp / "dst.parquet") is None


# ---------------------------------------------------------------------------------------------
# v2
# ---------------------------------------------------------------------------------------------

def test_v2_key_is_deterministic_and_named_as_specified(run):
    v2 = WeightCache(run.tmp / "cache", key_format="v2")
    key = v2.key(NOISE_C3, run.ctx)
    assert key == v2.key(NOISE_C3, run.ctx) == WeightCache(run.tmp / "other", key_format="v2") \
        .key(NOISE_C3, run.ctx)
    cohort8, tag, buf, sha12 = key.split("__")
    assert cohort8 == orch_cache.cohort_fingerprint(run.ctx.cohort_parquet)[:8]
    assert (tag, buf, len(sha12)) == ("BG_NOISE", "b270m", 12)
    areal = v2.key(BG_C3, run.ctx).split("__")
    assert areal[1:4] == ["BG", "b270m", "r25m"] and len(areal[4]) == 12
    assert v2.path(key) == run.tmp / "cache" / "c3" / "v2" / f"{key}.parquet"


def test_v2_key_ignores_what_does_not_change_the_weights(run, tmp_path):
    v2 = WeightCache(run.tmp / "cache", key_format="v2")
    key = v2.key(NOISE_C3, run.ctx)
    elsewhere = RunContext(run_dir=tmp_path / "elsewhere", cohort_parquet=run.ctx.cohort_parquet,
                           data_dir=run.ctx.data_dir, name_suffix="_task_ffffffff",
                           engine={"threads": 3, "memory_limit": "1GB",
                                   "temp_directory": str(tmp_path)})
    assert v2.key(NOISE_C3, elsewhere) == key
    # PATID and dates join at C4: a cohort with other pids keeps the C3 key
    other_pids = _cohort(tmp_path / "pids.parquet", pids=[f"Q{i}" for i in range(6)])
    assert v2.key(NOISE_C3, dataclasses.replace(run.ctx, cohort_parquet=other_pids)) == key


def _change_receipt(run, _mp=None):
    from spacescans.data import load_manifest
    from spacescans.data.receipts import receipt_path
    (artifact,) = load_manifest().datasets["noise_rasters"].artifacts
    p = receipt_path(run.data, artifact)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"receipt_version": 1, "artifact": artifact, "sha256": "ab" * 32,
                             "files": []}))


COMPONENT_CHANGES = {
    "schema": lambda run, mp: mp.setattr(orch_cache, "V2_SCHEMA", 3),
    "spacescans version": lambda run, mp: mp.setattr(spacescans, "__version__", "9.9.9"),
    "step code": lambda run, mp: mp.setattr(orch_cache, "step_code_fingerprint", lambda: "sha256:0"),
    "native libraries": lambda run, mp: mp.setattr(
        orch_cache, "native_versions", lambda: {"exactextract": "0", "gdal": "0", "geos": "0",
                                                "proj": "0"}),
    "buffer": lambda run, mp: mp.setattr(run, "ctx", dataclasses.replace(run.ctx, buffer_m=500)),
    "coordinates": lambda run, mp: mp.setattr(run, "ctx", dataclasses.replace(
        run.ctx, cohort_parquet=_cohort(run.tmp / "moved.parquet", lon_shift=1e-9))),
    "data file": lambda run, mp: (run.data / "Noise" / "C3" / "CONUS_L50dBA_sumDay_exi.tif")
        .write_bytes(b"other grid"),
    "data receipt": _change_receipt,
    "config": lambda run, mp: mp.setattr(run, "step", dataclasses.replace(
        NOISE_C3, fill={"buffer.grid_id_offset": 1})),
    "pattern": lambda run, mp: mp.setattr(run, "step", dataclasses.replace(
        NOISE_C3, fill={"linkage_pattern": "boundary_overlap_fast"})),
    "tag": lambda run, mp: mp.setattr(run, "step", dataclasses.replace(NOISE_C3,
                                                                        cache_tag="BG_NOISE2")),
}


@pytest.mark.parametrize("component", list(COMPONENT_CHANGES))
def test_v2_changing_any_component_changes_the_key_and_misses(run, monkeypatch, component):
    v2 = WeightCache(run.tmp / "cache", key_format="v2")
    run.step = NOISE_C3
    key = v2.store(run.step, run.ctx, run.out, seconds=1.0)
    assert v2.fetch(run.step, run.ctx, run.tmp / "hit.parquet") == key
    COMPONENT_CHANGES[component](run, monkeypatch)
    assert v2.key(run.step, run.ctx) != key
    assert v2.fetch(run.step, run.ctx, run.tmp / "miss.parquet") is None


def test_v2_meta_records_every_component_and_no_cohort_value(run):
    v2 = WeightCache(run.tmp / "cache", key_format="v2")
    key = v2.store(NOISE_C3, run.ctx, run.out, seconds=2.5)
    meta = json.loads(v2.meta_path(key).read_text())
    comps = meta["components"]
    assert set(comps) == {"schema", "spacescans", "pattern", "tag", "config", "cohort_fp",
                          "source_fp", "native"}
    assert comps["schema"] == 2 and comps["spacescans"]["version"] == spacescans.__version__
    assert set(comps["native"]) == {"exactextract", "gdal", "geos", "proj"}
    assert comps["pattern"] == "grid_weights" and comps["tag"] == "BG_NOISE"
    assert "name" not in comps["config"] and "path" not in comps["config"]["output"]
    assert "patient_file" not in comps["config"]["buffer"]
    assert comps["source_fp"]["files"][0][0] == "Noise/C3/CONUS_L50dBA_sumDay_exi.tif"
    assert meta["file"]["rows"] == 3 and meta["seconds"] == 2.5
    text = v2.meta_path(key).read_text()
    for secret in ("P0", "-86.1", "39.7", str(run.ctx.cohort_parquet), "2015-01-01"):
        assert secret not in text


def test_v2_read_needs_every_recorded_component_and_the_parquet(run):
    v2 = WeightCache(run.tmp / "cache", key_format="v2")
    key = v2.store(NOISE_C3, run.ctx, run.out, seconds=1.0)
    dst = run.tmp / "dst.parquet"
    meta_path = v2.meta_path(key)
    good = meta_path.read_text()

    meta = json.loads(good)
    meta["components"]["native"]["gdal"] = "tampered"
    meta_path.write_text(json.dumps(meta))
    assert v2.fetch(NOISE_C3, run.ctx, dst) is None

    meta = json.loads(good)
    del meta["components"]["source_fp"]
    meta_path.write_text(json.dumps(meta))
    assert v2.fetch(NOISE_C3, run.ctx, dst) is None

    meta_path.write_text(good)
    data = bytearray(v2.path(key).read_bytes())
    data[10] ^= 0xFF                    # same size, other bytes
    v2.path(key).write_bytes(bytes(data))
    assert v2.fetch(NOISE_C3, run.ctx, dst) is None and not dst.exists()

    v2.store(NOISE_C3, run.ctx, run.out, seconds=1.0)
    meta_path.unlink()
    assert v2.fetch(NOISE_C3, run.ctx, dst) is None
    v2.store(NOISE_C3, run.ctx, run.out, seconds=1.0)
    assert v2.fetch(NOISE_C3, run.ctx, dst) == key and dst.read_bytes() == run.out.read_bytes()


def test_v2_store_is_atomic(run, monkeypatch):
    v2 = WeightCache(run.tmp / "cache", key_format="v2")
    key = v2.key(NOISE_C3, run.ctx)
    real_replace = os.replace
    calls = []

    def crash_on_first(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise OSError("disk went away")
        return real_replace(src, dst)

    monkeypatch.setattr(orch_cache.os, "replace", crash_on_first)
    with pytest.raises(OSError, match="disk went away"):
        v2.store(NOISE_C3, run.ctx, run.out, seconds=1.0)
    monkeypatch.setattr(orch_cache.os, "replace", real_replace)
    assert not v2.path(key).exists() and not v2.meta_path(key).exists()
    assert not list(v2.directory.glob(".*.tmp"))          # temp files cleaned up
    assert v2.fetch(NOISE_C3, run.ctx, run.tmp / "dst.parquet") is None
    assert v2.info()["entries"] == 0
    # the entry appears only complete: parquet first, then the meta that validates it
    assert v2.store(NOISE_C3, run.ctx, run.out, seconds=1.0) == key
    assert v2.fetch(NOISE_C3, run.ctx, run.tmp / "dst.parquet") == key


def test_v2_store_refuses_an_unreadable_output(run):
    bad = run.tmp / "bad.parquet"
    bad.write_bytes(b"x" * 500)
    with pytest.raises(ValueError, match="not a readable parquet"):
        WeightCache(run.tmp / "cache", key_format="v2").store(NOISE_C3, run.ctx, bad, seconds=1)


def test_v2_store_waits_for_the_keys_lock(run):
    v2 = WeightCache(run.tmp / "cache", key_format="v2")
    key = v2.key(NOISE_C3, run.ctx)
    done = threading.Event()

    def store():
        v2.store(NOISE_C3, run.ctx, run.out, seconds=1.0)
        done.set()

    with v2._lock(key) as got:
        assert got
        t = threading.Thread(target=store)
        t.start()
        assert not done.wait(0.5)          # blocked while another writer holds the key
        other = v2.key(BG_C3, run.ctx)
        with v2._lock(other) as got_other:  # one lock per key: other keys are free
            assert got_other
    assert done.wait(10)
    t.join()
    assert v2.fetch(NOISE_C3, run.ctx, run.tmp / "dst.parquet") == key
    assert (v2.directory / ".locks" / f"{key}.lock").exists()


def test_v2_directories_are_private(run, monkeypatch):
    old = os.umask(0o022)
    try:
        v2 = WeightCache(run.tmp / "cache" / "root", key_format="v2")
        key = v2.store(NOISE_C3, run.ctx, run.out, seconds=1.0)
    finally:
        os.umask(old)
    for d in (run.tmp / "cache", v2.root, v2.root / "c3", v2.directory, v2.directory / ".locks"):
        assert _mode(d) == 0o700, d
    assert _mode(v2.path(key)) == 0o600 and _mode(v2.meta_path(key)) == 0o600


def test_info_and_clear(run):
    for fmt in ("legacy-v1", "v2"):
        cache = WeightCache(run.tmp / f"cache-{fmt}", key_format=fmt)
        assert cache.info()["entries"] == 0 and cache.info()["oldest"] is None
        old_key = cache.store(NOISE_C3, run.ctx, run.out, seconds=1.0)
        new_key = cache.store(BG_C3, run.ctx, run.out, seconds=1.0)
        past = time.time() - 7200
        os.utime(cache.path(old_key), (past, past))
        info = cache.info()
        assert info["entries"] == 2 and info["key_format"] == fmt
        assert info["bytes"] == sum(p.stat().st_size for k in (old_key, new_key)
                                    for p in (cache.path(k), cache.meta_path(k)))
        assert info["oldest"] < info["newest"]
        old_bytes = cache.path(old_key).stat().st_size + cache.meta_path(old_key).stat().st_size
        assert cache.clear(older_than_s=3600) == {"removed": 1, "kept": 1, "bytes": old_bytes}
        assert not cache.path(old_key).exists() and cache.path(new_key).exists()
        assert cache.info()["bytes"] == info["bytes"] - old_bytes
        result = cache.clear()
        assert result["removed"] == 1 and result["kept"] == 0
        assert cache.info()["entries"] == 0


def test_v2_clear_leaves_an_entry_being_written(run):
    v2 = WeightCache(run.tmp / "cache", key_format="v2")
    key = v2.store(NOISE_C3, run.ctx, run.out, seconds=1.0)
    with v2._lock(key):
        assert v2.clear() == {"removed": 0, "kept": 1, "bytes": 0}
    assert v2.clear()["removed"] == 1


def test_v2_clear_removes_the_temp_copy_of_an_interrupted_store(run):
    """Review P3: a fresh temp copy (C3 weights) survived clear() and info() never counted it."""
    v2 = WeightCache(run.tmp / "cache", key_format="v2")
    key = v2.store(NOISE_C3, run.ctx, run.out, seconds=1.0)
    leftover = v2.directory / f".{key}.ab12cd34.parquet.tmp"      # named as store names it
    leftover.write_bytes(v2.path(key).read_bytes())
    leftover_bytes = leftover.stat().st_size
    busy = v2.directory / f".{v2.key(BG_C3, run.ctx)}.ef56ab78.parquet.tmp"
    busy.write_bytes(b"x" * 64)
    info = v2.info()
    assert (info["entries"], info["temp_files"]) == (1, 2)
    assert info["temp_bytes"] == leftover_bytes + 64
    # with an age limit, a temp file younger than both the limit and an hour stays
    assert v2.clear(older_than_s=7 * 86400)["removed"] == 0 and leftover.exists()
    entry_bytes = v2.path(key).stat().st_size + v2.meta_path(key).stat().st_size
    with v2._lock(v2.key(BG_C3, run.ctx)):              # a store is writing this one
        res = v2.clear()
    assert res["removed"] == 1 and res["bytes"] == entry_bytes + leftover_bytes
    assert not leftover.exists() and busy.exists()
    assert v2.info()["temp_files"] == 1
    assert v2.clear()["bytes"] == 64 and not busy.exists()
    assert v2.info()["temp_files"] == 0 and not any(v2.directory.glob(".*.tmp"))


def test_source_fingerprint_prefers_receipts(run):
    cfg = render(NOISE_C3, run.ctx)
    by_stat = orch_cache.source_fingerprint(cfg, run.data)
    assert by_stat["receipts"] == []
    assert [f[0] for f in by_stat["files"]] == ["Noise/C3/CONUS_L50dBA_sumDay_exi.tif"]
    _change_receipt(run)
    by_receipt = orch_cache.source_fingerprint(cfg, run.data)
    assert by_receipt["files"] == [] and by_receipt["receipts"][0][1] == "ab" * 32


def test_source_fingerprint_covers_shapefile_parts_and_missing_files(run):
    shp = run.data / "County" / "C3" / "tl_2010_us_county10"
    shp.mkdir(parents=True)
    for ext in ("shp", "shx", "dbf", "prj"):
        (shp / f"tl_2010_us_county10.{ext}").write_bytes(ext.encode())
    cfg = render(catalog.recipe("zcta5_cbp").steps["c3_county"], run.ctx)
    files = orch_cache.source_fingerprint(cfg, run.data)["files"]
    assert [Path(f[0]).suffix for f in files] == [".dbf", ".prj", ".shp", ".shx"]
    zcta = orch_cache.source_fingerprint(render(catalog.recipe("zcta5_cbp").steps["c3_zcta5"],
                                                run.ctx), run.data)["files"]
    assert zcta == [["ZCTA5/C3/tl_2010_us_zcta510/tl_2010_us_zcta510.shp", None, None]]


def test_v2_key_follows_the_tile_cache_a_c3_step_reads(run):
    """Review F2: replacing an NHD feature tile left the v2 key unchanged."""
    step = catalog.plan("nhd_bluespace", ["nhd_bluespace"])[0]
    tile = run.data / "cache" / "C3" / "nhd_features" / "tile_gx0_gy0_flow.parquet"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(b"v1" * 100)
    v2 = WeightCache(run.tmp / "cache", key_format="v2")
    comps = v2.components(step, run.ctx)
    assert "road_cache_dir" not in comps["config"]["source"]       # a location, not an input
    [tree] = comps["source_fp"]["trees"]
    assert tree[:3] == ["cache/C3/nhd_features", 1, 200]
    k1 = v2.key(step, run.ctx)
    tile.write_bytes(b"version two" * 50)
    k2 = v2.key(step, run.ctx)
    assert k1 != k2
    # a private tile folder (link's run_dir/feature_cache) is neither fingerprinted nor keyed
    cfg = render(step, run.ctx)
    cfg["source"]["road_cache_dir"] = str(run.tmp / "private" / "c3_nhd_bluespace")
    assert "trees" not in orch_cache.source_fingerprint(cfg, run.data)
    assert orch_cache.normalised_config(cfg) == comps["config"]
    # steps without a tile cache keep their source_fp shape
    assert "trees" not in orch_cache.source_fingerprint(render(NOISE_C3, run.ctx), run.data)


def test_unknown_key_format_is_refused(tmp_path):
    with pytest.raises(ValueError, match="key_format"):
        WeightCache(tmp_path, key_format="v3")


# ---------------------------------------------------------------------------------------------
# phase C: one v2 entry for the custom and the catalog c3_bg; receipts for the tile caches
# ---------------------------------------------------------------------------------------------

def test_custom_and_catalog_c3_bg_share_one_v2_key(run, tmp_path):
    """orch-core open issue 2: the custom path's c3_bg renders ``time: {output_grouping:
    episode}`` (a C4 setting no C3 pattern reads), bg_ndi_wi's does not; both must share one
    v2 entry as they share the legacy one. Legacy keys are unchanged (gate A2)."""
    csv = tmp_path / "vals.csv"
    csv.write_text("GEOID10,myval\n180973201001,1.5\n")
    d = custom.define_polygon(csv, boundary="BG", key_col="GEOID10", value_cols=["myval"])
    key = d["variable_key"]
    ctx = dataclasses.replace(run.ctx, overlay={key: d})
    catalog_step = catalog.plan("bg_ndi_wi", ["ndi"])[0]
    custom_step = next(s for s in catalog.plan("custom", [key], overlay={key: d}) if s.name == "c3_bg")
    assert catalog_step.name == custom_step.name == "c3_bg"
    cfg_catalog, cfg_custom = render(catalog_step, ctx), render(custom_step, ctx)
    assert "time" not in cfg_catalog and cfg_custom["time"] == {"output_grouping": "episode"}
    assert orch_cache.normalised_config(cfg_catalog) == orch_cache.normalised_config(cfg_custom)
    assert "time" not in orch_cache.normalised_config(cfg_custom)
    v2 = WeightCache(run.tmp / "cache", key_format="v2")
    assert v2.key(catalog_step, ctx) == v2.key(custom_step, ctx)
    assert v2.components(catalog_step, ctx) == v2.components(custom_step, ctx)
    legacy = WeightCache(run.tmp / "legacy", key_format="legacy-v1")
    assert legacy.key(catalog_step, ctx) == legacy.key(custom_step, ctx)
    assert legacy.key(catalog_step, ctx).endswith("__BG__b270m__r25m")
    # a stored custom entry is a hit for the catalog step
    out = run.ctx.run_dir / "output" / "c3_bg.parquet"
    pd.DataFrame({"geoid": [0, 1], "GEOID10": ["180973201001", "180973201001"],
                  "value": [1.0, 1.0]}).to_parquet(out, index=False)
    stored = v2.store(custom_step, ctx, out, seconds=1.0)
    assert v2.fetch(catalog_step, ctx, run.tmp / "hit.parquet") == stored
    # a C4 setting that is kept in a time block still counts
    cfg_years = json.loads(json.dumps(cfg_custom))
    cfg_years["time"]["years"] = [2015]
    assert orch_cache.normalised_config(cfg_years) == {**orch_cache.normalised_config(cfg_catalog),
                                                       "time": {"years": [2015]}}


def test_an_empty_time_block_is_kept_for_patterns_that_read_it(run):
    """tiger_proximity and proximity read ``config.time`` (years, temporal_mode): an empty
    block (``time: {}``, years None) and no block (years 2013-2019) compute different things,
    so only ``output_grouping`` goes from their key, never the block."""
    step = catalog.plan("tiger_proximity", ["tiger_proximity"])[0]
    cfg = render(step, run.ctx)
    assert cfg["linkage_pattern"] == "tiger_proximity" and cfg["time"]["output_grouping"] == "episode"
    norm = orch_cache.normalised_config(cfg)
    assert "output_grouping" not in norm["time"] and norm["time"]["years"] == list(range(2013, 2020))
    for pattern in ("tiger_proximity", "proximity"):
        bare = {"linkage_pattern": pattern, "source": {"file": "x.shp"}}
        only_grouping = {**bare, "time": {"output_grouping": "episode"}}
        assert orch_cache.normalised_config(only_grouping) == {**bare, "time": {}}
        assert orch_cache.normalised_config(only_grouping) != orch_cache.normalised_config(bare)
    for pattern in sorted(orch_cache._TIMELESS_C3_PATTERNS):
        bare = {"linkage_pattern": pattern, "source": {"file": "x.shp"}}
        assert orch_cache.normalised_config({**bare, "time": {"output_grouping": "episode"}}) == bare


def test_timeless_c3_patterns_never_read_the_time_block():
    """The guard behind ``_TIMELESS_C3_PATTERNS``: neither those pattern modules nor what they
    call (geometry, io, ``linkage.helpers.load_patients``) read ``config.time``. A computation
    change that starts reading it must take the pattern out of the set."""
    import inspect
    import re
    from importlib import import_module

    reads_time = re.compile(r"\b(?:config|cfg|self\.config)\.time\b|getattr\([^)]*['\"]time['\"]")
    modules = {"boundary_overlap": "boundary_overlap_linkage",
               "boundary_overlap_fast": "boundary_overlap_fast_linkage",
               "grid_weights": "grid_weights_linkage", "nhd_proximity": "nhd_proximity_linkage"}
    assert set(modules) == orch_cache._TIMELESS_C3_PATTERNS
    base = Path(spacescans.__file__).resolve().parent
    sources = {f"linkage/{m}.py": (base / "linkage" / f"{m}.py").read_text() for m in modules.values()}
    for package in ("geometry", "io"):
        for path in sorted((base / package).rglob("*.py")):
            sources[path.relative_to(base).as_posix()] = path.read_text()
    helpers = import_module("spacescans.linkage.helpers")
    sources["linkage/helpers.py:load_patients"] = inspect.getsource(helpers.load_patients)
    readers = {name: m.group(0) for name, text in sources.items() if (m := reads_time.search(text))}
    assert readers == {}
    # and the regex does find the reads it is meant to find
    assert reads_time.search((base / "linkage" / "tiger_proximity_linkage.py").read_text())


def _tile_cache_root(run, folder: str, n: int = 3) -> list[Path]:
    tiles = []
    for i in range(n):
        tile = run.data / folder / (f"2013/tl_2013_{i:05d}_roads.parquet" if "tiger" in folder
                                    else f"tile_gx{i}_gy0_flow.parquet")
        tile.parent.mkdir(parents=True, exist_ok=True)
        tile.write_bytes(b"tile" * (10 + i))
        tiles.append(tile)
    return tiles


@pytest.mark.parametrize("variable,step,folder,artifact", [
    ("tiger_proximity", "c3_tiger_roads", "cache/C3/tiger_roads_filtered",
     "tiger_roads_filtered_cache_v1.tar.gz"),
    ("nhd_bluespace", "c3_nhd_bluespace", "cache/C3/nhd_features", "nhd_features_cache_v1.tar.gz")])
def test_tile_cache_receipts_make_the_v2_fingerprint_instant(run, monkeypatch, variable, step,
                                                             folder, artifact):
    """Phase-B leftover (fix #5): without a receipt the tile cache a TIGER/NHD C3 step reads is
    fingerprinted by walking its tree (about 0.4-0.6 s on the real root, twice per step); the
    receipt ``data.fetch``/``adopt`` write for the cache's artifact replaces the walk by the
    artifact's sha256."""
    from spacescans.data import load_manifest
    from spacescans.data.receipts import read_receipt, write_receipt

    man = load_manifest()
    art = man.artifacts[artifact]
    assert folder in art.roots or any(r.rstrip("/") == folder for r in art.roots)
    tiles = _tile_cache_root(run, folder)
    planned = next(s for s in catalog.plan(variable, [variable]) if s.name == step)
    cfg = render(planned, run.ctx)
    assert cfg["source"]["road_cache_dir"] == folder          # the data root's prebuilt cache
    v2 = WeightCache(run.tmp / "cache", key_format="v2")

    without = orch_cache.source_fingerprint(cfg, run.data)
    [tree] = without["trees"]
    assert tree[:3] == [folder, 3, sum(t.stat().st_size for t in tiles)]
    assert not any(name == artifact for name, _sha in without["receipts"])
    key_without = v2.key(planned, run.ctx)

    files = [(t.relative_to(run.data).as_posix(), t.stat().st_size, orch_cache.sha256_file(t))
             for t in tiles]
    write_receipt(run.data, art, files=files, source="test", origin="adopt",
                  data_release=man.data_release)
    assert read_receipt(run.data, artifact)["sha256"] == art.sha256

    def never(ref, data_dir):
        raise AssertionError(f"the tree {ref} was walked although its receipt covers it")

    monkeypatch.setattr(orch_cache, "_tree_entry", never)
    with_receipt = orch_cache.source_fingerprint(cfg, run.data)
    assert "trees" not in with_receipt
    assert [artifact, art.sha256] in with_receipt["receipts"]
    key_with = v2.key(planned, run.ctx)
    assert key_with != key_without
    # the receipt identifies the data: touching a tile no longer changes the key, deleting
    # the receipt brings the walk (and the old key) back
    tiles[0].write_bytes(b"changed" * 20)
    assert v2.key(planned, run.ctx) == key_with
    monkeypatch.undo()
    (run.data / ".spacescans" / "receipts" / f"{artifact}.json").unlink()
    assert "trees" in orch_cache.source_fingerprint(cfg, run.data)


def test_tile_tree_fingerprint_ignores_macos_metadata(run):
    """Finder's .DS_Store and AppleDouble files are rewritten behind the pipeline's back and no
    step reads them: they must not move a v2 key (the real TIGER cache carries a .DS_Store)."""
    folder = "cache/C3/nhd_features"
    _tile_cache_root(run, folder)
    before = orch_cache._tree_entry(folder, run.data)
    (run.data / folder / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (run.data / folder / "._tile_gx0_gy0_flow.parquet").write_bytes(b"\x00\x05\x16\x07")
    (run.data / folder / "__MACOSX").mkdir()
    (run.data / folder / "__MACOSX" / "._x").write_bytes(b"junk")
    assert orch_cache._tree_entry(folder, run.data) == before
    (run.data / folder / "tile_gx9_gy9_flow.parquet").write_bytes(b"real")
    after = orch_cache._tree_entry(folder, run.data)
    assert after[1] == before[1] + 1 and after[3] != before[3]
