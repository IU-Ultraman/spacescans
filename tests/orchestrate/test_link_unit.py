"""orchestrate.run_source and orchestrate.link with a fake executor (no pipeline, no data).

The fake executor reads each rendered config and writes a plausible output parquet
(C3: geoid/grid_id/weight; C4: PATID, geoid and the variable's value columns), so the
web-runner semantics can be checked fast: cache before render, no YAML on a hit, one C3
per link, failure and cancel handling, the dispatcher's stop/continue rule, the fan-in
name rule, the custom definition check (#15), the data check (#17), coverage, provenance
and the web's log lines. Gate A4 against the real pipeline is test_link_a4.py.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
import threading
from pathlib import Path

import pandas as pd
import pytest
import yaml

from spacescans import catalog
from spacescans.orchestrate import (
    LOG_CACHE_HIT,
    LOG_REUSED,
    LinkConfigError,
    LogWriter,
    RenderError,
    RunCancelled,
    RunContext,
    StepFailed,
    StepRun,
    WeightCache,
    link,
    partial,
    run_source,
    tee,
)
from spacescans.orchestrate import run as run_mod

# the module (the package attribute ``link`` is the function)
link_fn = importlib.import_module("spacescans.orchestrate.link")

COHORT = pd.DataFrame({
    "pid": ["A1", "A1", "B2", "C3"],
    "startDate": ["2014-01-01", "2016-01-01", "2015-03-01", "2013-06-01"],
    "endDate": ["2015-12-31", "2018-12-31", "2019-12-31", "2017-01-01"],
    "longitude": [-86.15, -86.10, -87.62, -84.39],
    "latitude": [39.77, 39.80, 41.88, 33.75],
})


def _value_cols_for(step_name: str) -> list[str]:
    """The pre-rename value columns the C4 output of ``step_name`` carries."""
    for rec in catalog.all_recipes().values():
        if rec.dynamic:
            continue
        inverse = {v: k for k, v in rec.renames.items()}
        for key in rec.variables:
            if rec.output_for(key) == f"{step_name}.parquet":
                return [inverse.get(c, c) for c in catalog.variable(key).value_cols]
    return ["intermediate"]


class FakeExecutor:
    """Writes each step's output from its rendered config; records what ran."""

    def __init__(self, *, fail=(), cancel_on=None, missing_value_rows=()):
        self.calls: list[str] = []
        self.fail = set(fail)
        self.cancel_on = cancel_on
        self.missing_value_rows = set(missing_value_rows)

    def command(self, config, data_dir):
        return ["fake-spacescans", "run", "--data-dir", os.fspath(data_dir), os.fspath(config)]

    def run(self, config, *, data_dir, cwd, on_line, cancel=None):
        cfg = yaml.safe_load(Path(config).read_text())
        name = Path(config).stem
        self.calls.append(name)
        on_line(f"[fake] {name} start")
        on_line("[overlap_fast] tile 5/10 ( 50.0%)")
        if self.cancel_on == name and cancel is not None:
            cancel.set()
            return StepRun(rc=-15, seconds=0.01, maxrss_bytes=None, tail=["stopped"],
                           cancelled=True)
        if name in self.fail:
            return StepRun(rc=3, seconds=0.02, maxrss_bytes=123, tail=["Traceback", "boom"])
        cohort = pd.read_parquet(cfg["buffer"]["patient_file"])
        out = Path(cfg["output"]["path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        if name.startswith("c3_"):
            df = pd.DataFrame({"geoid": cohort["episode_id"], "grid_id": cohort["episode_id"] + 7,
                               "weight": 1.0})
        else:
            cols = (cfg.get("exposure", {}).get("value_cols") if name.startswith("c4_custom_")
                    else _value_cols_for(name))
            df = pd.DataFrame({"PATID": cohort["pid"], "geoid": cohort["episode_id"]})
            for j, col in enumerate(cols):
                vals = cohort["episode_id"].astype(float) * 0.5 + j
                df[col] = [None if i in self.missing_value_rows else v
                           for i, v in enumerate(vals)]
        df.to_parquet(out, index=False)
        return StepRun(rc=0, seconds=0.05, maxrss_bytes=1000 + len(self.calls), tail=[])


@pytest.fixture
def no_data_check(monkeypatch):
    """Skip data.status (unit tests have no data root); test_data_missing covers it."""
    monkeypatch.setattr(link_fn, "_check_data",
                        lambda groups, overlay, data_dir: ({"data_dir": str(data_dir),
                                                            "data_release": "test",
                                                            "variables": {}, "datasets": {}}, []))


def _ctx(tmp_path, **kw) -> RunContext:
    from spacescans import cohort

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    cohort.to_pipeline_parquet(COHORT, run_dir / "input.parquet", pid_as_str=True,
                               columns="required")
    return RunContext(run_dir=run_dir, cohort_parquet=run_dir / "input.parquet",
                      data_dir=tmp_path / "data", **kw)


def _events():
    seen = []
    return seen, seen.append


# ---------------------------------------------------------------------------
# run_source
# ---------------------------------------------------------------------------

def test_run_source_runs_and_stores_c3_then_hits_without_rendering(tmp_path):
    ctx = _ctx(tmp_path)
    cache = WeightCache(tmp_path / "cache", key_format="legacy-v1")
    seen, emit = _events()
    res = run_source("noise", ["noise"], ctx, cache=cache, executor=FakeExecutor(), on_event=emit)
    assert [(s.name, s.status) for s in res.steps] == [("c3_noise", "ran"), ("c4_noise", "ran")]
    key = res.steps[0].cache_key
    assert key and cache.path(key).is_file() and cache.meta_path(key).is_file()
    kinds = [e.kind for e in seen]
    assert kinds[:4] == ["plan", "step_start", "spawn", "line"]
    assert "progress" in kinds and kinds.count("cache_write") == 1
    assert res.outputs == {"noise": ctx.run_dir / "output" / "c4_noise.parquet"}

    # A second run in a new run dir: the C3 step is a cache hit and renders no YAML.
    ctx2 = _ctx(tmp_path / "second")
    seen2, emit2 = _events()
    exe = FakeExecutor()
    res2 = run_source("noise", ["noise"], ctx2, cache=cache, executor=exe, on_event=emit2)
    assert [(s.name, s.status) for s in res2.steps] == [("c3_noise", "cache_hit"),
                                                         ("c4_noise", "ran")]
    assert exe.calls == ["c4_noise"]
    assert not (ctx2.run_dir / "pipeline_configs" / "c3_noise.yaml").exists()
    hit = next(e for e in seen2 if e.kind == "cache_hit")
    assert hit.text == LOG_CACHE_HIT.format(key=key)
    assert (ctx2.run_dir / "output" / "c3_noise.parquet").is_file()


def test_run_source_logs_the_web_lines(tmp_path):
    ctx = _ctx(tmp_path)
    cache = WeightCache(tmp_path / "cache", key_format="legacy-v1")
    log = LogWriter.for_run_dir(ctx.run_dir)
    run_source("noise", ["noise"], ctx, cache=cache, executor=FakeExecutor(), on_event=log)
    recs = [json.loads(line) for line in (ctx.run_dir / "logs.jsonl").read_text().splitlines()]
    msgs = [(r["level"], r["source"], r["msg"]) for r in recs if r["source"] != "c4_noise"
            and not r["msg"].startswith("[")]
    key = WeightCache(tmp_path / "cache", key_format="legacy-v1").key(
        catalog.plan("noise", ["noise"])[0], ctx)
    cfg = ctx.run_dir / "pipeline_configs"
    assert msgs == [
        ("info", "runner", (f"spawning c3_noise: fake-spacescans run --data-dir {ctx.data_dir} "
                            f"{cfg / 'c3_noise.yaml'}")),
        ("info", "runner", "step c3_noise exit code 0"),
        ("info", "c3_noise", f"cache write: {key}.parquet"),
        ("info", "runner", (f"spawning c4_noise: fake-spacescans run --data-dir {ctx.data_dir} "
                            f"{cfg / 'c4_noise.yaml'}")),
        ("info", "runner", "step c4_noise exit code 0"),
    ]


def test_a_failed_step_raises_with_the_steps_done(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(StepFailed) as info:
        run_source("noise", ["noise"], ctx, cache=None, executor=FakeExecutor(fail={"c4_noise"}))
    exc = info.value
    assert (exc.step, exc.rc, exc.kind, exc.experiment) == ("c4_noise", 3, "step_failed", "noise")
    assert exc.log_tail == ["Traceback", "boom"]
    assert [(s.name, s.status) for s in exc.steps] == [("c3_noise", "ran"), ("c4_noise", "failed")]
    assert "boom" not in str(exc) and exc.to_dict()["details"]["rc"] == 3


def test_a_cache_lookup_error_runs_fresh_and_skips_the_write_back(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    cache = WeightCache(tmp_path / "cache", key_format="legacy-v1")

    def broken(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr(cache, "fetch", broken)
    seen, emit = _events()
    res = run_source("noise", ["noise"], ctx, cache=cache, executor=FakeExecutor(), on_event=emit)
    warn = [e for e in seen if e.kind == "cache_warning"]
    assert [w.text for w in warn] == [
        "cache check failed for c3_noise: OSError('disk gone') — running fresh"]
    assert not any(e.kind == "cache_write" for e in seen)
    assert res.steps[0].status == "ran" and res.steps[0].cache_key is None


def test_a_render_failure_is_a_render_error_with_the_web_line(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)

    def bad(step, ctx, **kw):
        raise KeyError("source")
    monkeypatch.setattr("spacescans.orchestrate.render.write_config", bad)
    seen, emit = _events()
    with pytest.raises(RenderError) as info:
        run_source("noise", ["noise"], ctx, cache=None, executor=FakeExecutor(), on_event=emit)
    assert info.value.kind == "config" and info.value.step == "c3_noise"
    line = next(e for e in seen if e.kind == "warning")
    assert line.text == "render_yaml(c3_noise) failed: KeyError('source')"
    assert line.data["level"] == "error" and line.data["source"] == "runner"


def test_cancel_raises_run_cancelled(tmp_path):
    ctx = _ctx(tmp_path)
    cancel = threading.Event()
    with pytest.raises(RunCancelled) as info:
        run_source("noise", ["noise"], ctx, cache=None,
                   executor=FakeExecutor(cancel_on="c3_noise"), cancel=cancel)
    assert info.value.kind == "interrupted" and info.value.step == "c3_noise"
    pre = threading.Event()
    pre.set()
    exe = FakeExecutor()
    with pytest.raises(RunCancelled):
        run_source("noise", ["noise"], ctx, cache=None, executor=exe, cancel=pre)
    assert exe.calls == []


def test_partial_applies_faqsd_renames_and_can_rewrite_in_place(tmp_path):
    ctx = _ctx(tmp_path)
    run_source("faqsd", ["faqsd"], ctx, cache=None, executor=FakeExecutor())
    table, stats = partial("faqsd", ["faqsd"], ctx)
    assert list(table.columns) == ["pid", "episode_id", "faqsd_o3", "faqsd_pm25"]
    assert stats.matched_n == len(COHORT) and not stats.low
    c4 = ctx.run_dir / "output" / "c4_tract_faqsd.parquet"
    assert "o3" in pd.read_parquet(c4).columns
    partial("faqsd", ["faqsd"], ctx, rewrite_in_place=True)
    assert "faqsd_o3" in pd.read_parquet(c4).columns


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------

def _link(tmp_path, variables, **kw):
    kw.setdefault("executor", FakeExecutor())
    return link(COHORT, variables, data_dir=tmp_path / "data", run_dir=tmp_path / "run", **kw)


def test_link_groups_by_catalog_order_keeping_request_order_within_a_source(tmp_path,
                                                                             no_data_check):
    res = _link(tmp_path, ["noise", "walkability", "ndi"])
    assert list(res.partials) == ["bg_ndi_wi", "noise"]
    assert list(res.exposures.columns) == ["episode_id", "NatWalkInd", "ndi", "l50dba_exi",
                                           "l50dba_imp", "l50dba_nat"]
    assert list(res.exposures["episode_id"]) == list(range(len(COHORT)))
    assert list(res.coverage["variable"]) == ["walkability", "ndi", "noise"]
    assert (res.coverage["episodes"] == len(COHORT)).all()
    assert (res.coverage["patients"] == COHORT["pid"].nunique()).all()
    assert res.ok and res.failed == [] and res.skipped == []
    assert list(res.features["feature_name"]) == [
        r["feature_name"] for r in catalog.feature_rows(list(res.exposures.columns))]
    # the config names carry the web's task suffix (last 8 chars of the run dir name)
    cfg = yaml.safe_load((res.run_dir / "pipeline_configs" / "c3_bg.yaml").read_text())
    assert cfg["name"].endswith("_task_run")


def test_an_identical_c3_step_runs_once_per_link(tmp_path, no_data_check):
    exe = FakeExecutor()
    seen, emit = _events()
    res = _link(tmp_path, ["fara_tract", "faqsd"], executor=exe, on_event=emit)
    assert exe.calls.count("c3_tract_us") == 1
    statuses = [(s.experiment, s.name, s.status) for s in res.steps]
    assert ("faqsd", "c3_tract_us", "reused") in statuses
    reused = [e for e in seen if e.kind == "cache_hit" and e.data.get("status") == "reused"]
    assert [e.text for e in reused] == [LOG_REUSED.format(step="c3_tract_us")]


def test_a_custom_bg_dataset_reuses_bg_ndi_wi_weights(tmp_path, no_data_check):
    from spacescans import custom

    csv = tmp_path / "vals.csv"
    csv.write_text("GEOID10,myval\n180973201001,1.5\n170318391001,2.5\n")
    d = custom.define_polygon(csv, boundary="BG", key_col="GEOID10", value_cols=["myval"])
    key = d["variable_key"]
    exe = FakeExecutor()
    res = _link(tmp_path, [key, "walkability"], custom={key: d}, executor=exe)
    assert exe.calls.count("c3_bg") == 1
    assert list(res.partials) == ["bg_ndi_wi", "custom"]
    assert "myval" in res.exposures.columns
    assert res.provenance["custom"][key]["boundary"] == "BG"
    assert res.provenance["custom"][key]["sha256"] == hashlib.sha256(csv.read_bytes()).hexdigest()


def test_custom_definitions_are_checked_deeply(tmp_path, no_data_check):
    from spacescans import custom

    csv = tmp_path / "vals.csv"
    csv.write_text("GEOID10,myval\n180973201001,1.5\n")
    d = custom.define_polygon(csv, boundary="BG", key_col="GEOID10", value_cols=["myval"])
    key = d["variable_key"]
    csv.write_text("GEOID10,myval\n180973201001,9.5\n")     # edited after it was defined
    exe = FakeExecutor()
    with pytest.raises(custom.CustomExposomeError, match="changed since"):
        _link(tmp_path, [key], custom={key: d}, executor=exe)
    assert exe.calls == []


def test_on_error_stop_skips_the_rest_and_continue_runs_them(tmp_path, no_data_check):
    res = _link(tmp_path / "a", ["walkability", "noise", "vnl"],
                executor=FakeExecutor(fail={"c4_noise"}))
    assert res.failed == ["noise"] and res.skipped == ["vnl"]
    assert list(res.partials) == ["bg_ndi_wi"]
    assert list(res.exposures.columns) == ["episode_id", "NatWalkInd"]
    err = res.errors[0]
    assert (err["kind"], err["experiment"], err["step"], err["rc"]) == (
        "step_failed", "noise", "c4_noise", 3)
    assert err["variables"] == ["noise"] and err["log_tail"] == ["Traceback", "boom"]
    assert not res.ok

    res2 = _link(tmp_path / "b", ["walkability", "noise", "vnl"], on_error="continue",
                 executor=FakeExecutor(fail={"c4_noise"}))
    assert res2.failed == ["noise"] and res2.skipped == []
    assert list(res2.partials) == ["bg_ndi_wi", "vnl"]


def test_name_clashes_get_the_web_dup_suffix(tmp_path, no_data_check):
    res = _link(tmp_path, ["noise", "walkability"], reserved_names=["NatWalkInd", "age"])
    assert "NatWalkInd_bg_ndi_wi_dup" in res.exposures.columns
    assert "NatWalkInd" not in res.exposures.columns
    # a cohort column of the same name is taken too, as in the web fan-in
    df = COHORT.assign(l50dba_exi=1.0)
    res2 = link(df, ["noise"], data_dir=tmp_path / "data", run_dir=tmp_path / "run2",
                executor=FakeExecutor())
    assert "l50dba_exi_noise_dup" in res2.exposures.columns
    # every rename is reported, and the renamed column keeps its dictionary row (review F6)
    renamed = [w for w in res2.warnings if w["code"] == "renamed"]
    assert [(w["column"], w["renamed_to"], w["experiment"]) for w in renamed] == [
        ("l50dba_exi", "l50dba_exi_noise_dup", "noise")]
    assert "l50dba_exi_noise_dup" in renamed[0]["message"]
    feats = res2.features.set_index("feature_name")
    assert list(feats.index) == ["l50dba_exi_noise_dup", "l50dba_imp", "l50dba_nat"]
    want = catalog.feature_rows(["l50dba_exi"])[0]
    assert feats.loc["l50dba_exi_noise_dup", "short_description"] == want["short_description"]
    assert [w["renamed_to"] for w in res.warnings if w["code"] == "renamed"] == [
        "NatWalkInd_bg_ndi_wi_dup"]
    # only the five cohort columns reach the disk
    assert list(pd.read_parquet(res2.run_dir / "input.parquet").columns) == [
        "pid", "startDate", "endDate", "longitude", "latitude", "episode_id"]


def test_low_match_is_a_warning_and_a_web_log_record(tmp_path, no_data_check):
    log_dir = tmp_path / "run"
    log_dir.mkdir()
    res = _link(tmp_path, ["noise"], executor=FakeExecutor(missing_value_rows={0, 1}),
                on_event=LogWriter.for_run_dir(log_dir))
    low = [w for w in res.warnings if w["code"] == "low_match"]
    assert low and low[0]["pct"] == 50.0
    rec = [json.loads(line) for line in (log_dir / "logs.jsonl").read_text().splitlines()
           if '"event"' in line]
    assert rec and rec[0]["event"] == "merge_partial_low_match_pct"
    assert list(rec[0]) == ["ts", "level", "experiment_key", "event", "match_pct", "cohort_n",
                            "matched_n"]
    cov = res.coverage.set_index("variable").loc["noise"]
    assert (cov["with_value"], cov["pct"]) == (2, 50.0)


def test_data_missing_is_raised_before_any_step(tmp_path):
    from spacescans import data

    exe = FakeExecutor()
    (tmp_path / "empty").mkdir()
    with pytest.raises(data.DataMissingError) as info:
        link(COHORT, ["noise"], data_dir=tmp_path / "empty", run_dir=tmp_path / "run",
             executor=exe)
    assert exe.calls == [] and info.value.kind == "data_missing"
    assert "noise" in info.value.details["variables"] and info.value.hint
    assert not (tmp_path / "run").exists()


def test_bad_arguments_are_config_errors(tmp_path, no_data_check):
    for kw in ({"buffer_m": 270.5}, {"raster_res_m": 0}, {"on_error": "ignore"},
               {"buffer_m": True}, {"threads": 0}, {"memory_limit": 8}):
        with pytest.raises(LinkConfigError):
            _link(tmp_path, ["noise"], **kw)
    with pytest.raises(LinkConfigError):
        _link(tmp_path, "noise")
    with pytest.raises(LinkConfigError, match="no catalog knows"):
        _link(tmp_path, ["noise", "custom_deadbeef"])
    with pytest.raises(LinkConfigError, match="cohort file not found"):
        link(tmp_path / "nope.csv", ["noise"], data_dir=tmp_path / "data",
             run_dir=tmp_path / "run3", executor=FakeExecutor())
    assert LinkConfigError("x").to_dict()["kind"] == "config"


def test_run_dir_is_private_and_provenance_has_no_cohort_values(tmp_path, no_data_check):
    res = link(COHORT, ["noise"], data_dir=tmp_path / "data", run_dir=None,
               executor=FakeExecutor(), threads=2, memory_limit="1GB")
    try:
        assert stat.S_IMODE(res.run_dir.stat().st_mode) == 0o700
        text = json.dumps(res.provenance, default=str)
        for value in list(COHORT["pid"]) + ["86.15", "39.77", "2014-01-01"]:
            assert value not in text
        assert res.provenance["cohort"]["episodes"] == len(COHORT)
        assert res.provenance["run"]["threads"] == 2
        assert res.provenance["spacescans"]["code_fp"].startswith("sha256:")
        cfg = yaml.safe_load((res.run_dir / "pipeline_configs" / "c3_noise.yaml").read_text())
        assert cfg["engine"]["threads"] == 2 and cfg["engine"]["memory_limit"] == "1GB"
        for w in res.warnings:
            assert not any(v in json.dumps(w) for v in COHORT["pid"])
    finally:
        import shutil
        shutil.rmtree(res.run_dir, ignore_errors=True)


def test_events_feed_a_tee_and_the_bridge_sees_a_link_plan(tmp_path, no_data_check):
    seen, emit = _events()
    log_dir = tmp_path / "run"
    log_dir.mkdir()
    _link(tmp_path, ["fara_tract", "faqsd"], on_event=tee(emit, LogWriter.for_run_dir(log_dir)))
    plan = seen[1]
    assert plan.kind == "plan" and plan.experiment == "link"
    assert plan.data["total_steps"] == 3          # c3_tract_us once, two C4 steps
    assert seen[0].kind == "cohort" and seen[0].data == {"rows": 4, "patients": 3}
    done = [e for e in seen if e.kind == "done"]
    assert [(e.experiment, e.data["ok"]) for e in done] == [("fara_tract", True), ("faqsd", True)]
    for e in seen:
        assert not any(p in json.dumps(e.to_dict(), default=str) for p in COHORT["pid"])


def test_a_cache_directory_means_a_v2_cache(tmp_path, no_data_check):
    res = _link(tmp_path, ["noise"], cache=tmp_path / "c")
    key = next(s.cache_key for s in res.steps if s.name == "c3_noise")
    assert key and (tmp_path / "c" / "c3" / "v2" / f"{key}.parquet").is_file()
    assert res.provenance["cache"]["key_format"] == "v2"


def test_catalog_coverage_delegates_to_compute_coverage():
    from spacescans.orchestrate import compute_coverage

    got = catalog.coverage(COHORT, ["noise", "ndi"])
    want = compute_coverage(COHORT, ["noise", "ndi"], catalog.variables())
    assert got == want and got["row_count"] == len(COHORT)
    with pytest.raises(KeyError):
        catalog.coverage(COHORT, ["nope"])


def test_step_statuses_are_documented():
    assert run_mod.STEP_STATUSES == ("ran", "cache_hit", "reused", "failed")


def test_two_custom_datasets_with_one_column_name_keep_both(tmp_path, no_data_check):
    """Review F1: the second dataset's column was dropped and its coverage taken from the first."""
    from spacescans import custom

    a = tmp_path / "a.csv"
    a.write_text("GEOID10,pm25\n180973201001,1.5\n")
    b = tmp_path / "b.csv"
    b.write_text("GEOID10,pm25\n18097320100,99.0\n")
    da = custom.define_polygon(a, boundary="BG", key_col="GEOID10", value_cols=["pm25"])
    db = custom.define_polygon(b, boundary="Tract", key_col="GEOID10", value_cols=["pm25"])
    ka, kb = da["variable_key"], db["variable_key"]

    class Marked(FakeExecutor):            # dataset b: 1000 + ..., and no value in row 0
        def run(self, config, **kw):
            run = super().run(config, **kw)
            if Path(config).stem == f"c4_{kb}":
                out = Path(yaml.safe_load(Path(config).read_text())["output"]["path"])
                df = pd.read_parquet(out)
                df["pm25"] = [None] + list(df["pm25"][1:] + 1000)
                df.to_parquet(out, index=False)
            return run

    res = _link(tmp_path, [ka, kb], custom={ka: da, kb: db}, executor=Marked())
    dup = f"pm25_{kb}_dup"
    assert list(res.exposures.columns) == ["episode_id", "pm25", dup]
    assert list(res.exposures["pm25"]) == [0.0, 0.5, 1.0, 1.5]
    assert res.exposures[dup].isna().tolist() == [True, False, False, False]
    assert list(res.exposures[dup][1:]) == [1000.5, 1001.0, 1001.5]
    cov = res.coverage.set_index("variable")
    assert (cov.loc[ka, "with_value"], cov.loc[kb, "with_value"]) == (4, 3)
    assert cov.loc[kb, "pct"] == 75.0
    renamed = [w for w in res.warnings if w["code"] == "renamed"]
    assert [(w["variable"], w["column"], w["renamed_to"]) for w in renamed] == [(kb, "pm25", dup)]
    # dictionary rows come from each dataset's own definition (phase-C decision 3): the renamed
    # column keeps dataset b's label under its new name
    assert list(res.features["feature_name"]) == ["pm25", dup]
    assert list(res.features["short_description"]) == [da["label"], db["label"]] == ["a", "b"]
    assert list(res.features["detailed_description"]) == [da["description"], db["description"]]
    from spacescans.orchestrate.run import clash_renames
    assert clash_renames("custom", [ka, kb], {ka: da, kb: db}) == {kb: {"pm25": dup}}
    assert clash_renames("bg_ndi_wi", ["walkability", "ndi"]) == {}


def test_link_refuses_a_cohort_that_validate_refuses(tmp_path, no_data_check):
    """Review F4: link ran the steps on NaN / out-of-range coordinates and reversed dates."""
    from spacescans.cohort import CohortError

    bad = COHORT.copy()
    bad.loc[1, "longitude"] = float("nan")
    bad.loc[2, "latitude"] = 123.0
    bad.loc[3, "endDate"] = "2010-01-01"
    exe = FakeExecutor()
    with pytest.raises(CohortError) as info:
        link(bad, ["noise"], data_dir=tmp_path / "data", run_dir=tmp_path / "run", executor=exe)
    assert info.value.kind == "cohort" and exe.calls == []
    assert not (tmp_path / "run").exists()
    codes = {i.code for i in info.value.issues}
    assert {"coord_missing", "coord_range", "date_order"} <= codes
    for v in ("123", "2010-01-01"):
        assert v not in str(info.value)
    csv = tmp_path / "bad.csv"
    bad.to_csv(csv, index=False)
    with pytest.raises(CohortError):
        link(csv, ["noise"], data_dir=tmp_path / "data", run_dir=tmp_path / "run", executor=exe)
    assert exe.calls == [] and not (tmp_path / "run").exists()


def test_a_timed_out_step_is_not_reported_as_out_of_memory():
    """Review F3: the executor's own SIGKILL after a timeout read as the OOM killer."""
    from spacescans.bridge.errors import classify

    timed = StepFailed("c3_noise", -9, ["x"], timed_out=True)
    assert "memory" not in str(timed) and "timeout" in timed.hint and not timed.oom
    b = classify(timed)
    assert b.details["oom"] is False and "memory" not in b.hint
    oom = StepFailed("c3_noise", -9, ["x"])
    assert oom.oom and "memory" in str(oom) and "memory" in oom.hint
    assert classify(oom).details["oom"] is True


@pytest.mark.parametrize("variable,step,folder,prebuilt", [
    ("tiger_proximity", "c3_tiger_roads", "cache/C3/tiger_roads_filtered", "tiger_roads_cache"),
    ("nhd_bluespace", "c3_nhd_bluespace", "cache/C3/nhd_features", "nhd_features_cache")])
def test_tile_caches_stay_out_of_the_data_root_unless_prebuilt(tmp_path, monkeypatch, no_data_check,
                                                               variable, step, folder, prebuilt):
    """Review P1: TIGER/NHD C3 steps wrote tiles named by the cohort's counties into data_dir."""
    from spacescans.orchestrate.render import render

    res = _link(tmp_path / "raw", [variable])           # status not known: private folder
    cfg = yaml.safe_load((res.run_dir / "pipeline_configs" / f"{step}.yaml").read_text())
    assert cfg["source"]["road_cache_dir"] == str(res.run_dir / "feature_cache" / step)
    assert catalog.template(catalog.plan(variable, [variable])[0].template)["source"][
        "road_cache_dir"] == folder

    # data.status chose the prebuilt tile cache: the data root's folder is read in place
    monkeypatch.setattr(link_fn, "_check_data", lambda groups, overlay, data_dir: (
        {"data_dir": str(data_dir), "data_release": "test", "datasets": {},
         "variables": {variable: {"status": "ready", "datasets": [prebuilt]}}}, []))
    res2 = _link(tmp_path / "prebuilt", [variable])
    cfg2 = yaml.safe_load((res2.run_dir / "pipeline_configs" / f"{step}.yaml").read_text())
    assert cfg2["source"]["road_cache_dir"] == folder

    # the web's render (a plain RunContext) is unchanged
    ctx = _ctx(tmp_path / "web")
    assert render(catalog.plan(variable, [variable])[0], ctx)["source"]["road_cache_dir"] == folder
