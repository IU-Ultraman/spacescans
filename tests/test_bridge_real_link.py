"""The bridge's ``link`` on a real data root (opt-in): synth_1k, noise.

This is the whole client path R uses, with nothing stood in: the bridge reads the
five-column cohort parquet, runs the real ``orchestrate.link`` with the
``SubprocessExecutor`` (``spacescans run`` per step) and the v2 ``WeightCache``,
and writes ``exposures.parquet``. The values are held to the golden web run at
``CROSS_ENV`` (the host is a different environment from the frozen image), a
second link must take its C3 step from the cache, and the data root must be
left untouched.

Needs ``SPACESCANS_REAL_DATA_DIR`` (a data root; only read) and the goldens
(``SPACESCANS_GOLDEN_DIR``) for the cohort and the reference values.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import golden_compare as gc

# tests/conftest.py removes SPACESCANS_* variables inside each test: read it at import.
REAL_DATA_DIR = os.environ.get("SPACESCANS_REAL_DATA_DIR") or None
BRIDGE = [sys.executable, "-I", "-u", "-X", "utf8", "-m", "spacescans.bridge"]
COHORT_COLS = ["pid", "startDate", "endDate", "longitude", "latitude"]

pytestmark = [pytest.mark.slow, pytest.mark.geo, pytest.mark.extras]


def _link(tmp: Path, name: str, args: dict) -> tuple[int, dict, list[dict]]:
    req, res = tmp / f"{name}.request.json", tmp / f"{name}.result.json"
    req.write_text(json.dumps({"protocol": 1, "command": "link",
                               "client": {"name": "pytest", "version": "0", "protocol_range": [1, 1]},
                               "args": args}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("SPACESCANS_")}
    p = subprocess.run(BRIDGE + ["link", "--request", str(req), "--result", str(res)],
                       capture_output=True, text=True, cwd=str(tmp), env=env, timeout=900)
    events = [json.loads(line) for line in p.stdout.splitlines()]
    return p.returncode, json.loads(res.read_text()), events


def test_bridge_link_on_real_data_matches_the_golden_noise_run(tmp_path, golden_dir):
    if not REAL_DATA_DIR or not Path(REAL_DATA_DIR).is_dir():
        pytest.skip("set SPACESCANS_REAL_DATA_DIR to a data root to run a real link")
    # the cohort as R writes it: five columns, pid text, dates as dates
    raw = pd.read_csv(golden_dir / "inputs" / "synth_1k.csv", dtype={"pid": str})
    co = pd.DataFrame({"pid": raw["pid"].astype(str),
                       "startDate": pd.to_datetime(raw["startDate"]).dt.date,
                       "endDate": pd.to_datetime(raw["endDate"]).dt.date,
                       "longitude": raw["longitude"].astype(float),
                       "latitude": raw["latitude"].astype(float)})
    co.to_parquet(tmp_path / "cohort.parquet", index=False)
    stamp = tmp_path / "stamp"
    stamp.touch()
    time.sleep(1.1)
    args = {"cohort": str(tmp_path / "cohort.parquet"), "variables": ["noise"],
            "data_dir": REAL_DATA_DIR, "run_dir": str(tmp_path / "run1"),
            "cache_dir": str(tmp_path / "cache"), "threads": 2, "on_error": "stop"}
    rc, res, events = _link(tmp_path, "link1", args)
    assert rc == 0, res["error"]
    v = res["value"]
    assert v["rows"] == v["episodes"] == len(co) and v["failed"] == [] and v["skipped"] == []
    assert [(s["name"], s["status"]) for s in v["steps"]] == [("c3_noise", "ran"), ("c4_noise", "ran")]

    gold = pd.read_csv(golden_dir / "noise" / "output" / "result.csv", dtype={"pid": str})
    values = [c for c in gold.columns if c not in COHORT_COLS]
    ours = pd.read_parquet(v["exposures"])
    assert list(ours["episode_id"]) == list(range(len(co)))
    assert [c for c in ours.columns if c != "episode_id"] == values
    for col in values:
        e, a = gold[col].to_numpy(float), ours[col].to_numpy(float)
        assert (np.isnan(e) == np.isnan(a)).all(), f"{col}: missing-value mask differs"
        rtol, atol = gc.CROSS_ENV.for_column(col)
        ok = np.isclose(a, e, rtol=rtol, atol=atol, equal_nan=True)
        assert ok.all(), f"{col}: {int((~ok).sum())} value(s) outside CROSS_ENV"

    run1 = Path(v["run_dir"])
    log = [json.loads(x) for x in (run1 / "logs.jsonl").read_text().splitlines()]
    msgs = [x.get("msg", "") for x in log]
    assert any(m.startswith("spawning c3_noise: ") for m in msgs)
    assert "step c3_noise exit code 0" in msgs and any(m.startswith("cache write: ") for m in msgs)
    assert {"step_start", "spawn", "step_end", "cache_write"} <= {e["ev"] for e in events}

    # a second link takes the C3 weights from the cache and gives the same values
    rc, res2, events2 = _link(tmp_path, "link2", {**args, "run_dir": str(tmp_path / "run2")})
    assert rc == 0, res2["error"]
    assert [(s["name"], s["status"]) for s in res2["value"]["steps"]] == [
        ("c3_noise", "cache_hit"), ("c4_noise", "ran")]
    assert any(e["ev"] == "cache_hit" for e in events2)
    pd.testing.assert_frame_equal(pd.read_parquet(res2["value"]["exposures"]), ours)

    # nothing in the data root changed
    newer = subprocess.run(["find", REAL_DATA_DIR, "-newer", str(stamp)],
                           capture_output=True, text=True).stdout.split()
    assert newer == []
