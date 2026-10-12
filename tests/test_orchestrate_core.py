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
    legacy.path(key).write_bytes(run.out.read_bytes())    # no meta needed, as in the web
    assert legacy.fetch(NOISE_C3, run.ctx, dst) == key
    assert dst.read_bytes() == run.out.read_bytes()


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


def test_unknown_key_format_is_refused(tmp_path):
    with pytest.raises(ValueError, match="key_format"):
        WeightCache(tmp_path, key_format="v3")
