"""spacescans.bridge, protocol 1: commands, envelope, events, errors, exit codes, signals.

Most tests run the bridge exactly as a client does:
``python -I -u -X utf8 -m spacescans.bridge <command> --request ... --result ...``.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pandas as pd
import pytest
import yaml

# Same helpers as tests/test_upstream_bridge_support.py (importlib mode: no shared module).
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


def make_c4_fixture(tmp: Path, *, years=(2017, 2018)) -> dict:
    """A tiny yearly_areal C4 config that runs on a base install."""
    patients = pd.DataFrame({
        "pid": ["P1", "P1", "P2"],
        "startDate": pd.to_datetime(["2017-01-01", "2018-01-01", "2017-06-01"]),
        "endDate": pd.to_datetime(["2017-12-31", "2018-12-31", "2018-05-31"]),
        "longitude": [-86.1, -86.2, -86.3],
        "latitude": [39.7, 39.8, 39.9],
        "episode_id": [0, 1, 2],
    })
    weights = pd.DataFrame({"geoid": [0, 1, 2, 2], "GEOID10": ["A", "B", "A", "B"],
                            "weight": [1.0, 1.0, 0.5, 0.5]})
    exposure = pd.DataFrame({"GEOID10": ["A", "A", "B", "B"], "year": [2017, 2018, 2017, 2018],
                             "v": [10.0, 20.0, 30.0, 40.0]})
    patients.to_parquet(tmp / "patients.parquet", index=False)
    weights.to_parquet(tmp / "weights.parquet", index=False)
    exposure.to_parquet(tmp / "exposure.parquet", index=False)
    return {
        "name": "c4_fixture",
        "linkage_pattern": "yearly_areal",
        "geometry_type": "polygon",
        "source": {"file": "weights.parquet", "join_col": "GEOID10"},
        "buffer": {"patient_file": "patients.parquet", "patient_adapter": "demo_conus"},
        "exposure": {"file": "exposure.parquet", "join_col": "GEOID10",
                     "value_cols": ["v"], "year_col": "year"},
        "time": {"years": list(years), "output_grouping": "episode"},
        "output": {"path": "c4_fixture.parquet"},
    }

BRIDGE = [sys.executable, "-I", "-u", "-X", "utf8", "-m", "spacescans.bridge"]


def call(tmp: Path, command: str, args: dict | None = None, *, request: dict | None | str = "default",
         extra: tuple[str, ...] = (), prefix: list[str] | None = None, timeout: float = 180):
    """Run one bridge command; return (rc, result | None, events, stderr)."""
    req = tmp / f"{command}.request.json"
    res = tmp / f"{command}.result.json"
    if res.exists():
        res.unlink()
    if request == "default":
        request = {"protocol": 1, "command": command,
                   "client": {"name": "pytest", "version": "0", "protocol_range": [1, 1]},
                   "args": args or {}}
    argv = [command, "--result", str(res), *extra]
    if request is not None:
        req.write_text(request if isinstance(request, str) else json.dumps(request))
        argv += ["--request", str(req)]
    cmd = (prefix or BRIDGE) + argv
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tmp), timeout=timeout)
    events = [json.loads(line) for line in p.stdout.splitlines()]  # stdout must be JSON Lines
    result = json.loads(res.read_text()) if res.exists() else None
    return p.returncode, result, events, p.stderr


def base_only_prefix(tmp: Path) -> list[str]:
    """Run the bridge with the geo/rda/hdf4/nc stacks hidden (a base install)."""
    runner = tmp / "base_only_bridge.py"
    runner.write_text(BLOCK_GEO + textwrap.dedent("""
        import runpy, sys
        sys.argv = ["spacescans.bridge"] + sys.argv[1:]
        runpy.run_module("spacescans.bridge", run_name="__main__", alter_sys=True)
    """))
    return [sys.executable, "-I", "-u", "-X", "utf8", str(runner)]


# --- envelope and events -----------------------------------------------------

def test_capabilities_envelope_and_events(tmp_path):
    rc, res, events, _ = call(tmp_path, "capabilities")
    assert rc == 0
    assert res["protocol"] == 1 and res["ok"] is True and res["command"] == "capabilities"
    import spacescans
    from spacescans._capabilities import code_fingerprint
    assert res["spacescans"]["version"] == spacescans.__version__
    assert res["spacescans"]["code_fp"] == code_fingerprint()
    assert res["error"] is None and isinstance(res["elapsed_s"], float)
    v = res["value"]
    assert v["protocol"] == 1 and v["protocol_range"] == [1, 1]
    assert {"capabilities", "schema", "resolve", "run", "quickstart", "templates",
            "yaml-load", "yaml-dump"} <= set(v["commands"])
    assert events[0]["ev"] == "start" and events[-1] == {**events[-1], "ev": "done", "ok": True, "exit": 0}
    assert all("t" in e for e in events)


def test_hello_is_an_alias(tmp_path):
    rc, res, _, _ = call(tmp_path, "hello")
    assert rc == 0 and res["value"]["protocol"] == 1


def test_stray_output_goes_to_stderr(tmp_path):
    rc, res, events, err = call(tmp_path, "ping", {"echo": {"a": [1, 2]}})
    assert rc == 0 and res["value"]["echo"] == {"a": [1, 2]}
    assert "stray print()" in err and "raw write to fd 1" in err
    assert [e["ev"] for e in events] == ["start", "done"]


def test_progress_events_are_rate_limited(tmp_path):
    rc, _, events, _ = call(tmp_path, "ping", {"sleep_s": 1.0})
    assert rc == 0
    prog = [e for e in events if e["ev"] == "progress"]
    assert 2 <= len(prog) <= 7, len(prog)       # <= 4/s plus the forced final 1.0
    assert prog[-1]["frac"] == 1.0


def test_no_result_flag_sends_result_event(tmp_path):
    p = subprocess.run(BRIDGE + ["ping"], capture_output=True, text=True, cwd=str(tmp_path))
    events = [json.loads(line) for line in p.stdout.splitlines()]
    assert p.returncode == 0
    assert events[-2]["ev"] == "result" and events[-2]["result"]["value"]["pong"] is True


# --- protocol errors (exit 2) ------------------------------------------------

@pytest.mark.parametrize("request_body,needle", [
    ("{not json", "not valid JSON"),
    ('["a"]', "JSON object"),
    (json.dumps({"protocol": 2}), "protocol 2"),
    (json.dumps({"client": {"name": "spacescans-r", "version": "9", "protocol_range": [2, 3]}}),
     "accepts protocol 2-3"),
    (json.dumps({"command": "run"}), "is for command 'run'"),
    (json.dumps({"args": [1]}), "'args' must be an object"),
])
def test_protocol_errors_exit_2_with_result(tmp_path, request_body, needle):
    rc, res, _, _ = call(tmp_path, "ping", request=request_body)
    assert rc == 2
    assert res["ok"] is False and res["error"]["kind"] == "protocol"
    assert needle in res["error"]["message"]


def test_unknown_command_exit_2(tmp_path):
    rc, res, _, _ = call(tmp_path, "no-such-command", request=None)
    assert rc == 2 and res["error"]["kind"] == "protocol"
    assert "run" in res["error"]["details"]["commands"]


# --- schema / resolve --------------------------------------------------------

def test_schema(tmp_path):
    rc, res, _, _ = call(tmp_path, "schema")
    assert rc == 0
    schema = res["value"]["schema"]
    assert "linkage_pattern" in schema["properties"]
    assert "EngineConfig" in schema["$defs"]
    assert "threads" in schema["$defs"]["EngineConfig"]["properties"]


def test_resolve_coerces_r_scalars_and_reports_unknown_keys(tmp_path):
    cfg = make_c4_fixture(tmp_path)
    cfg["exposure"]["value_cols"] = "v"        # R: value_cols = "v" (auto-unboxed)
    cfg["time"]["years"] = 2017                # R: years = 2017
    cfg["buffer"]["bufer_m"] = 300             # typo
    cfg["depends_on"] = "c3_bg"
    rc, res, _, _ = call(tmp_path, "resolve", {"config": cfg, "data_dir": str(tmp_path),
                                              "output_dir": str(tmp_path / "out")})
    assert rc == 0, res
    out = res["value"]["config"]
    assert out["exposure"]["value_cols"] == ["v"] and out["time"]["years"] == [2017]
    assert out["depends_on"] == ["c3_bg"]
    assert out["source"]["file"] == str(tmp_path / "weights.parquet")
    assert out["output"]["path"] == str(tmp_path / "out" / "c4_fixture.parquet")
    assert out["engine"]["threads"] is None
    assert res["value"]["unknown_keys"] == ["buffer.bufer_m"]
    assert res["warnings"][0]["code"] == "unknown_key"


def test_resolve_strict_rejects_unknown_keys(tmp_path):
    cfg = make_c4_fixture(tmp_path)
    cfg["outptu"] = {"path": "x"}
    rc, res, _, _ = call(tmp_path, "resolve", {"config": cfg, "data_dir": str(tmp_path), "strict": True})
    assert rc == 1 and res["error"]["kind"] == "config"
    assert res["error"]["details"]["unknown_keys"] == ["outptu"]


def test_resolve_yaml_path_and_relative_paths(tmp_path):
    cfg = make_c4_fixture(tmp_path)
    path = tmp_path / "c4.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    rc, res, _, _ = call(tmp_path, "resolve", {"config": str(path), "data_dir": str(tmp_path)})
    assert rc == 0 and res["value"]["config_source"] == {"source": "yaml", "path": str(path)}
    rc, res, _, _ = call(tmp_path, "resolve", {"config": "c4.yaml"})
    assert rc == 1 and res["error"]["kind"] == "config" and "absolute" in res["error"]["message"]
    rc, res, _, _ = call(tmp_path, "resolve", {"config": str(path), "data_dir": "rel/dir"})
    assert rc == 1 and res["error"]["kind"] == "config"


def test_validation_error_is_config_with_locations(tmp_path):
    cfg = make_c4_fixture(tmp_path)
    cfg["buffer"]["buffer_m"] = 270.5          # pydantic: not a whole number
    rc, res, _, _ = call(tmp_path, "resolve", {"config": cfg, "data_dir": str(tmp_path)})
    assert rc == 1
    err = res["error"]
    assert err["kind"] == "config" and err["type"] == "pydantic_core._pydantic_core.ValidationError"
    assert err["details"]["errors"][0]["loc"] == "buffer.buffer_m"
    assert "Traceback" in err["traceback"]


def test_missing_data_dir_for_relative_path_is_config(tmp_path, monkeypatch):
    monkeypatch.delenv("SPACESCANS_DATA_DIR", raising=False)
    cfg = make_c4_fixture(tmp_path)
    rc, res, _, _ = call(tmp_path, "resolve", {"config": cfg})
    assert rc == 1 and res["error"]["kind"] == "config"
    assert "relative but no data root" in res["error"]["message"]


# --- run ---------------------------------------------------------------------

def test_run_c4_on_base_install(tmp_path):
    cfg = make_c4_fixture(tmp_path)
    cfg["exposure"]["value_cols"] = "v"
    rc, res, events, _ = call(tmp_path, "run", {"config": cfg, "data_dir": str(tmp_path),
                                               "output_dir": str(tmp_path / "out"), "threads": 1,
                                               "workers": 2},
                              prefix=base_only_prefix(tmp_path))
    assert rc == 0, res["error"]
    v = res["value"]
    assert v["rows"] == 3 and v["columns"] == ["PATID", "geoid", "v"]
    assert v["output_grouping"] == "episode" and v["linkage_pattern"] == "yearly_areal"
    assert v["engine"]["threads"] == "1"
    assert v["config"]["engine"]["workers"] == 2
    assert v["maxrss_bytes"] > 10_000_000          # bytes, not KiB
    df = pd.read_parquet(v["output"])
    assert sorted(df["geoid"]) == [0, 1, 2]
    kinds = [e["ev"] for e in events]
    assert kinds[0] == "start" and "step_start" in kinds and "step_end" in kinds and kinds[-1] == "done"
    end = next(e for e in events if e["ev"] == "step_end")
    assert end["ok"] is True and end["rc"] == 0


def test_run_missing_input_is_data_error(tmp_path):
    cfg = make_c4_fixture(tmp_path)
    (tmp_path / "patients.parquet").unlink()
    rc, res, events, _ = call(tmp_path, "run", {"config": cfg, "data_dir": str(tmp_path)})
    assert rc == 1
    err = res["error"]
    # phase B: design 3.5's data_missing (R still gets spacescans_error_data for data_* kinds)
    assert err["kind"] == "data_missing" and err["type"] == "FileNotFoundError"
    assert err["details"]["path"].endswith("patients.parquet")
    assert any(e["ev"] == "step_end" and e["ok"] is False for e in events)


def test_missing_path_from_either_filenotfounderror_form():
    from spacescans.bridge.errors import _missing_path
    import errno
    # pandas < 3.1: FileNotFoundError(errno, strerror, path); pandas 3.1: FileNotFoundError(path)
    assert _missing_path(FileNotFoundError(errno.ENOENT, "No such file", "/d/patients.parquet")) == "/d/patients.parquet"
    assert _missing_path(FileNotFoundError("/d/patients.parquet")) == "/d/patients.parquet"
    assert _missing_path(FileNotFoundError("cohort file not found: /d/x.csv")) is None   # a message, not a path
    assert _missing_path(FileNotFoundError()) is None


def test_run_bad_threads_is_config(tmp_path):
    cfg = make_c4_fixture(tmp_path)
    rc, res, _, _ = call(tmp_path, "run", {"config": cfg, "data_dir": str(tmp_path), "threads": 0})
    assert rc == 1 and res["error"]["kind"] == "config" and "threads" in res["error"]["message"]


def test_run_bad_workers_is_config(tmp_path):
    cfg = make_c4_fixture(tmp_path)
    rc, res, _, _ = call(tmp_path, "run", {"config": cfg, "data_dir": str(tmp_path), "workers": 0})
    assert rc == 1 and res["error"]["kind"] == "config" and "workers" in res["error"]["message"]


def test_run_missing_extra_on_base_install(tmp_path):
    cfg = {"name": "c3", "linkage_pattern": "boundary_overlap_fast", "geometry_type": "polygon",
           "source": {"file": str(tmp_path / "a.shp"), "join_col": "G"},
           "buffer": {"patient_file": str(tmp_path / "p.parquet")},
           "output": {"path": str(tmp_path / "o.parquet")}}
    rc, res, _, _ = call(tmp_path, "run", {"config": cfg}, prefix=base_only_prefix(tmp_path))
    assert rc == 1
    err = res["error"]
    assert err["kind"] == "missing_extra", err
    assert err["details"]["extra"] == "geo"
    assert err["type"] == "spacescans.pipeline.registry.UnavailablePatternError"
    assert "spacescans[geo]" in err["hint"]


def test_capabilities_on_base_install(tmp_path):
    rc, res, _, _ = call(tmp_path, "capabilities", prefix=base_only_prefix(tmp_path))
    assert rc == 0
    v = res["value"]
    assert v["extras"]["geo"]["available"] is False
    assert "spacescans.linkage.boundary_overlap_fast_linkage" in v["registry"]["unavailable"]


@pytest.mark.geo
def test_quickstart(tmp_path):
    rc, res, events, _ = call(tmp_path, "quickstart", {"output_dir": str(tmp_path / "qs"), "threads": 2})
    assert rc == 0, res["error"]
    v = res["value"]
    assert v["demo"] is True and v["rows"] > 0
    assert v["columns"] == ["geoid", "GEOID10", "value"]
    assert Path(v["output"]).parent == tmp_path / "qs"
    assert any(e["ev"] == "progress" and e["text"] and "[overlap_fast]" in e["text"] for e in events)
    assert "SPACESCANS_QUICKSTART_DATA_DIR" not in os.environ


# --- templates / yaml --------------------------------------------------------

def test_templates_list_and_get(tmp_path):
    # phase B (#10): the init-config starter templates moved under kind "init"
    rc, res, _, _ = call(tmp_path, "templates", {"kind": "init"})
    names = [t["name"] for t in res["value"]["templates"]]
    assert rc == 0 and {"c3/county", "c4/zbp", "quickstart/end_to_end"} <= set(names)
    rc, res, _, _ = call(tmp_path, "templates", {"kind": "init", "name": "c3/county"})
    assert rc == 0 and res["value"]["config"]["linkage_pattern"] == "boundary_overlap_fast"
    assert res["value"]["text"].startswith("name: county_us")
    rc, res, _, _ = call(tmp_path, "templates", {"kind": "init", "name": "c3/nope"})
    assert rc == 1 and res["error"]["kind"] == "config" and "c3/county" in res["error"]["details"]["available"]


def test_yaml_dump_and_load_roundtrip(tmp_path):
    cfg = make_c4_fixture(tmp_path)
    cfg["exposure"]["value_cols"] = "v"
    target = tmp_path / "cfgs" / "c4.yaml"
    rc, res, _, _ = call(tmp_path, "yaml-dump", {"config": cfg, "path": str(target)})
    assert rc == 0 and target.read_text() == res["value"]["text"]
    assert yaml.safe_load(target.read_text())["exposure"]["value_cols"] == ["v"]
    assert list(yaml.safe_load(target.read_text())) == list(cfg)       # key order kept
    rc, res, _, _ = call(tmp_path, "yaml-load", {"path": str(target)})
    assert rc == 0 and res["value"]["config"]["name"] == "c4_fixture"
    rc, res, _, _ = call(tmp_path, "yaml-load", {"text": "a: [1, 2"})
    assert rc == 1 and res["error"]["kind"] == "config"
    rc, res, _, _ = call(tmp_path, "yaml-load", {"path": str(tmp_path / "missing.yaml")})
    assert rc == 1 and res["error"]["kind"] == "config"


# --- interrupts and crashes --------------------------------------------------

@pytest.mark.parametrize("sig,code", [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_signal_writes_interrupted_result(tmp_path, sig, code):
    req = tmp_path / "req.json"
    res = tmp_path / "res.json"
    req.write_text(json.dumps({"args": {"sleep_s": 60}}))
    p = subprocess.Popen(BRIDGE + ["ping", "--request", str(req), "--result", str(res)],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=str(tmp_path))
    assert json.loads(p.stdout.readline())["ev"] == "start"
    time.sleep(0.3)
    t0 = time.monotonic()
    p.send_signal(sig)
    rc = p.wait(timeout=10)
    assert rc == code and time.monotonic() - t0 < 2
    out = json.loads(res.read_text())
    assert out["ok"] is False and out["error"]["kind"] == "interrupted"


def test_crash_leaves_no_result(tmp_path):
    rc, res, _, _ = call(tmp_path, "ping", {"exit_code": 7})
    assert rc == 7 and res is None
    rc, res, _, _ = call(tmp_path, "ping", {"signal": int(signal.SIGKILL)})
    assert rc == -signal.SIGKILL and res is None


def test_requested_error_kind_round_trips(tmp_path):
    rc, res, _, _ = call(tmp_path, "ping", {"fail_kind": "data_missing"})
    assert rc == 1
    assert res["error"]["kind"] == "data_missing" and res["error"]["hint"] == "this is a test failure"


def test_run_captured_stays_interruptible():
    """The main thread must see SIGINT while the worker is stuck (as in a long C call)."""
    from spacescans.bridge.commands import run_captured
    release = threading.Event()
    lines: list[str] = []

    def stuck():
        print("worker started")
        release.wait(30)
        return "late"

    timer = threading.Timer(0.3, lambda: signal.raise_signal(signal.SIGINT))
    timer.start()
    previous = signal.getsignal(signal.SIGTERM)
    t0 = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            run_captured(stuck, lines.append)
    finally:  # an interrupt leaves both signals ignored
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, previous)
    assert time.monotonic() - t0 < 2
    release.set()
    assert lines == ["worker started"]


# --- unit: coercion, unknown keys, classification ---------------------------

def test_coerce_scalars_in_nested_and_union_fields():
    from spacescans.bridge.configs import coerce_scalars
    raw = {
        "name": "x", "linkage_pattern": "yearly_areal", "geometry_type": "polygon",
        "source": {"file": "w.parquet"},
        "buffer": {"patient_file": "p.parquet"},
        "exposure": {"file": "e.parquet", "value_cols": "v", "fill_na": {"v": 0}},
        "transforms": [
            {"type": "filter", "column": "c", "values": "a"},
            {"type": "recode", "rules": {"source_cols": "s", "output_col": "o",
                                          "conditions": [{"when": {"s": 1}, "then": 2}]}},
            {"type": "spatial_fix", "fix_type": "set_extent", "extent": [0, 0, 1, 1]},
        ],
        "output": {"path": "o.parquet"},
    }
    out = coerce_scalars(raw)
    assert out["source"]["file"] == "w.parquet"           # str | list[str]: scalar is fine
    assert out["exposure"]["value_cols"] == ["v"] and out["exposure"]["fill_na"] == {"v": 0}
    assert out["transforms"][0]["values"] == ["a"]
    assert out["transforms"][1]["rules"] == [{"source_cols": ["s"], "output_col": "o",
                                              "conditions": [{"when": {"s": 1}, "then": 2}]}]
    assert out["transforms"][2]["extent"] == [0, 0, 1, 1]
    assert raw["exposure"]["value_cols"] == "v"            # input untouched
    from spacescans.models.config import DatasetConfig
    DatasetConfig.model_validate(out)


def test_unknown_keys_nested_paths():
    from spacescans.bridge.configs import unknown_keys
    raw = {"name": "x", "linkage_pattern": "yearly_areal", "geometry_type": "polygon",
           "base_dir": "/d", "colour": 1,
           "source": {"file": "w", "jion_col": "G"},
           "buffer": {"patient_file": "p"},
           "transforms": [{"type": "filter", "column": "c", "valeus": ["a"]}],
           "engine": {"threads": 2, "thread": 2},
           "output": {"path": "o"}}
    assert unknown_keys(raw) == ["colour", "source.jion_col", "transforms[0].valeus", "engine.thread"]


def test_classify_mapping():
    from pydantic import ValidationError

    from spacescans._extras import MissingExtraError
    from spacescans.bridge.errors import classify
    from spacescans.config_resolution import ConfigResolutionError
    from spacescans.models.config import EngineConfig
    try:
        EngineConfig(threads=0)
    except ValidationError as e:
        assert classify(e).kind == "config"
    assert classify(ConfigResolutionError("x")).kind == "config"
    assert classify(MissingExtraError("m", extra="nc", missing=["xarray"])).details == {
        "extra": "nc", "missing": ["xarray"]}
    assert classify(KeyError("Unknown linkage pattern: zz. Registered: []")).kind == "config"
    assert classify(FileNotFoundError(2, "No such file", "/x")).kind == "data_missing"
    assert classify(PermissionError(13, "denied", "/x")).kind == "data_missing"
    assert classify(ValueError("boom")).kind == "internal"          # phase B (#7): not "runtime"
    assert classify(SystemExit(3)).kind == "internal"
    assert classify(ValueError("boom")).to_dict()["type"] == "ValueError"
