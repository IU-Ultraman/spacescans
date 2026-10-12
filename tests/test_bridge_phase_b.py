"""spacescans.bridge phase B: the new commands, error kinds, events and interrupts.

Every command runs as a client runs it (``python -I -u -X utf8 -m
spacescans.bridge <cmd> --request ... --result ...``) and every stdout line is
parsed as JSON, so each test also checks that the event stream is JSON Lines.

``orchestrate.link`` and ``orchestrate.cache.WeightCache`` live on other
phase-B branches: the link and cache tests inject fakes with the contract 5.1
and 5.3 signatures through a runner script (the bridge runs with ``-I``, so
nothing can be injected through the environment's PYTHONPATH).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

import pandas as pd
import pytest

BRIDGE = [sys.executable, "-I", "-u", "-X", "utf8", "-m", "spacescans.bridge"]
NEW_COMMANDS = {"hello", "catalog", "coverage", "data-status", "data-plan", "data-verify",
                "data-fetch", "data-import", "data-terms", "custom-validate", "link",
                "cache-info", "cache-clear", "run", "schema", "quickstart", "resolve", "ping",
                "templates"}
COHORT_VALUES = ("P-0001", "P-0002", "-86.1234", "39.7654", "2016-03-04")


def call(tmp: Path, command: str, args: dict | None = None, *, prefix: list[str] | None = None,
         env: dict | None = None, timeout: float = 180, verbose: bool = False):
    """Run one bridge command; return (rc, result | None, events, stderr)."""
    req = tmp / f"{command}.request.json"
    res = tmp / f"{command}.result.json"
    if res.exists():
        res.unlink()
    req.write_text(json.dumps({"protocol": 1, "command": command, "verbose": verbose,
                               "client": {"name": "pytest", "version": "0", "protocol_range": [1, 1]},
                               "args": args or {}}))
    p = subprocess.run((prefix or BRIDGE) + [command, "--request", str(req), "--result", str(res)],
                       capture_output=True, text=True, cwd=str(tmp), timeout=timeout,
                       env=env if env is not None else os.environ.copy())
    events = [json.loads(line) for line in p.stdout.splitlines()]  # JSON Lines or fail
    result = json.loads(res.read_text()) if res.exists() else None
    return p.returncode, result, events, p.stderr


def write_cohort(path: Path, n: int = 4) -> Path:
    """A cohort as the R package writes it: five columns, pid text, dates as dates."""
    df = pd.DataFrame({
        "pid": [f"P-{i // 2 + 1:04d}" for i in range(n)],
        "startDate": [dt.date(2016, 3, 4) + dt.timedelta(days=100 * i) for i in range(n)],
        "endDate": [dt.date(2017, 3, 4) + dt.timedelta(days=100 * i) for i in range(n)],
        "longitude": [-86.1234 - 0.01 * i for i in range(n)],
        "latitude": [39.7654 + 0.01 * i for i in range(n)],
    })
    df.to_parquet(path, index=False)
    return path


# --- fakes for orchestrate.link / WeightCache -----------------------------------

FAKES = r'''
import json, os, signal, subprocess, sys, threading, time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

import spacescans.orchestrate as _orch

MODE = os.environ.get("SS_FAKE_MODE", "ok")
MARK = os.environ.get("SS_FAKE_MARK", "ss-fake")


@dataclass
class Event:
    kind: str
    experiment: str
    step: str | None = None
    index: int | None = None
    total: int | None = None
    frac: float | None = None
    text: str | None = None
    data: dict | None = None
    t: float = 0.0


@dataclass
class StepRecord:
    name: str
    status: str
    seconds: float
    rc: int | None
    maxrss_bytes: int | None
    cache_key: str | None


class StepFailed(Exception):
    kind = "step_failed"

    def __init__(self, step, rc, log_tail):
        super().__init__(f"step {step} exit code {rc}")
        self.step, self.rc, self.log_tail = step, rc, log_tail


StepFailed.__module__ = "spacescans.orchestrate.run"


def _spawn(run_dir):
    child = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep(120)  # {MARK}"],
                             start_new_session=True)
    (Path(run_dir) / "child.pid").write_text(str(child.pid))
    return child


def fake_link(cohort, variables, *, data_dir=None, run_dir=None, buffer_m=270, raster_res_m=25,
              custom=None, cache=None, executor="subprocess", threads=None, memory_limit=None,
              on_error="stop", reserved_names=(), on_event=None, cancel=None):
    run_dir = Path(run_dir)
    (run_dir / "fake_args.json").write_text(json.dumps({
        "columns": list(cohort.columns), "rows": len(cohort), "pid_type": str(cohort["pid"].dtype),
        "variables": list(variables), "data_dir": str(data_dir), "run_dir": str(run_dir),
        "buffer_m": buffer_m, "raster_res_m": raster_res_m,
        "custom": custom, "cache": None if cache is None else [type(cache).__name__, str(cache.root), cache.key_format],
        "executor": executor, "threads": threads, "memory_limit": memory_limit, "on_error": on_error,
        "reserved_names": list(reserved_names), "cancel_is_event": isinstance(cancel, threading.Event),
        "env_data_dir": os.environ.get("SPACESCANS_DATA_DIR"), "cwd": os.getcwd(),
    }))
    emit = on_event or (lambda e: None)
    if MODE == "hang":
        child = _spawn(run_dir)
        emit(Event("step_start", "bg_ndi_wi", "c3_bg", 0, 3))
        while not cancel.wait(0.05):
            pass
        os.killpg(child.pid, signal.SIGTERM)      # what SubprocessExecutor does on cancel
        child.wait(5)
        raise RuntimeError("cancelled")
    if MODE == "hang_ignore":
        _spawn(run_dir)
        while True:                                 # ignores cancel and leaves its child behind
            time.sleep(0.05)
    if MODE == "step_failed":
        emit(Event("step_start", "bg_ndi_wi", "c3_bg", 0, 3))
        raise StepFailed("c3_bg", -9, ["[overlap_fast] tile 1/2", "Killed"])
    if MODE == "data_missing":
        from spacescans.data import DataMissingError
        raise DataMissingError("data for ndi is not in the data directory",
                               hint="spacescans data fetch ndi", details={"variables": ["ndi"]})
    if MODE == "internal":
        raise ZeroDivisionError("division by zero")
    steps = [("bg_ndi_wi", "c3_bg"), ("bg_ndi_wi", "c3_bg_2020"), ("bg_ndi_wi", "c4_ndi"), ("bg_ndi_wi", "c4_wi")]
    emit(Event("plan", "bg_ndi_wi", data={"experiments": ["bg_ndi_wi"]}))
    emit(Event("cohort", "bg_ndi_wi", data={"rows": len(cohort)}))
    records = []
    for i, (exp, step) in enumerate(steps):
        emit(Event("step_start", exp, step, i, len(steps)))
        if step == "c3_bg_2020":
            emit(Event("cache_hit", exp, step, i, len(steps), text=f"cache hit: abc__BG2020__b{buffer_m}m — skipping pipeline run",
                       data={"key": f"abc__BG2020__b{buffer_m}m"}))
            records.append(StepRecord(step, "cache_hit", 0.01, None, None, "abc"))
            continue
        emit(Event("spawn", exp, step, i, len(steps), text=f"spawning {step}: python -m spacescans run {step}.yaml"))
        n = 400 if step == "c3_bg" else 3
        t0 = time.monotonic()
        for k in range(1, n + 1):
            line = f"[overlap_fast] tile {k}/{n} ({100 * k / n:5.1f}%)"
            emit(Event("line", exp, step, i, len(steps), text=line))
            emit(Event("progress", exp, step, i, len(steps), frac=k / n, text=line))
            if step == "c3_bg":
                time.sleep(0.003)
        emit(Event("step_end", exp, step, i, len(steps), text=f"step {step} exit code 0",
                   data={"rc": 0, "seconds": round(time.monotonic() - t0, 3), "maxrss_bytes": 123456,
                         "step": "shadowed"}))
        records.append(StepRecord(step, "ran", 0.5, 0, 123456, None))
    emit(Event("merge", "bg_ndi_wi", text="ndi: 100.0% of episodes linked (4/4)"))
    emit(Event("warning", "bg_ndi_wi", text="vnl matched 72.4% of episodes", data={"code": "low_match", "pct": 72.4}))
    emit(Event("done", "bg_ndi_wi"))
    print("a stray print from the orchestrator")
    n = len(cohort)
    exposures = pd.DataFrame({"episode_id": range(n), "ndi": [0.5] * n, "NatWalkInd": [10.0] * n})
    from spacescans import catalog
    features = pd.DataFrame(catalog.feature_rows(["ndi", "NatWalkInd"]))
    coverage = pd.DataFrame([{"variable": "ndi", "episodes": n, "with_value": n, "pct": 100.0},
                             {"variable": "walkability", "episodes": n, "with_value": n - 1, "pct": float("nan")}])
    class Result:
        pass
    r = Result()
    r.exposures, r.features, r.coverage = exposures, features, coverage
    r.steps, r.failed, r.skipped = records, [], []
    r.warnings = [{"code": "low_match", "experiment": "vnl", "pct": 72.4, "msg": "vnl matched 72.4% of episodes"}]
    r.provenance = {"versions": {"spacescans": "0.2.7"}, "cache_keys": {"c3_bg": "abc"}}
    r.run_dir = run_dir
    return r


class FakeWeightCache:
    def __init__(self, root, *, key_format):
        self.root, self.key_format = Path(root), key_format

    def info(self):
        return {"entries": 2, "bytes": 2048, "oldest": 1.0, "newest": 2.0, "root": str(self.root)}

    def clear(self, *, older_than_s=None):
        return {"removed": 1 if older_than_s else 2, "bytes_freed": 1024}


_orch.link = fake_link
import types
_cache_mod = types.ModuleType("spacescans.orchestrate.cache")
_cache_mod.WeightCache = FakeWeightCache
sys.modules["spacescans.orchestrate.cache"] = _cache_mod
'''


def fake_prefix(tmp: Path) -> list[str]:
    runner = tmp / "fake_bridge.py"
    runner.write_text(FAKES + textwrap.dedent("""
        import runpy
        sys.argv = ["spacescans.bridge"] + sys.argv[1:]
        runpy.run_module("spacescans.bridge", run_name="__main__", alter_sys=True)
    """))
    return [sys.executable, "-I", "-u", "-X", "utf8", str(runner)]


def fake_env(mode: str = "ok", **extra) -> dict:
    env = os.environ.copy()
    env["SS_FAKE_MODE"] = mode
    env.update(extra)
    return env


def link_args(tmp: Path, **over) -> dict:
    data_dir = tmp / "data"
    data_dir.mkdir(exist_ok=True)
    args = {"cohort": str(write_cohort(tmp / "cohort.parquet")), "variables": ["ndi", "walkability"],
            "data_dir": str(data_dir), "run_dir": str(tmp / "run"), "buffer_m": 270, "raster_res_m": 25,
            "cache_dir": str(tmp / "cache"), "executor": "subprocess", "threads": 2,
            "memory_limit": "4GB", "on_error": "stop", "reserved_names": "age"}
    args.update(over)
    return args


def no_cohort_values(*texts: str) -> None:
    for text in texts:
        for v in COHORT_VALUES:
            assert v not in text, f"cohort value {v!r} leaked"


# --- hello / templates ---------------------------------------------------------------

def test_hello_has_catalog_hash_data_release_and_protocol(tmp_path):
    rc, res, events, _ = call(tmp_path, "hello")
    assert rc == 0, res
    v = res["value"]
    from spacescans import catalog
    from spacescans._capabilities import catalog_fingerprint, fingerprint_entries
    from spacescans.data import load_manifest
    expected = fingerprint_entries((rel, catalog.template_bytes(rel)) for rel in catalog.templates())
    assert v["catalog"]["hash"] == expected == catalog_fingerprint()
    assert v["catalog"]["templates"] == 25 and v["catalog"]["variables"] == 11
    assert v["data_release"] == load_manifest().data_release
    assert v["protocol"] == 1 and v["protocol_range"] == [1, 1]
    assert NEW_COMMANDS <= set(v["commands"])
    assert [e["ev"] for e in events] == ["start", "done"]


def test_templates_lists_the_25_catalog_templates_with_hashes(tmp_path):
    from spacescans import catalog
    rc, res, _, _ = call(tmp_path, "templates")
    assert rc == 0
    v = res["value"]
    assert v["set"] == "catalog" and len(v["templates"]) == 25
    hashes = catalog.template_hashes()
    assert {t["file"]: t["sha256"] for t in v["templates"]} == hashes
    assert v["hash"].startswith("sha256:")
    rc, res, _, _ = call(tmp_path, "templates", {"name": "c3/bg_us_demo"})
    assert rc == 0 and res["value"]["text"] == catalog.template_text("c3/bg_us_demo.yaml")
    assert res["value"]["sha256"] == hashes["c3/bg_us_demo.yaml"]
    rc, res, _, _ = call(tmp_path, "templates", {"name": "c4/zbp"})          # an init template name
    assert rc == 1 and res["error"]["kind"] == "config" and "init" in res["error"]["hint"]
    rc, res, _, _ = call(tmp_path, "templates", {"kind": "init"})
    assert rc == 0 and len(res["value"]["templates"]) == 5
    rc, res, _, _ = call(tmp_path, "templates", {"kind": "nope"})
    assert rc == 1 and res["error"]["kind"] == "config"


# --- catalog / coverage -----------------------------------------------------------------

def test_catalog_lists_variables_requirements_and_features(tmp_path):
    rc, res, _, _ = call(tmp_path, "catalog")
    assert rc == 0, res
    v = res["value"]
    rows = {r["key"]: r for r in v["variables"]}
    assert len(rows) == 11 and "status" not in rows["ndi"]
    features = {f["feature_name"] for f in v["features"]}
    for r in rows.values():
        assert set(r["columns"]) <= features, r["key"]
        assert r["datasets"] and r["templates"] and r["patterns"]
    assert rows["faqsd"]["columns"] == ["faqsd_o3", "faqsd_pm25"]
    assert rows["ndi"]["steps"] == ["c3_bg", "c3_bg_2020", "c4_ndi"]
    assert rows["ndi"]["requires"] == [["bg_boundaries"], ["ndi_panel"]]
    assert v["data_release"] and v["catalog"]["templates"] == 25


def test_catalog_with_data_dir_adds_status(tmp_path):
    empty = tmp_path / "empty-data"
    rc, res, _, _ = call(tmp_path, "catalog", {"data_dir": str(empty), "variables": "ndi"})
    assert rc == 0, res
    (row,) = res["value"]["variables"]
    assert row["key"] == "ndi" and row["usable"] is False and row["status"] in ("missing", "action_required")
    assert res["value"]["status"]["ready"] is False
    rc, res, _, _ = call(tmp_path, "catalog", {"variables": ["ndi", "nope"]})
    assert rc == 1 and res["error"]["kind"] == "config" and res["error"]["details"]["unknown"] == ["nope"]


def test_coverage_counts_episodes_and_patients(tmp_path):
    cohort = write_cohort(tmp_path / "cohort.parquet")
    rc, res, events, err = call(tmp_path, "coverage", {"cohort": str(cohort), "variables": "ndi"})
    assert rc == 0, res
    v = res["value"]
    assert v["episodes"] == 4 and v["patients"] == 2
    (row,) = v["variables"]
    assert row["variable"] == "ndi" and row["episodes_covered"] == 4 and row["pct"] == 100.0
    no_cohort_values(json.dumps(res), err, json.dumps(events))


def test_coverage_bad_cohort_is_a_cohort_error_without_values(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("pid,startDate,endDate,longitude,latitude\n"
                   "P-0001,2016-03-04,2017-03-04,-86.1234,39.7654\n"
                   "P-0002,04.03.2016,2017-03-04,-86.1234,39.7654\n")
    rc, res, events, err = call(tmp_path, "coverage", {"cohort": str(bad), "variables": ["ndi"]})
    assert rc == 1
    e = res["error"]
    assert e["kind"] == "cohort" and e["type"] == "spacescans.cohort.CohortError"
    assert "Unrecognized date format" in e["message"] and e["details"]["issues"][0]["code"] == "date_format"
    no_cohort_values(json.dumps(res), err, json.dumps(events))
    coords = tmp_path / "coords.csv"
    coords.write_text("pid,startDate,endDate,longitude,latitude\n"
                      "P-0001,2016-03-04,2017-03-04,-86.1234,39.7654\n"
                      "P-0002,2016-03-04,2017-03-04,,95.5\n")
    rc, res, _, err = call(tmp_path, "coverage", {"cohort": str(coords), "variables": ["ndi"]})
    assert rc == 1 and res["error"]["kind"] == "cohort"
    assert {i["code"] for i in res["error"]["details"]["issues"]} == {"coord_missing", "coord_range"}
    assert res["error"]["details"]["issues"][0]["rows"] == [2]
    assert "95.5" not in json.dumps(res) + err
    rc, res, _, _ = call(tmp_path, "coverage", {"cohort": "cohort.parquet", "variables": ["ndi"]})
    assert rc == 1 and res["error"]["kind"] == "config" and "absolute" in res["error"]["message"]
    rc, res, _, _ = call(tmp_path, "coverage", {"cohort": str(coords), "variables": ["zz"]})
    assert rc == 1 and res["error"]["kind"] == "config"


# --- data-* --------------------------------------------------------------------------------

PANEL = bytes(range(256)) * 40


def tiny_manifest(tmp: Path, mirror: Path, *, panel: bytes = PANEL) -> Path:
    mirror.mkdir(exist_ok=True)
    (mirror / "panel.Rda").write_bytes(panel)
    m = {
        "schema_version": 1, "data_release": "2099.01",
        "mirrors": {"test": {"type": "file", "url": str(mirror)}},
        "lists": {"years": {"range": [2013, 2015]}},
        "artifacts": {"panel.Rda": {"kind": "bare", "status": "available", "mirror": "test",
                                    "sha256": hashlib.sha256(PANEL).hexdigest(), "bytes": len(PANEL),
                                    "dest": "NDI/C4"}},
        "datasets": {
            "ndi_panel": {"title": "test panel", "acquire": "mirror", "artifacts": ["panel.Rda"],
                          "provider": "project", "license": "derived", "redistribution": "allowed",
                          "attribution": "Cite us."},
            "vnl": {"title": "night lights", "acquire": "provider-manual", "provider": "EOG",
                    "license": "CC BY 4.0", "redistribution": "allowed with attribution",
                    "attribution": "Credit EOG.",
                    "terms": {"id": "eog-vnl", "url": "https://example.org/licence.pdf",
                              "summary": "account required", "account": True},
                    "instructions": ["Download it from EOG yourself."],
                    "layout": [{"rule": "one_per", "dir": "VNL/C3",
                                "regex": "^VNL_v21_npp_(?P<year>\\d{4})_global_.*\\.tif$",
                                "group": "year", "values": "@years", "warn_other": "*.tif"}]},
        },
        "variables": {"ndi": {"requires": ["ndi_panel"]}, "vnl": {"requires": ["vnl"]}},
        "boundaries": {},
    }
    path = tmp / "manifest.json"
    path.write_text(json.dumps(m))
    return path


def test_data_status_plan_fetch_verify_round_trip(tmp_path):
    man = tiny_manifest(tmp_path, tmp_path / "mirror")
    data_dir = tmp_path / "ss-data"
    base = {"data_dir": str(data_dir), "manifest": str(man)}
    env = os.environ.copy()
    env.pop("SPACESCANS_DATA_MIRROR", None)
    rc, res, _, _ = call(tmp_path, "data-status", {**base, "variables": "ndi"}, env=env)
    assert rc == 0 and res["value"]["ready"] is False
    assert res["value"]["variables"][0]["status"] == "missing"
    assert res["value"]["next_steps"][0]["action"] == "fetch"
    rc, res, _, _ = call(tmp_path, "data-plan", {**base, "variables": ["ndi"]}, env=env)
    assert rc == 0 and [i["artifact"] for i in res["value"]["items"]] == ["panel.Rda"]
    rc, res, events, _ = call(tmp_path, "data-fetch", {**base, "variables": ["ndi"]}, env=env)
    assert rc == 0, res
    assert res["value"]["ok"] is True and res["value"]["status"]["ready"] is True
    kinds = [e["ev"] for e in events]
    assert "artifact_start" in kinds and "artifact_end" in kinds and kinds[-1] == "done"
    assert (data_dir / "NDI" / "C4" / "panel.Rda").read_bytes() == PANEL
    rc, res, _, _ = call(tmp_path, "data-verify", {**base, "variables": ["ndi"]}, env=env)
    assert rc == 0 and res["value"]["ok"] is True
    (data_dir / "NDI" / "C4" / "panel.Rda").write_bytes(b"x" * len(PANEL))   # same size, wrong bytes
    rc, res, _, _ = call(tmp_path, "data-verify", {**base, "variables": ["ndi"]}, env=env)
    assert rc == 0 and res["value"]["ok"] is False and res["value"]["corrupt"] == ["ndi_panel"]


def test_data_errors_keep_their_kinds(tmp_path):
    man = tiny_manifest(tmp_path, tmp_path / "mirror", panel=b"y" * len(PANEL))   # mirror copy is corrupt
    base = {"data_dir": str(tmp_path / "ss-data"), "manifest": str(man)}
    rc, res, _, _ = call(tmp_path, "data-fetch", {**base, "variables": ["ndi"]})
    assert rc == 1 and res["error"]["kind"] == "data_corrupt", res["error"]
    assert "report" in res["error"]["details"]
    rc, res, _, _ = call(tmp_path, "data-status", {**base, "variables": ["nope"]})
    assert rc == 1 and res["error"]["kind"] == "config"
    assert res["error"]["details"]["unknown_variables"] == ["nope"]
    env = os.environ.copy()
    env.pop("SPACESCANS_DATA_DIR", None)
    rc, res, _, _ = call(tmp_path, "data-status", {"variables": ["ndi"]}, env=env)
    assert rc == 1 and res["error"]["kind"] == "config" and "data directory" in res["error"]["message"]
    rc, res, _, _ = call(tmp_path, "data-fetch", {**base, "accept_terms": "nope"})
    assert rc == 1 and res["error"]["kind"] == "config" and res["error"]["details"]["unknown"] == ["nope"]


def test_data_import_and_terms(tmp_path):
    man = tiny_manifest(tmp_path, tmp_path / "mirror")
    good = tmp_path / "VNL_v21_npp_2014_global_vcmslcfg_c202205302300.average_masked.dat.tif"
    good.write_bytes(b"tif")
    bad = tmp_path / "notes.txt"
    bad.write_text("x")
    data_dir = tmp_path / "ss-data"
    rc, res, _, _ = call(tmp_path, "data-import", {"dataset": "vnl", "paths": [str(good), str(bad)],
                                                   "data_dir": str(data_dir), "manifest": str(man)})
    assert rc == 0, res
    results = {Path(i["source"]).name: i["result"] for i in res["value"]["items"]}
    assert results == {good.name: "placed", "notes.txt": "rejected"}
    assert (data_dir / "VNL" / "C3" / good.name).is_file()
    assert any(w["code"] == "import_rejected" for w in res["warnings"])
    rc, res, _, _ = call(tmp_path, "data-import", {"dataset": "vnl", "paths": ["rel.tif"]})
    assert rc == 1 and res["error"]["kind"] == "config"
    rc, res, _, _ = call(tmp_path, "data-terms", {"dataset": "vnl"})                 # packaged manifest
    assert rc == 0 and res["value"]["dataset"] == "vnl" and res["value"]["terms"]["id"]
    rc, res, _, _ = call(tmp_path, "data-terms", {"dataset": "nope"})
    assert rc == 1 and res["error"]["kind"] == "config"


# --- custom-validate ---------------------------------------------------------------------------

def test_custom_validate_polygon_and_check(tmp_path):
    csv = tmp_path / "green.csv"
    csv.write_text("county_fips,greenness\n01001,0.3\n01003,0.4\n")
    rc, res, _, _ = call(tmp_path, "custom-validate", {
        "kind": "polygon", "files": str(csv),
        "options": {"boundary": "County", "key_col": "county_fips", "value_cols": "greenness",
                    "name": "Greenness", "units": {"greenness": "index"}}})
    assert rc == 0, res
    d = res["value"]["definition"]
    assert res["value"]["key"] == d["variable_key"] and d["value_cols"] == ["greenness"]
    assert d["values_path"] == str(csv) and d["label"] == "Greenness"
    damaged = {**d, "value_cols": "greenness", "value_labels": [], "year_col": {}}   # jsonlite round trip
    rc, res, _, _ = call(tmp_path, "custom-validate", {"kind": "check", "definition": damaged,
                                                       "key": d["variable_key"], "deep": True})
    assert rc == 0 and res["value"]["definition"]["value_cols"] == ["greenness"]
    assert res["value"]["definition"]["year_col"] is None
    csv.write_text("county_fips,greenness\n01001,0.9\n01003,0.4\n")                # edited since
    rc, res, _, _ = call(tmp_path, "custom-validate", {"kind": "check", "definition": d, "deep": True})
    assert rc == 1 and res["error"]["kind"] == "config" and "changed" in res["error"]["message"]
    assert res["error"]["type"] == "spacescans.custom.errors.CustomExposomeError"
    rc, res, _, _ = call(tmp_path, "custom-validate", {
        "kind": "polygon", "files": [str(csv)],
        "options": {"boundary": "Planet", "key_col": "county_fips", "value_cols": ["greenness"]}})
    assert rc == 1 and res["error"]["kind"] == "config"


@pytest.mark.geo
def test_custom_validate_raster(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    tif = tmp_path / "heat.tif"
    with rasterio.open(tif, "w", driver="GTiff", height=4, width=4, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_origin(-87, 40, 0.1, 0.1)) as ds:
        ds.write(np.ones((1, 4, 4), dtype="float32"))
    rc, res, _, _ = call(tmp_path, "custom-validate", {"kind": "raster", "files": [str(tif)],
                                                       "options": {"name": "heat"}})
    assert rc == 0, res
    d = res["value"]["definition"]
    assert d["geometry"] == "raster" and d["value_cols"] == ["value"] and len(d["grid_hash"]) == 8


# --- link (fake orchestrate.link) ------------------------------------------------------------------

def test_link_writes_results_and_streams_events(tmp_path, monkeypatch):
    monkeypatch.setenv("SPACESCANS_DATA_DIR", str(tmp_path / "somewhere-else"))
    args = link_args(tmp_path)
    rc, res, events, err = call(tmp_path, "link", args, prefix=fake_prefix(tmp_path),
                                env=fake_env("ok"), verbose=False)
    assert rc == 0, res["error"]
    v = res["value"]
    run = tmp_path / "run"
    assert v["exposures"] == str(run / "exposures.parquet") and v["run_dir"] == str(run)
    assert v["rows"] == v["episodes"] == 4 and v["patients"] == 2
    ex = pd.read_parquet(v["exposures"])
    assert list(ex["episode_id"]) == [0, 1, 2, 3] and v["columns"] == ["episode_id", "ndi", "NatWalkInd"]
    feats = pd.read_csv(v["features"])
    assert list(feats.columns) == ["feature_name", "short_description", "detailed_description"]
    assert list(feats["feature_name"]) == ["ndi", "NatWalkInd"]
    assert v["coverage"][1]["pct"] is None                     # NaN -> null
    assert [s["status"] for s in v["steps"]] == ["ran", "cache_hit", "ran", "ran"]
    assert v["failed"] == [] and v["skipped"] == []
    prov = json.loads(Path(v["provenance"]).read_text())
    assert prov["bridge"]["client"]["name"] == "pytest" and prov["cache_keys"] == {"c3_bg": "abc"}
    # the arguments link received
    got = json.loads((run / "fake_args.json").read_text())
    assert got["columns"] == ["pid", "startDate", "endDate", "longitude", "latitude"]
    assert got["variables"] == ["ndi", "walkability"] and got["reserved_names"] == ["age"]
    assert got["cache"] == ["FakeWeightCache", str(tmp_path / "cache"), "v2"]
    assert (got["executor"], got["threads"], got["memory_limit"], got["on_error"]) == ("subprocess", 2, "4GB", "stop")
    assert got["cancel_is_event"] is True and got["buffer_m"] == 270
    assert got["env_data_dir"] == got["data_dir"] == args["data_dir"]          # mismatch #11
    # warnings: link's own (msg -> message) reach the client
    assert any(w["code"] == "low_match" and w["message"] for w in res["warnings"])
    # events
    kinds = [e["ev"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert {"plan", "step_start", "cache_hit", "spawn", "progress", "step_end", "merge", "warning"} <= set(kinds)
    assert "line" not in kinds                                       # only with verbose
    prog = [e for e in events if e["ev"] == "progress" and e["step"] == "c3_bg"]
    span = prog[-1]["t"] - prog[0]["t"]
    assert prog[-1]["frac"] == 1.0 and len(prog) <= 4 * span + 3, (len(prog), span)
    overall = [e["overall"] for e in events if e.get("overall") is not None]
    assert overall == sorted(overall) and 0 <= overall[0] and overall[-1] == 1.0
    end = next(e for e in events if e["ev"] == "step_end")
    assert end["rc"] == 0 and end["maxrss_bytes"] == 123456 and end["step"] == "c3_bg"
    assert end["data_step"] == "shadowed"
    # logs.jsonl in the web's format
    lines = [json.loads(x) for x in (run / "logs.jsonl").read_text().splitlines()]
    assert all(set(x) == {"ts", "level", "source", "msg"} for x in lines)
    msgs = [(x["source"], x["msg"]) for x in lines]
    assert ("runner", "spawning c3_bg: python -m spacescans run c3_bg.yaml") in msgs
    assert ("runner", "step c3_bg exit code 0") in msgs
    assert ("c3_bg", "[overlap_fast] tile 400/400 (100.0%)") in msgs
    assert ("merge", "ndi: 100.0% of episodes linked (4/4)") in msgs
    assert any(x["level"] == "warning" for x in lines)
    assert "a stray print from the orchestrator" in err
    no_cohort_values(json.dumps(res), err, json.dumps(events), (run / "logs.jsonl").read_text(),
                     Path(v["provenance"]).read_text())


def test_link_verbose_streams_lines(tmp_path):
    rc, res, events, _ = call(tmp_path, "link", link_args(tmp_path), prefix=fake_prefix(tmp_path),
                              env=fake_env("ok"), verbose=True)
    assert rc == 0, res
    assert sum(e["ev"] == "line" for e in events) >= 400


@pytest.mark.parametrize("mode,kind", [("step_failed", "step_failed"), ("data_missing", "data_missing"),
                                       ("internal", "internal")])
def test_link_error_kinds(tmp_path, mode, kind):
    rc, res, _, _ = call(tmp_path, "link", link_args(tmp_path), prefix=fake_prefix(tmp_path),
                         env=fake_env(mode))
    assert rc == 1
    e = res["error"]
    assert e["kind"] == kind, e
    if kind == "step_failed":
        assert e["details"]["step"] == "c3_bg" and e["details"]["rc"] == -9 and e["details"]["oom"] is True
        assert e["details"]["log_tail"][-1] == "Killed" and "memory" in e["hint"]
        assert e["type"] == "spacescans.orchestrate.run.StepFailed"
    elif kind == "data_missing":
        assert e["hint"] == "spacescans data fetch ndi" and e["details"] == {"variables": ["ndi"]}
    else:
        assert "ZeroDivisionError" in e["message"] and "Traceback" in e["traceback"]


def test_link_argument_errors(tmp_path):
    prefix = fake_prefix(tmp_path)
    rc, res, _, _ = call(tmp_path, "link", link_args(tmp_path, variables=[]), prefix=prefix, env=fake_env())
    assert rc == 1 and res["error"]["kind"] == "config"
    rc, res, _, _ = call(tmp_path, "link", link_args(tmp_path, buffer_m=270.5), prefix=prefix, env=fake_env())
    assert rc == 1 and res["error"]["kind"] == "config" and "buffer_m" in res["error"]["message"]
    rc, res, _, _ = call(tmp_path, "link", link_args(tmp_path, executor="threads"), prefix=prefix, env=fake_env())
    assert rc == 1 and res["error"]["kind"] == "config"
    env = fake_env()
    env.pop("SPACESCANS_DATA_DIR", None)
    rc, res, _, _ = call(tmp_path, "link", link_args(tmp_path, data_dir=None), prefix=prefix, env=env)
    assert rc == 1 and res["error"]["kind"] == "config" and "data directory" in res["error"]["message"]
    bad = tmp_path / "bad.parquet"
    pd.DataFrame({"pid": ["P-0001"], "startDate": [dt.date(2016, 3, 4)], "endDate": [dt.date(2015, 1, 1)],
                  "longitude": [-86.1234], "latitude": [39.7654]}).to_parquet(bad, index=False)
    rc, res, _, err = call(tmp_path, "link", link_args(tmp_path, cohort=str(bad)), prefix=prefix, env=fake_env())
    assert rc == 1 and res["error"]["kind"] == "cohort"
    assert res["error"]["details"]["issues"][0]["code"] == "date_order"
    no_cohort_values(json.dumps(res), err)


def test_link_without_orchestrate_link_is_internal(tmp_path):
    import spacescans.orchestrate as orch
    if callable(getattr(orch, "link", None)) or hasattr(getattr(orch, "link", None), "link"):
        pytest.skip("this build has orchestrate.link")
    rc, res, _, _ = call(tmp_path, "link", link_args(tmp_path, cache_dir=None))
    assert rc == 1 and res["error"]["kind"] == "internal" and "orchestrate.link" in res["error"]["message"]


# --- interrupts: exit 130/143, kind interrupted, no leftover processes ----------------------------

def _alive(pid: int) -> bool:
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _marked(mark: str) -> list[str]:
    out = subprocess.run(["ps", "-A", "-o", "pid=,command="], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if mark in line and "ps -A" not in line]


@pytest.mark.parametrize("mode,sig,code", [("hang", signal.SIGINT, 130), ("hang", signal.SIGTERM, 143),
                                           ("hang_ignore", signal.SIGINT, 130)])
def test_link_interrupt_kills_steps_and_leaves_no_process(tmp_path, mode, sig, code):
    mark = f"ss-fake-{uuid.uuid4().hex}"
    args = link_args(tmp_path, cancel_grace_s=1.0 if mode == "hang_ignore" else 7.0)
    req = tmp_path / "req.json"
    res = tmp_path / "res.json"
    req.write_text(json.dumps({"protocol": 1, "command": "link", "args": args}))
    p = subprocess.Popen(fake_prefix(tmp_path) + ["link", "--request", str(req), "--result", str(res)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(tmp_path),
                         env=fake_env(mode, SS_FAKE_MARK=mark))
    pid_file = tmp_path / "run" / "child.pid"
    deadline = time.monotonic() + 60
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    child = int(pid_file.read_text())
    assert _alive(child) and _marked(mark)
    t0 = time.monotonic()
    p.send_signal(sig)
    out, err = p.communicate(timeout=20)
    elapsed = time.monotonic() - t0
    assert p.returncode == code, err
    assert elapsed < 10, elapsed
    [json.loads(line) for line in out.splitlines()]               # still JSON Lines
    result = json.loads(res.read_text())
    assert result["ok"] is False and result["error"]["kind"] == "interrupted"
    if mode == "hang_ignore":     # the grace period is waited out, then the sweep kills the child
        assert elapsed >= 0.9 and "still running" in err and "killed 1 leftover" in err, err
    else:                         # the orchestrator stopped its own step; nothing left to sweep
        assert "worker stopped" in err and "killed 0 leftover" in err, err
    deadline = time.monotonic() + 3
    while (_alive(child) or _marked(mark)) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(child) and not _marked(mark), _marked(mark)


# --- cache-info / cache-clear (fake WeightCache) -------------------------------------------------

def test_cache_info_and_clear(tmp_path):
    prefix = fake_prefix(tmp_path)
    root = str(tmp_path / "cache")
    rc, res, _, _ = call(tmp_path, "cache-info", {"cache_dir": root}, prefix=prefix, env=fake_env())
    assert rc == 0, res
    assert res["value"] == {"cache_dir": root, "key_format": "v2", "entries": 2, "bytes": 2048,
                            "oldest": 1.0, "newest": 2.0, "root": root}
    rc, res, _, _ = call(tmp_path, "cache-clear", {"cache_dir": root, "older_than_s": 86400},
                         prefix=prefix, env=fake_env())
    assert rc == 0 and res["value"]["removed"] == 1 and res["value"]["older_than_s"] == 86400
    rc, res, _, _ = call(tmp_path, "cache-clear", {"cache_dir": root}, prefix=prefix, env=fake_env())
    assert rc == 0 and res["value"]["removed"] == 2
    rc, res, _, _ = call(tmp_path, "cache-info", {"cache_dir": "rel"}, prefix=prefix, env=fake_env())
    assert rc == 1 and res["error"]["kind"] == "config"
    rc, res, _, _ = call(tmp_path, "cache-clear", {"cache_dir": root, "older_than_s": -1},
                         prefix=prefix, env=fake_env())
    assert rc == 1 and res["error"]["kind"] == "config"


# --- SPACESCANS_DATA_DIR (#11) ----------------------------------------------------------------------

def test_resolve_uses_the_request_data_dir_for_env_placeholders(tmp_path, monkeypatch):
    env_dir, req_dir = tmp_path / "A", tmp_path / "B"
    monkeypatch.setenv("SPACESCANS_DATA_DIR", str(env_dir))
    cfg = {"name": "x", "linkage_pattern": "yearly_areal", "geometry_type": "polygon",
           "source": {"file": "${SPACESCANS_DATA_DIR}/w.parquet", "join_col": "G"},
           "buffer": {"patient_file": "p.parquet"},
           "exposure": {"file": "e.parquet", "join_col": "G", "value_cols": ["v"]},
           "output": {"path": str(tmp_path / "o.parquet")}}
    rc, res, _, _ = call(tmp_path, "resolve", {"config": cfg, "data_dir": str(req_dir)})
    assert rc == 0, res
    out = res["value"]["config"]
    assert out["source"]["file"] == str(req_dir / "w.parquet")
    assert out["buffer"]["patient_file"] == str(req_dir / "p.parquet")


# --- in-process units ----------------------------------------------------------------------------------

def test_classify_honours_kinds():
    from spacescans import data
    from spacescans.bridge.errors import classify
    from spacescans.cohort import CohortError, Issue
    from spacescans.custom import CustomExposomeError, RasterError

    e = classify(CohortError("Missing pid in 1 row (row 3)",
                             [Issue("pid_missing", "Missing pid in 1 row (row 3)", "pid", (3,), 1)],
                             code="invalid"))
    assert e.kind == "cohort" and e.details["issues"][0]["rows"] == [3] and e.hint
    assert classify(CustomExposomeError("bad upload")).kind == "config"
    assert classify(RasterError("bad grid")).to_dict()["type"] == "spacescans.custom.errors.RasterError"
    for exc, kind in [(data.DataMissingError("m"), "data_missing"), (data.DataCorruptError("c"), "data_corrupt"),
                      (data.NetworkError("n"), "network"), (data.DiskSpaceError("d"), "disk_space"),
                      (data.LockedError("l"), "locked"), (data.TermsRequiredError("t"), "terms_required"),
                      (data.UsageError("u"), "config"), (data.ManifestError("x"), "internal")]:
        assert classify(exc).kind == kind

    class Foreign(Exception):
        kind = "weird"              # not a protocol kind, not from spacescans: ignored
    assert classify(Foreign("x")).kind == "internal"

    class ForeignStep(Exception):
        kind = "step_failed"        # a protocol kind: honoured wherever it comes from
        step, rc, log_tail = "c4_x", 137, "a\nb"
    s = classify(ForeignStep())
    assert s.kind == "step_failed" and s.details["oom"] is True and s.details["log_tail"] == ["a", "b"]
    assert "c4_x" in s.message


def test_cohort_error_never_quotes_the_date_sample():
    from spacescans.bridge.errors import classify
    from spacescans.cohort import CohortError, detect_date_format
    sample = "31" + ".12." + "2016"                         # built at run time: frames show source lines
    with pytest.raises(CohortError) as info:
        detect_date_format([pd.Series([sample, "2016-03-04"])])
    assert sample in str(info.value)                       # the web's message is unchanged
    d = classify(info.value).to_dict()
    assert d["kind"] == "cohort"
    assert sample not in json.dumps(d), d                  # message, details and traceback
    assert "Unrecognized date format" in d["traceback"]


def test_relay_overall_fraction_is_monotonic():
    from spacescans.bridge.cmd_link import LinkRelay
    from spacescans.bridge.events import EventStream

    class Sink:
        def __init__(self):
            self.lines = []

        def write(self, s):
            self.lines.append(s)

        def flush(self):
            pass

    sink = Sink()
    relay = LinkRelay(EventStream(sink, progress_interval_s=0), None, total_steps=2, verbose=False)
    E = lambda **k: {"experiment": "x", **k}  # dict events work too
    relay(E(kind="step_start", step="a"))
    relay(E(kind="progress", step="a", frac=0.5))
    relay(E(kind="progress", step="a", frac=0.25))          # never goes backwards
    relay(E(kind="step_end", step="a", data={"rc": 0}))
    relay(E(kind="cache_hit", step="b", data={"key": "k"}))
    relay(E(kind="start", step=None))                       # reserved name gets a prefix
    evs = [json.loads(x) for x in sink.lines]
    assert [e.get("overall") for e in evs if e["ev"] != "orchestrate_start"] == [0.0, 0.25, 0.25, 0.5, 1.0]
    assert evs[-1]["ev"] == "orchestrate_start"


def test_planned_steps_counts_shared_steps_once():
    from spacescans.bridge.cmd_link import planned_steps
    assert planned_steps(["ndi", "walkability"], None) == 4     # c3_bg shared
    assert planned_steps(["faqsd", "fara_tract"], None) == 3    # c3_tract_us shared
    assert planned_steps(["nope"], None) is None


def test_apply_data_dir_sets_the_env(monkeypatch):
    from spacescans.bridge.main import apply_data_dir
    monkeypatch.setenv("SPACESCANS_DATA_DIR", "/env/a")
    assert apply_data_dir({"data_dir": "rel"}) is None and os.environ["SPACESCANS_DATA_DIR"] == "/env/a"
    assert apply_data_dir({"data_dir": "/req/b"}) == "/req/b" and os.environ["SPACESCANS_DATA_DIR"] == "/req/b"
    assert apply_data_dir({}) is None and os.environ["SPACESCANS_DATA_DIR"] == "/req/b"


def test_list_arguments_accept_r_scalars():
    from spacescans.bridge.args import mapping_arg, path_list_arg, str_list_arg
    from spacescans.bridge.errors import BridgeError
    assert str_list_arg({"v": "ndi"}, "v") == ["ndi"]
    assert path_list_arg({"p": "/a"}, "p") == ["/a"]
    assert mapping_arg({"c": []}, "c") == {}
    with pytest.raises(BridgeError):
        str_list_arg({"v": [1]}, "v")
    with pytest.raises(BridgeError):
        path_list_arg({"p": ["/a", "b"]}, "p")


def test_kill_descendants_kills_a_child_session():
    from spacescans.bridge.worker import descendants, kill_descendants
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        assert child.pid in [pid for pid, _ in descendants()]
        assert child.pid in kill_descendants()
        assert child.wait(5) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()


def test_run_cancellable_sets_cancel_on_interrupt():
    from spacescans.bridge.worker import run_cancellable
    cancel = threading.Event()
    seen = []

    def work():
        cancel.wait(30)
        seen.append(cancel.is_set())

    timer = threading.Timer(0.3, lambda: signal.raise_signal(signal.SIGINT))
    timer.start()
    previous = signal.getsignal(signal.SIGTERM)
    t0 = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            run_cancellable(work, lambda line: None, cancel=cancel, grace_s=2)
    finally:
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, previous)
    assert time.monotonic() - t0 < 3 and seen == [True]


# --- base install (geo stack hidden) -------------------------------------------------------------------

BLOCK_GEO = """
import sys
from importlib.abc import MetaPathFinder
_BLOCKED = {"geopandas", "rasterio", "shapely", "exactextract", "pyogrio",
            "pyreadr", "pyhdf", "xarray", "netCDF4"}
class _Blocker(MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in _BLOCKED:
            raise ModuleNotFoundError(f"No module named {name!r} (blocked for base-install test)",
                                      name=name.split(".")[0])
        return None
sys.meta_path.insert(0, _Blocker())
import importlib.util
_real_find_spec = importlib.util.find_spec
importlib.util.find_spec = lambda n, *a, **k: None if n.split(".")[0] in _BLOCKED else _real_find_spec(n, *a, **k)
"""


def test_catalog_and_hello_on_a_base_install(tmp_path):
    runner = tmp_path / "base_only_bridge.py"
    runner.write_text(BLOCK_GEO + textwrap.dedent("""
        import runpy, sys
        sys.argv = ["spacescans.bridge"] + sys.argv[1:]
        runpy.run_module("spacescans.bridge", run_name="__main__", alter_sys=True)
    """))
    prefix = [sys.executable, "-I", "-u", "-X", "utf8", str(runner)]
    rc, res, _, _ = call(tmp_path, "catalog", prefix=prefix)
    assert rc == 0, res
    rows = {r["key"]: r for r in res["value"]["variables"]}
    assert rows["ndi"]["missing_extras"] == ["geo"] and rows["ndi"]["runnable_here"] is False
    rc, res, _, _ = call(tmp_path, "hello", prefix=prefix)
    assert rc == 0 and res["value"]["catalog"]["templates"] == 25 and res["value"]["data_release"]
