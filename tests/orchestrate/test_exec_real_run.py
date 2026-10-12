"""One real C3 step through SubprocessExecutor: c3_noise on synth_1k (opt-in).

Needs SPACESCANS_GOLDEN_DIR (the web 0.2.7 goldens) and SPACESCANS_REAL_DATA_DIR (a
pipeline data root holding Noise/C3/CONUS_L50dBA_sumDay_exi.tif), both read at import,
plus the [geo] extra. The data root is only read: a stamp is touched before the run and
``find <data_dir> -newer <stamp>`` must print nothing afterwards.

The step is the golden noise run's own ``pipeline_configs/c3_noise.yaml`` with its task
paths rewritten to this run's directory, reading a cohort parquet written by
``cohort.to_pipeline_parquet`` from the golden synth_1k CSV. The command prefix is the
web's form, ``[<python>, <its spacescans script>]``, so the log's spawn line differs
from the golden one only by deployment paths (PIPELINE_PYTHON, PIPELINE_CLI, the data
mount) and, when pandas differs from the frozen image's, by the cohort sha8 prefix of
the cache key (golden README rule 7).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from spacescans import catalog, cohort
from spacescans.orchestrate.events import LOG_CACHE_WRITE, Event, LogWriter, tee
from spacescans.orchestrate.execute import SubprocessExecutor, execute_step

REPO = Path(__file__).resolve().parents[2]
_GOLDEN_ENV = os.environ.get("SPACESCANS_GOLDEN_DIR")
GOLDEN = Path(_GOLDEN_ENV) if _GOLDEN_ENV else None
_DATA_ENV = os.environ.get("SPACESCANS_REAL_DATA_DIR")
DATA = Path(_DATA_ENV) if _DATA_ENV else None
NOISE_GRID = "Noise/C3/CONUS_L50dBA_sumDay_exi.tif"

READY = (GOLDEN is not None and (GOLDEN / "noise" / "pipeline_configs" / "c3_noise.yaml").is_file()
         and (GOLDEN / "noise" / "output" / "c3_noise.parquet").is_file()
         and DATA is not None and (DATA / NOISE_GRID).is_file())

pytestmark = [
    pytest.mark.geo,
    pytest.mark.extras,
    pytest.mark.skipif(not READY, reason="needs SPACESCANS_GOLDEN_DIR (with outputs) and "
                                         "SPACESCANS_REAL_DATA_DIR holding the noise grid"),
    pytest.mark.skipif(os.name != "posix", reason="POSIX only"),
]

# The frozen web image's deployment (golden noise/run.json, logs.jsonl).
WEB_COMMAND = "/opt/conda/bin/python /opt/conda/bin/spacescans run --data-dir /project "


def _golden_compare():
    if "golden_compare" in sys.modules:
        return sys.modules["golden_compare"]
    spec = importlib.util.spec_from_file_location(
        "golden_compare", REPO / "tests" / "golden" / "golden_compare.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["golden_compare"] = module
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_TIMING = [
    (re.compile(r"elapsed=\s*[\d.]+m"), "elapsed=<T>"),
    (re.compile(r"rate=\s*[\d.]+/s"), "rate=<R>"),
    (re.compile(r"ETA=\s*[\d.]+m"), "ETA=<T>"),
    (re.compile(r"\s+[\d.]+m \(\s*[\d.]+%\)$"), " <T> (<P>)"),
    (re.compile(r"(total)\s+[\d.]+m$"), r"\1 <T>"),
]


def _pipeline_lines(logs_jsonl: Path, step: str, task_dir: str) -> list[str]:
    """The step's own output lines, with the task dir and timing text normalised
    (golden README rule 8), sorted: the SUMMARY rows are ordered by time share."""
    out = []
    for line in logs_jsonl.read_text().splitlines():
        rec = json.loads(line)
        msg = rec.get("msg", "")
        if rec.get("source") != step or msg.startswith(("cache hit: ", "cache write: ")):
            continue
        msg = msg.replace(task_dir, "<TASK>")
        for pattern, repl in _TIMING:
            msg = pattern.sub(repl, msg)
        out.append(msg)
    return sorted(out)


@pytest.fixture(scope="module")
def real_run(tmp_path_factory):
    """Run c3_noise once for the tests below; returns what they check."""
    import pandas as pd

    gc = _golden_compare()
    golden_run = json.loads((GOLDEN / "noise" / "run.json").read_text())
    golden_task = golden_run["task_dir"]
    run_dir = tmp_path_factory.mktemp("real") / "task-orchexec-c3noise"
    (run_dir / "output").mkdir(parents=True)

    # 1. The cohort parquet, as the web's csv_to_parquet writes it (legacy path).
    info = cohort.to_pipeline_parquet(GOLDEN / "inputs" / "synth_1k.csv",
                                      run_dir / "input.parquet", pid_as_str=False)
    golden_input = GOLDEN / "noise" / "input.parquet"

    # 2. The golden YAML with its task paths rewritten (orch-core's render is not on this branch).
    golden_yaml = (GOLDEN / "noise" / "pipeline_configs" / "c3_noise.yaml").read_text()
    text = (golden_yaml.replace(golden_task, str(run_dir))
            .replace(f"_task_{Path(golden_task).name[-8:]}", f"_task_{run_dir.name[-8:]}"))
    config = run_dir / "pipeline_configs" / "c3_noise.yaml"
    config.parent.mkdir()
    config.write_text(text)

    # 3. Stamp, then run against the read-only data root.
    stamp = run_dir / "data-dir.stamp"
    stamp.touch()
    time.sleep(1.1)   # so a write in the stamp's second still counts as newer
    script = Path(sys.executable).parent / "spacescans"   # never resolve the venv symlink
    executor = SubprocessExecutor(cmd_prefix=[sys.executable, str(script)])
    writer = LogWriter.for_run_dir(run_dir)
    events: list[Event] = []
    run = execute_step(executor, config, experiment="noise", step="c3_noise", data_dir=DATA,
                       cwd=run_dir, on_event=tee(writer, events.append), index=0, total=2)
    sha = _sha256(run_dir / "input.parquet")
    if run.rc == 0:
        # What run_source writes after storing the C3 output (the cache itself is orch-core's).
        step = {s.name: s for s in catalog.plan("noise", ["noise"])}["c3_noise"]
        key = catalog.legacy_cache_key(step, sha, buffer_m=270, raster_res_m=25)
        writer(Event("cache_write", "noise", step="c3_noise",
                     text=LOG_CACHE_WRITE.format(file=f"{key}.parquet")))
    found = subprocess.run(["find", str(DATA), "-newer", str(stamp)], capture_output=True,
                           text=True, check=False)
    output = run_dir / "output" / "c3_noise.parquet"
    return {
        "gc": gc, "info": info, "golden_input": golden_input, "golden_yaml": golden_yaml,
        "golden_task": golden_task, "text": text, "run_dir": run_dir, "run": run,
        "events": events, "sha": sha, "script": script, "found": found,
        "golden_c3": pd.read_parquet(GOLDEN / "noise" / "output" / "c3_noise.parquet"),
        "c3": pd.read_parquet(output) if output.is_file() else None,
    }


def test_c3_noise_runs_on_synth_1k_without_writing_to_the_data_dir(real_run):
    import pandas as pd

    r, gc = real_run, real_run["gc"]
    assert r["info"].rows == 1001
    pd.testing.assert_frame_equal(pd.read_parquet(r["run_dir"] / "input.parquet"),
                                  pd.read_parquet(r["golden_input"]))
    assert gc.normalise_yaml(r["text"], str(r["run_dir"])) == gc.normalise_yaml(
        r["golden_yaml"], r["golden_task"])
    run = r["run"]
    assert run.rc == 0 and run.ok, run.tail
    assert run.maxrss_bytes and run.maxrss_bytes > 50 * 2**20
    assert [e.kind for e in r["events"]][0] == "spawn" and r["events"][-1].kind == "step_end"
    assert r["found"].returncode == 0 and r["found"].stdout == "", r["found"].stdout[:2000]


def test_c3_noise_logs_match_the_golden_runner_lines(real_run):
    r, gc = real_run, real_run["gc"]
    # Deployment paths of this host -> the frozen image's; the cohort sha8 prefix differs
    # only when pandas does (the parquet embeds its version; golden README rule 7).
    deployment = {f"{sys.executable} {r['script']} run --data-dir {DATA} ": WEB_COMMAND,
                  f": {r['sha'][:8]}__": f": {_sha256(r['golden_input'])[:8]}__"}

    def as_deployed(line: str) -> str:
        for old, new in deployment.items():
            line = line.replace(old, new)
        return line

    mine = [as_deployed(x) for x in gc.runner_log_lines(r["run_dir"] / "logs.jsonl",
                                                        str(r["run_dir"]))]
    golden = [x for x in gc.runner_log_lines(GOLDEN / "noise" / "logs.jsonl", r["golden_task"])
              if "c3_noise" in x]
    assert len(golden) == 3
    assert mine == golden
    # The step's own output lines, normalised (paths, timing text).
    assert (_pipeline_lines(r["run_dir"] / "logs.jsonl", "c3_noise", str(r["run_dir"]))
            == _pipeline_lines(GOLDEN / "noise" / "logs.jsonl", "c3_noise", r["golden_task"]))


def test_c3_noise_weights_have_the_golden_keys(real_run):
    import numpy as np

    r, gc = real_run, real_run["gc"]
    assert r["c3"] is not None
    e, a = gc.canonical_table(r["golden_c3"]), gc.canonical_table(r["c3"])
    assert list(a.columns) == list(e.columns) and len(a) == len(e)
    assert (a[["geoid", "grid_id"]].to_numpy() == e[["geoid", "grid_id"]].to_numpy()).all()
    assert (np.isnan(a["weight"].to_numpy()) == np.isnan(e["weight"].to_numpy())).all()
    sums = (a.groupby("geoid")["weight"].sum() - e.groupby("geoid")["weight"].sum()).abs()
    assert sums.max() <= 1e-12


@pytest.mark.xfail(strict=False, reason=(
    "host macOS (GDAL 3.12.2, rasterio 1.5.2) vs the frozen Linux image (GDAL 3.12.3, rasterio "
    "1.5.0): measured 2026-10-12, 43 of 8,098 weights differ and 3 exceed CROSS_ENV, at most "
    "8.6e-9 abs; keys and per-episode sums agree. Reported to orch-link for A4."))
def test_c3_noise_weights_within_cross_env(real_run):
    r, gc = real_run, real_run["gc"]
    assert r["c3"] is not None
    assert gc.compare_tables(r["golden_c3"], r["c3"], tol=gc.CROSS_ENV) == []
