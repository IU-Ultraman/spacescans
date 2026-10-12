"""orchestrate.events: the web's log strings, progress parsing, Event and LogWriter.

The golden replay test (opt-in: SPACESCANS_GOLDEN_DIR, read at import) rebuilds every
golden run's ``logs.jsonl`` through this API, from structured inputs only (step names,
config paths, exit codes, the catalog's legacy cache keys), and requires the
runner-authored lines to equal the golden ones after ``golden_compare.runner_log_lines``.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from spacescans.orchestrate.events import (
    EVENT_KINDS,
    LOG_CACHE_CHECK_FAILED,
    LOG_CACHE_HIT,
    LOG_CACHE_WRITE,
    LOG_CACHE_WRITE_FAILED,
    LOG_EXIT,
    LOG_SPAWN,
    PROGRESS_RE,
    Event,
    LogWriter,
    parse_progress,
    tee,
)
from spacescans.orchestrate.execute import SubprocessExecutor, command_line

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"

_GOLDEN_ENV = os.environ.get("SPACESCANS_GOLDEN_DIR")
GOLDEN = Path(_GOLDEN_ENV) if _GOLDEN_ENV else None
HAVE_GOLDEN = GOLDEN is not None and (GOLDEN / "manifest.json").is_file()
_WEB_ENV = os.environ.get("SPACESCANS_WEB_BACKEND")
WEB_RUNNER = Path(_WEB_ENV) / "app" / "experiments" / "bg_ndi_wi.py" if _WEB_ENV else None

#: The web's deployment in the frozen image: PIPELINE_PYTHON, PIPELINE_CLI, the data mount.
WEB_PREFIX = ("/opt/conda/bin/python", "/opt/conda/bin/spacescans")
WEB_DATA_DIR = Path("/project")


def _golden_compare():
    """tests/golden/golden_compare.py, the golden track's comparison helpers."""
    if "golden_compare" in sys.modules:
        return sys.modules["golden_compare"]
    spec = importlib.util.spec_from_file_location(
        "golden_compare", REPO / "tests" / "golden" / "golden_compare.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["golden_compare"] = module  # its dataclasses look the module up
    spec.loader.exec_module(module)
    return module


def _golden_runs() -> list[str]:
    if not HAVE_GOLDEN:
        return []
    manifest = json.loads((GOLDEN / "manifest.json").read_text())
    runs = []
    for name in sorted(manifest["runs"]):
        run_json = GOLDEN / name / "run.json"
        if (GOLDEN / name / "logs.jsonl").is_file() and run_json.is_file() \
                and "experiment" in json.loads(run_json.read_text()):
            runs.append(name)
    return runs


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line, expected", [
    ("[overlap_fast] tile 7460/14938 ( 49.9%) elapsed= 1.2m", 7460 / 14938),
    ("[overlap]   1600/3221 ( 49.7%)", 1600 / 3221),
    ("[nhd_proximity] tile 5/100 ( 5.0%) elapsed=0.50m", 0.05),
    ("  [overlap_fast] tile 1/4", 0.25),                      # anywhere in the line
    ("[overlap_fast] tile 12/10", 1.0),                       # clamped
    ("[overlap_fast] tile 3/0", None),                        # no total
    ("[grid_weights]   1001/1001 (100.0%)  elapsed= 0.01m", None),   # not a web progress line
    ("[tiger_proximity] year 2013: 10/20", None),
    ("[overlap_fast] === SUMMARY ===", None),
    ("Output: /x/c3_bg.parquet", None),
    ("", None),
])
def test_parse_progress_reads_the_web_runners_progress_lines(line, expected):
    got = parse_progress(line)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected, rel=0, abs=0)


@pytest.mark.skipif(WEB_RUNNER is None or not WEB_RUNNER.is_file(),
                    reason="SPACESCANS_WEB_BACKEND not set")
def test_progress_regex_is_the_web_runners_regex():
    tree = ast.parse(WEB_RUNNER.read_text())
    patterns = [
        node.value.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_PROGRESS_RE" for t in node.targets)
    ]
    assert patterns == [PROGRESS_RE.pattern]


def test_bridge_reexports_parse_progress_without_importing_pandas():
    from spacescans.bridge import events as bridge_events
    from spacescans.bridge.events import parse_progress as from_bridge

    assert from_bridge is parse_progress
    assert bridge_events.parse_progress is parse_progress
    code = ("import sys, spacescans.bridge.events, spacescans.bridge.commands\n"
            "print('pandas' in sys.modules)\n")
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, check=True)
    assert out.stdout.strip() == "False"


def test_events_execute_and_locks_import_with_the_stdlib_only():
    code = ("import sys\n"
            "before = set(sys.modules)\n"
            "import spacescans.orchestrate.events as e, spacescans.orchestrate.execute as x, "
            "spacescans.orchestrate.locks as l\n"
            "heavy = [m for m in ('pandas', 'numpy', 'yaml', 'pyarrow', 'duckdb') "
            "if m in sys.modules and m not in before]\n"
            "print(heavy)\n")
    # The package __init__ (merge, coverage) imports pandas; the three modules add nothing
    # heavier on top of it, so import them with spacescans.orchestrate stubbed out.
    stub = ("import sys, types, pathlib\n"
            f"pkg = types.ModuleType('spacescans.orchestrate'); "
            f"pkg.__path__ = [{str(SRC / 'spacescans' / 'orchestrate')!r}]\n"
            "sys.modules['spacescans.orchestrate'] = pkg\n")
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run([sys.executable, "-c", stub + code], capture_output=True, text=True,
                         env=env, check=True)
    assert out.stdout.strip() == "[]"


# ---------------------------------------------------------------------------
# the web's strings
# ---------------------------------------------------------------------------

def test_log_strings_are_the_web_runners_strings():
    # Literal lines from the golden web 0.2.7 runs (tests/golden/web-0.2.7/*/logs.jsonl).
    assert LOG_SPAWN.format(
        step="c3_noise",
        cmd="/opt/conda/bin/python /opt/conda/bin/spacescans run --data-dir /project "
            "/app/data/tasks/tier2-golden-noise/pipeline_configs/c3_noise.yaml",
    ) == ("spawning c3_noise: /opt/conda/bin/python /opt/conda/bin/spacescans run --data-dir "
          "/project /app/data/tasks/tier2-golden-noise/pipeline_configs/c3_noise.yaml")
    assert LOG_EXIT.format(step="c3_noise", rc=0) == "step c3_noise exit code 0"
    assert LOG_EXIT.format(step="c4_x", rc=-9) == "step c4_x exit code -9"
    assert (LOG_CACHE_HIT.format(key="12269dbe__BG__b270m__r25m")
            == "cache hit: 12269dbe__BG__b270m__r25m — skipping pipeline run")
    assert (LOG_CACHE_WRITE.format(file="12269dbe__BG_NOISE__b270m.parquet")
            == "cache write: 12269dbe__BG_NOISE__b270m.parquet")
    err = OSError(28, "No space left on device")
    assert (LOG_CACHE_WRITE_FAILED.format(exc=err)
            == f"cache write failed: {err!r} — continuing")
    assert (LOG_CACHE_CHECK_FAILED.format(step="c3_bg", exc=err)
            == f"cache check failed for c3_bg: {err!r} — running fresh")


def test_web_prefix_gives_the_web_spawn_line():
    ex = SubprocessExecutor(cmd_prefix=WEB_PREFIX)
    config = Path("/app/data/tasks/tier2-golden-noise/pipeline_configs/c3_noise.yaml")
    assert LOG_SPAWN.format(step="c3_noise", cmd=command_line(ex, config, WEB_DATA_DIR)) == (
        "spawning c3_noise: /opt/conda/bin/python /opt/conda/bin/spacescans run --data-dir "
        "/project /app/data/tasks/tier2-golden-noise/pipeline_configs/c3_noise.yaml")


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

def test_event_kinds_are_the_contract_kinds():
    assert EVENT_KINDS == ("plan", "cohort", "step_start", "cache_hit", "spawn", "line",
                           "progress", "step_end", "cache_write", "cache_warning", "merge",
                           "warning", "done")
    with pytest.raises(ValueError, match="unknown event kind"):
        Event("spawned", "noise")


def test_event_overall_is_the_web_slot_progress():
    assert Event("progress", "x", step="c3", index=1, total=4, frac=0.5).overall == 1.5 / 4
    assert Event("step_end", "x", step="c3", index=1, total=4).overall == 2 / 4
    assert Event("cache_hit", "x", step="c3", index=0, total=4).overall == 1 / 4
    assert Event("spawn", "x", step="c3", index=3, total=4).overall == 3 / 4
    assert Event("progress", "x", frac=0.5).overall is None


def test_event_to_dict_keeps_set_fields():
    ev = Event("step_end", "noise", step="c3_noise", index=0, total=2,
               text="step c3_noise exit code 0", data={"rc": 0}, t=1.5)
    assert ev.to_dict() == {"kind": "step_end", "experiment": "noise", "step": "c3_noise",
                            "index": 0, "total": 2, "text": "step c3_noise exit code 0",
                            "data": {"rc": 0}, "t": 1.5}
    json.dumps(ev.to_dict())


def test_tee_hands_each_event_to_every_sink_in_order():
    seen: list[tuple[str, str]] = []
    emit = tee(lambda e: seen.append(("a", e.kind)), None, lambda e: seen.append(("b", e.kind)))
    emit(Event("plan", "noise"))
    emit(Event("done", "noise"))
    assert seen == [("a", "plan"), ("b", "plan"), ("a", "done"), ("b", "done")]


# ---------------------------------------------------------------------------
# LogWriter
# ---------------------------------------------------------------------------

def test_write_uses_the_web_record_format(tmp_path):
    w = LogWriter.for_run_dir(tmp_path / "run")
    w.write("info", "c3_noise", "[grid_weights] processing 1001 buffers × raster=x.tif")
    raw = (tmp_path / "run" / "logs.jsonl").read_text()
    assert "\\u00d7" in raw  # json.dumps escapes like the web's _append_log
    (rec,) = _records(tmp_path / "run" / "logs.jsonl")
    assert list(rec) == ["ts", "level", "source", "msg"]
    assert rec["msg"].endswith("× raster=x.tif")
    ts = datetime.fromisoformat(rec["ts"])
    assert ts.tzinfo == timezone.utc and rec["ts"].endswith("+00:00")


def test_event_timestamps_become_the_web_iso_ts(tmp_path):
    w = LogWriter(tmp_path / "logs.jsonl")
    w(Event("spawn", "noise", step="c3_noise", text="spawning c3_noise: x", t=1760301802.99093))
    (rec,) = _records(tmp_path / "logs.jsonl")
    assert rec["ts"] == datetime.fromtimestamp(1760301802.99093, tz=timezone.utc).isoformat()


@pytest.mark.parametrize("event, expected", [
    (Event("spawn", "noise", step="c3_noise", text="spawning c3_noise: cmd"),
     ("info", "runner", "spawning c3_noise: cmd")),
    (Event("line", "noise", step="c3_noise", text="  [grid_weights] x"),
     ("info", "c3_noise", "  [grid_weights] x")),
    (Event("step_end", "noise", step="c3_noise", text="step c3_noise exit code 0", data={"rc": 0}),
     ("info", "runner", "step c3_noise exit code 0")),
    (Event("step_end", "noise", step="c3_noise", text="step c3_noise exit code 1", data={"rc": 1}),
     ("error", "runner", "step c3_noise exit code 1")),
    (Event("step_end", "noise", step="c3_noise", text="step c3_noise exit code -9",
           data={"rc": -9}), ("error", "runner", "step c3_noise exit code -9")),
    (Event("cache_hit", "bg_ndi_wi", step="c3_bg", text="cache hit: k — skipping pipeline run"),
     ("info", "c3_bg", "cache hit: k — skipping pipeline run")),
    (Event("cache_write", "noise", step="c3_noise", text="cache write: k.parquet"),
     ("info", "c3_noise", "cache write: k.parquet")),
    (Event("cache_warning", "noise", step="c3_noise", text="cache write failed: E — continuing"),
     ("warning", "c3_noise", "cache write failed: E — continuing")),
    (Event("merge", "custom", text="custom_x.v: 100.0% of episodes linked (1,001/1,001)"),
     ("info", "merge", "custom_x.v: 100.0% of episodes linked (1,001/1,001)")),
    (Event("merge", "custom", text="custom_x.v: 10.0% of episodes linked (1/10)",
           data={"level": "warning"}),
     ("warning", "merge", "custom_x.v: 10.0% of episodes linked (1/10)")),
    (Event("warning", "noise", text="render_yaml(c4_noise) failed: E",
           data={"level": "error", "source": "runner"}),
     ("error", "runner", "render_yaml(c4_noise) failed: E")),
    (Event("line", "custom", step="c4_custom_1", text="materialised 8,047 (grid_id) rows"),
     ("info", "c4_custom_1", "materialised 8,047 (grid_id) rows")),
])
def test_logged_kinds_get_the_web_level_and_source(tmp_path, event, expected):
    w = LogWriter(tmp_path / "logs.jsonl")
    w(event)
    (rec,) = _records(tmp_path / "logs.jsonl")
    assert (rec["level"], rec["source"], rec["msg"]) == expected


@pytest.mark.parametrize("kind", ["plan", "cohort", "step_start", "progress", "done"])
def test_status_kinds_are_not_logged(tmp_path, kind):
    w = LogWriter(tmp_path / "logs.jsonl")
    w(Event(kind, "noise", step="c3_noise", frac=0.5, text="something"))
    assert not (tmp_path / "logs.jsonl").exists()


def test_a_warning_without_text_is_the_web_msgless_record(tmp_path):
    from spacescans.orchestrate.merge import MatchStats

    stats = MatchStats(experiment="vnl", cohort_n=1001, matched_n=700, match_pct=69.93)
    w = LogWriter(tmp_path / "logs.jsonl")
    w(Event("warning", "vnl", data=stats.as_log_fields(), t=1760301802.5))
    (rec,) = _records(tmp_path / "logs.jsonl")
    # _merge._emit_log_warning: {"ts": time.time(), "level": "warning", **fields}
    assert rec == {"ts": 1760301802.5, "level": "warning", "experiment_key": "vnl",
                   "event": "merge_partial_low_match_pct", "match_pct": 69.93,
                   "cohort_n": 1001, "matched_n": 700}
    assert list(rec) == ["ts", "level", "experiment_key", "event", "match_pct", "cohort_n",
                         "matched_n"]


def test_writes_from_many_threads_stay_whole_lines(tmp_path):
    import threading

    w = LogWriter(tmp_path / "logs.jsonl")

    def burst(n: int) -> None:
        for i in range(50):
            w.write("info", f"s{n}", "x" * 300 + str(i))

    threads = [threading.Thread(target=burst, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    recs = _records(tmp_path / "logs.jsonl")
    assert len(recs) == 400
    assert sorted(r["msg"][300:] for r in recs if r["source"] == "s3") == sorted(
        str(i) for i in range(50))


# ---------------------------------------------------------------------------
# golden replay: every golden run's runner lines, rebuilt through this API
# ---------------------------------------------------------------------------

def _replay(name: str, out: Path) -> str:
    from spacescans import catalog

    run = json.loads((GOLDEN / name / "run.json").read_text())
    config = json.loads((GOLDEN / name / "config.json").read_text())
    exp, task_dir = run["experiment"], run["task_dir"]
    overlay = config.get("custom_variables") if exp == "custom" else None
    steps = {s.name: s for s in catalog.plan(exp, run["variables"], overlay=overlay)}
    sha = hashlib.sha256((GOLDEN / name / "input.parquet").read_bytes()).hexdigest()
    buffer_m = config["buffer"]["size"]
    res = config["buffer"].get("raster_res_m", 25)
    executor = SubprocessExecutor(cmd_prefix=WEB_PREFIX)
    writer = LogWriter(out)
    for rec in _records(GOLDEN / name / "logs.jsonl"):
        if "msg" not in rec:
            writer(Event("warning", exp, data={k: v for k, v in rec.items() if k != "ts"}))
            continue
        level, src, msg = rec["level"], rec["source"], rec["msg"]
        if src == "runner" and msg.startswith("spawning "):
            step = msg[len("spawning "):].split(":", 1)[0]
            config_path = Path(task_dir) / "pipeline_configs" / f"{step}.yaml"
            cmd = command_line(executor, config_path, WEB_DATA_DIR)
            writer(Event("spawn", exp, step=step, text=LOG_SPAWN.format(step=step, cmd=cmd)))
        elif src == "runner" and msg.startswith("step "):
            m = re.fullmatch(r"step (\S+) exit code (-?\d+)", msg)
            step, rc = m.group(1), int(m.group(2))
            writer(Event("step_end", exp, step=step, text=LOG_EXIT.format(step=step, rc=rc),
                         data={"rc": rc}))
        elif msg.startswith(("cache hit: ", "cache write: ")):
            key = catalog.legacy_cache_key(steps[src], sha, buffer_m=buffer_m, raster_res_m=res)
            if msg.startswith("cache hit: "):
                writer(Event("cache_hit", exp, step=src, text=LOG_CACHE_HIT.format(key=key)))
            else:
                writer(Event("cache_write", exp, step=src,
                             text=LOG_CACHE_WRITE.format(file=f"{key}.parquet")))
        elif src == "merge":  # the upload runner's linkage report (custom track's text)
            writer(Event("merge", exp, text=msg,
                         data=None if level == "info" else {"level": level}))
        else:  # pipeline output; the raster upload's "materialised" note on its C4 step
            writer(Event("line", exp, step=src, text=msg))
    return task_dir


@pytest.mark.skipif(not HAVE_GOLDEN, reason="SPACESCANS_GOLDEN_DIR not set or incomplete")
@pytest.mark.parametrize("name", _golden_runs() or ["<no goldens>"])
def test_golden_runner_log_lines_rebuilt_through_the_api(tmp_path, name):
    gc = _golden_compare()
    out = tmp_path / "logs.jsonl"
    task_dir = _replay(name, out)
    expected = gc.runner_log_lines(GOLDEN / name / "logs.jsonl", task_dir)
    assert expected, f"{name}: no runner lines in the golden log"
    assert gc.runner_log_lines(out, task_dir) == expected
    # Every record, not only the runner's, has the same level, source and text in order.
    strip = [(r.get("level"), r.get("source"), r.get("msg")) for r in _records(out)]
    want = [(r.get("level"), r.get("source"), r.get("msg"))
            for r in _records(GOLDEN / name / "logs.jsonl")]
    assert strip == want


@pytest.mark.skipif(not HAVE_GOLDEN, reason="SPACESCANS_GOLDEN_DIR not set or incomplete")
def test_golden_replay_covers_every_runner_line_kind():
    kinds = set()
    for name in _golden_runs():
        for rec in _records(GOLDEN / name / "logs.jsonl"):
            msg = rec.get("msg", "")
            for prefix in ("spawning ", "step ", "cache hit: ", "cache write: ", "materialised "):
                if msg.startswith(prefix):
                    kinds.add(prefix)
            if rec.get("source") == "merge":
                kinds.add("merge")
    assert kinds == {"spawning ", "step ", "cache hit: ", "cache write: ", "materialised ",
                     "merge"}
