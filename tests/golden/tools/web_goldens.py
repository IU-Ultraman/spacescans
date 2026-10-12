#!/usr/bin/env python3
"""Regenerate the tier-2 golden references from the 0.2.7 web runners.

Every catalog experiment and two upload (custom) cases run on synth_1k inside the running
backend container, the way spacescans-paper/results/experiments/custom_equivalence.py runs
them: one task directory per run, /app/data/tasks/tier2-golden-<name>/ holding input.csv and
config.json, then

    cd /app && /opt/conda/bin/python -m app.experiments.<exp> run <task_dir> --variables <keys>

Deliberate differences from custom_equivalence.py:

* Each run gets its own empty C3 weight cache. The driver sets
  C3_CACHE_DIR=<task_dir>/golden_c3_cache on `docker exec`, and app.config.Settings (a
  pydantic BaseSettings) reads it from the environment. Every C3 step is therefore computed
  cold and every step's YAML is rendered. The production cache /app/data/c3_cache is neither
  read nor written. The exception is bg_ndi_wi_rev, which reuses bg_ndi_wi's private cache on
  purpose so the cache-hit path (no YAML rendered, "cache hit" log line) is recorded too.
* The runner's stdout, stderr and exit code are kept, not discarded.
* After each successful run the dispatcher's fan-in, `_merge.fan_in(task_dir, [exp])`, is
  applied (dispatcher.py:204-206), so result.csv and feature_dictionary.csv are recorded too.
  `fanin_all` applies the same function to the partials of every catalog run plus the custom
  raster run, in the dispatcher's experiment order.

The app allows one runner at a time (flock on /app/data/.run_lock). The driver waits until no
runner process is alive and retries a run whose status.json reports the lock message.

Subcommands (host side; `run` needs only the stdlib and the docker CLI, `collect` and
`compare` need pyarrow and pandas, for example the worktree's .venv):

    python3 tests/golden/tools/web_goldens.py run --stage DIR [--only a,b] [--suffix -rep2]
    .venv/bin/python tests/golden/tools/web_goldens.py collect --stage DIR --out tests/golden/web-0.2.7
    .venv/bin/python tests/golden/tools/web_goldens.py compare --a DIR --b DIR --out FILE
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN_ROOT = HERE.parent
sys.path.insert(0, str(GOLDEN_ROOT))

CONTAINER = os.environ.get("GOLDEN_CONTAINER", "spacescans-web-backend-1")
PY = "/opt/conda/bin/python"
TASKS = "/app/data/tasks"
TASK_PREFIX = "tier2-golden-"
UID = 9997                       # throwaway library owner for these goldens, never a real account
LIBRARY = f"/app/data/custom_exposomes/user-{UID}"
LOCK_MSG = "another task acquired the run lock first"
DEFAULT_COHORT = Path("/Users/xai/Desktop/spacescans-all/spacescans-paper/results/data/cohorts/synth_1k.csv")
BUFFER = {"shape": "circle", "size": 270, "raster_res_m": 25}
BASE_CONFIG = {"experiment": "auto", "buffer": BUFFER, "version": 1, "input_file": "input.csv"}
LARGE_BYTES = 20 * 1024 * 1024   # intermediate files at or above this are described, not copied

# The registration scripts are custom_equivalence.py's, with the owner UID changed to 9997.
REGISTER_RASTER = r'''
import json, sys
sys.path.insert(0, "/app")
from pathlib import Path
from app import custom_exposomes as lib
raster = Path(sys.argv[1])
m = lib.create_raster(%(uid)d, rasters=[(raster.name, raster.read_bytes(), None)],
                      name="Noise via upload (paper RQ1)",
                      description="The catalog's existing-conditions noise raster, uploaded as a user dataset.",
                      value_col="l50dba_exi", value_label="Noise, existing conditions", value_unit="dBA")
print(json.dumps(m))
''' % {"uid": UID}

REGISTER_TABLE = r'''
import io, json, sys
sys.path.insert(0, "/app")
from app import custom_exposomes as lib
from spacescans.io.readers import read_table
# the catalog's reader and its year filter (configs c4/bg_wi_demo.yaml: key wi, year 2016)
wi = read_table(sys.argv[1], key="wi")
wi = wi.loc[wi["year"] == 2016, ["GEOID10", "NatWalkInd"]]
buf = io.StringIO()
wi.to_csv(buf, index=False)                 # float repr: every value round-trips exactly
m = lib.create(%(uid)d, content=buf.getvalue().encode(), name="Walkability via upload (paper RQ1)",
               description="The catalog's 2016 National Walkability Index by 2010 block group, uploaded as a user dataset.",
               boundary="BG", key_col="GEOID10", value_cols=["NatWalkInd"],
               value_labels={"NatWalkInd": "National Walkability Index"},
               uploaded_filename="walkability_bg2010_2016.csv")
print(json.dumps(m))
''' % {"uid": UID}

CUSTOM_SOURCES = {
    "raster": ("/project/Noise/C3/CONUS_L50dBA_sumDay_exi.tif", REGISTER_RASTER),
    "table": ("/project/Walkability/C4/epawalkind_nationwide_2016_2021.Rda", REGISTER_TABLE),
}

FAN_IN = r'''
import json, sys
sys.path.insert(0, "/app")
from pathlib import Path
from app.experiments import _merge
task = Path(sys.argv[1])
exps = json.loads(sys.argv[2])
_merge.fan_in(task, exps)
print(json.dumps({"fan_in": exps}))
'''

DISPATCH_PLAN = r'''
import json, sys
sys.path.insert(0, "/app")
from pathlib import Path
from app import variable_registry
plan = variable_registry.variables_by_experiment(json.loads(sys.argv[2]), task_dir=Path(sys.argv[1]))
print(json.dumps({k: list(v) for k, v in plan.items()}))
'''

DESCRIBE = r'''
import hashlib, json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
out = []
for p in sorted(root.rglob("*")):
    if not p.is_file():
        continue
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    rec = {"path": str(p.relative_to(root)), "bytes": p.stat().st_size, "sha256": h.hexdigest()}
    if p.suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
            md = pq.ParquetFile(p)
            rec["rows"] = md.metadata.num_rows
            rec["columns"] = [f"{x.name}:{x.type}" for x in md.schema_arrow]
        except Exception as exc:
            rec["parquet_error"] = repr(exc)
    out.append(rec)
print(json.dumps(out))
'''


@dataclass(frozen=True)
class Case:
    name: str
    experiment: str
    variables: tuple[str, ...] = ()
    custom: str | None = None          # "raster" | "table" for the upload path
    cache_from: str | None = None      # reuse another case's private C3 cache (warm run)
    note: str = ""


# Run order: fast cases first so a driver problem shows up early; ACAG (~14 min) last.
CASES: tuple[Case, ...] = (
    Case("noise", "noise", ("noise",)),
    Case("temis", "temis", ("temis",)),
    Case("vnl", "vnl", ("vnl",)),
    Case("nhd_bluespace", "nhd_bluespace", ("nhd_bluespace",)),
    Case("tiger_proximity", "tiger_proximity", ("tiger_proximity",)),
    Case("fara_tract", "fara_tract", ("fara_tract",)),
    Case("zcta5_cbp", "zcta5_cbp", ("cbp_zcta5",)),
    Case("bg_ndi_wi", "bg_ndi_wi", ("ndi", "walkability")),
    Case("bg_ndi_wi_rev", "bg_ndi_wi", ("walkability", "ndi"), cache_from="bg_ndi_wi",
         note="request order walkability,ndi (as runtime.py passes it); warm: reuses bg_ndi_wi's "
              "private C3 cache, so both C3 steps are cache hits and render no YAML"),
    Case("faqsd", "faqsd", ("faqsd",)),
    Case("custom_raster", "custom", custom="raster",
         note="the catalog noise GeoTIFF registered with app.custom_exposomes.create_raster"),
    Case("custom_bg_csv", "custom", custom="table",
         note="walkability 2016 by 2010 BG written to CSV and registered with app.custom_exposomes.create"),
    Case("acag", "acag", ("acag",)),
)
CASE_BY_NAME = {c.name: c for c in CASES}
# fanin_all: the catalog runs whose partials make up one all-catalog task, plus the custom raster.
FANIN_ALL_SOURCES = ("bg_ndi_wi", "zcta5_cbp", "tiger_proximity", "nhd_bluespace", "noise", "vnl",
                     "temis", "fara_tract", "acag", "faqsd", "custom_raster")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def eastern(iso_utc: str) -> str:
    """Render a UTC ISO timestamp in US Eastern for the human-facing records."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromisoformat(iso_utc).astimezone(ZoneInfo("America/New_York")).isoformat(
            timespec="seconds")
    except Exception:            # pragma: no cover - zoneinfo data missing
        return iso_utc


def dexec(cmd: str, *, input_bytes: bytes | None = None, env: dict[str, str] | None = None,
          check: bool = True) -> subprocess.CompletedProcess:
    args = ["docker", "exec", "-i"]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    args += [CONTAINER, "sh", "-c", cmd]
    r = subprocess.run(args, input=input_bytes, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"docker exec failed ({r.returncode}): {cmd}\n"
                           f"{r.stderr.decode(errors='replace')[-3000:]}")
    return r


def dpy(script: str, *args: str) -> str:
    quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in args)
    r = dexec(f"cd /app && {PY} - {quoted}", input_bytes=script.encode())
    return r.stdout.decode()


def runner_alive() -> bool:
    out = dexec("for p in /proc/[0-9]*; do tr '\\0' ' ' < $p/cmdline 2>/dev/null; echo; done "
                "| grep -E 'app\\.experiments|app\\.dispatcher|spacescans run' | grep -v grep || true")
    return bool(out.stdout.strip())


def wait_for_idle(poll_s: float = 10.0) -> float:
    waited = 0.0
    while runner_alive():
        time.sleep(poll_s)
        waited += poll_s
    return waited


def task_path(name: str, suffix: str) -> str:
    return f"{TASKS}/{TASK_PREFIX}{name}{suffix}"


def make_task(task: str, cohort: bytes, cfg: dict) -> None:
    dexec(f"rm -rf {task} && mkdir -p {task}/output")
    dexec(f"cat > {task}/input.csv", input_bytes=cohort)
    dexec(f"cat > {task}/config.json", input_bytes=json.dumps(cfg).encode())


def register_custom(kind: str) -> dict:
    source, script = CUSTOM_SOURCES[kind]
    out = dpy(script, source)
    return json.loads(out.strip().splitlines()[-1])


def pull_dir(remote: str, dest: Path) -> None:
    """Copy a container directory into dest (replacing it) through a tar stream."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    r = subprocess.run(["docker", "exec", CONTAINER, "tar", "-C", remote, "-cf", "-", "."],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"tar of {remote} failed: {r.stderr.decode(errors='replace')[-2000:]}")
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
        tf.extractall(dest, filter="data")


def describe_remote(remote: str) -> list[dict]:
    return json.loads(dpy(DESCRIBE, remote))


def log_span(task_local: Path) -> dict:
    """First and last logs.jsonl timestamps and the per-step spawn/exit lines."""
    p = task_local / "logs.jsonl"
    if not p.exists():
        return {}
    first = last = None
    steps: dict[str, dict] = {}
    for line in p.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = rec.get("ts")
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts, timezone.utc).isoformat()
        if ts:
            first = first or ts
            last = ts
        msg = str(rec.get("msg", ""))
        if rec.get("source") == "runner" and msg.startswith("spawning "):
            steps.setdefault(msg.split(":")[0][len("spawning "):], {})["spawn_ts"] = ts
        elif rec.get("source") == "runner" and msg.startswith("step ") and " exit code " in msg:
            name = msg[len("step "):msg.index(" exit code ")]
            steps.setdefault(name, {}).update(exit_ts=ts, rc=int(msg.rsplit(" ", 1)[-1]))
        elif "cache hit:" in msg:
            steps.setdefault(str(rec.get("source")), {})["cache_hit"] = msg
    for s in steps.values():
        if "spawn_ts" in s and "exit_ts" in s:
            s["seconds"] = round((datetime.fromisoformat(s["exit_ts"])
                                  - datetime.fromisoformat(s["spawn_ts"])).total_seconds(), 3)
    return {"first_ts": first, "last_ts": last, "steps": steps}


def run_case(case: Case, cohort: bytes, stage: Path, suffix: str) -> dict:
    task = task_path(case.name, suffix)
    cache_owner = case.cache_from or case.name
    cache_dir = f"{task_path(cache_owner, suffix)}/golden_c3_cache"
    cfg = dict(BASE_CONFIG)
    manifest = None
    if case.custom:
        manifest = register_custom(case.custom)
        key = manifest["variable_key"]
        variables = (key,)
        cfg.update({"variables": [key], "custom_variables": {key: manifest}})
    else:
        variables = case.variables
        cfg["variables"] = list(variables)
    keys = ",".join(variables)
    cmd = f"cd /app && {PY} -m app.experiments.{case.experiment} run {task} --variables {keys}"
    rec: dict = {"name": case.name, "experiment": case.experiment, "variables": list(variables),
                 "task_dir": task, "command": cmd, "env_overrides": {"C3_CACHE_DIR": cache_dir},
                 "cache": ("warm: reuses " + cache_owner + "'s private cache") if case.cache_from
                 else "cold: private empty cache", "note": case.note, "attempts": []}
    for attempt in range(1, 241):
        waited = wait_for_idle()
        make_task(task, cohort, cfg)
        start = utc_now()
        t0 = time.monotonic()
        r = dexec(cmd, env={"C3_CACHE_DIR": cache_dir}, check=False)
        wall = time.monotonic() - t0
        status = dexec(f"cat {task}/status.json 2>/dev/null || true").stdout.decode()
        rec["attempts"].append({"attempt": attempt, "waited_s": waited, "started_utc": start,
                                "rc": r.returncode, "wall_s": round(wall, 3),
                                "lock_refused": LOCK_MSG in status})
        if LOCK_MSG in status:
            time.sleep(15)
            continue
        break
    end = utc_now()
    rec.update(rc=r.returncode, wall_s=round(wall, 3), started_utc=start, ended_utc=end,
               started_eastern=eastern(start), ended_eastern=eastern(end))
    result = f"{task}/output/result_{case.experiment}.csv"
    rec["result_exists"] = dexec(f"test -f {result}", check=False).returncode == 0
    rec["success"] = r.returncode == 0 and rec["result_exists"]
    if rec["success"]:
        rec["fan_in"] = json.loads(dpy(FAN_IN, task, json.dumps([case.experiment])).strip().splitlines()[-1])
    local = stage / case.name
    if local.exists():
        shutil.rmtree(local)
    local.mkdir(parents=True)
    (local / "runner.stdout.txt").write_bytes(r.stdout)
    (local / "runner.stderr.txt").write_bytes(r.stderr)
    if manifest is not None:
        (local / "custom_definition.json").write_text(json.dumps(manifest, indent=2) + "\n")
        lib_dir = f"{LIBRARY}/{manifest['dataset_id']}"
        rec["custom_library_files"] = describe_remote(lib_dir)
        # keep the uploaded table itself when it is small (the raster is the catalog's own
        # 432 MB GeoTIFF, recorded by sha256 only)
        for f in rec["custom_library_files"]:
            if f["path"] == "values.csv" and f["bytes"] < LARGE_BYTES:
                (local / "upload").mkdir(exist_ok=True)
                (local / "upload" / "values.csv").write_bytes(
                    dexec(f"cat {lib_dir}/values.csv").stdout)
    rec["remote_files"] = describe_remote(task)
    pull_dir(task, local / "task")
    # keep the big files out of the local stage too: they are described in remote_files
    for f in rec["remote_files"]:
        if f["bytes"] >= LARGE_BYTES:
            (local / "task" / f["path"]).unlink(missing_ok=True)
    rec["log_span"] = log_span(local / "task")
    (local / "run.json").write_text(json.dumps(rec, indent=2) + "\n")
    print(f"[{eastern(end)}] {case.name}: rc={r.returncode} success={rec['success']} "
          f"wall={wall:.1f}s attempts={len(rec['attempts'])}", flush=True)
    return rec


def run_fanin_all(cohort: bytes, stage: Path, suffix: str) -> dict:
    """One all-catalog task assembled from the per-run partials, fanned in the dispatcher's way."""
    task = task_path("fanin_all", suffix)
    sources = []
    for name in FANIN_ALL_SOURCES:
        run = stage / name / "run.json"
        if run.exists() and json.loads(run.read_text()).get("success"):
            sources.append(name)
    custom_cfg = {}
    variables: list[str] = []
    for name in sources:
        case = CASE_BY_NAME[name]
        if case.custom:
            definition = json.loads((stage / name / "custom_definition.json").read_text())
            custom_cfg[definition["variable_key"]] = definition
            variables.append(definition["variable_key"])
        else:
            variables.extend(case.variables)
    cfg = {**BASE_CONFIG, "variables": variables}
    if custom_cfg:
        cfg["custom_variables"] = custom_cfg
    make_task(task, cohort, cfg)
    for name in sources:
        case = CASE_BY_NAME[name]
        dexec(f"cp {task_path(name, suffix)}/output/result_{case.experiment}.csv {task}/output/")
    plan = json.loads(dpy(DISPATCH_PLAN, task, json.dumps(variables)).strip().splitlines()[-1])
    exps = list(plan)
    fan = json.loads(dpy(FAN_IN, task, json.dumps(exps)).strip().splitlines()[-1])
    rec = {"name": "fanin_all", "task_dir": task, "sources": sources, "variables": variables,
           "dispatch_plan": plan, "fan_in": fan, "success": True,
           "note": "not a runner: app.experiments._merge.fan_in over the partials of the runs listed "
                   "in sources, in the order app.variable_registry.variables_by_experiment gives "
                   "(the dispatcher's order)"}
    local = stage / "fanin_all"
    rec["remote_files"] = describe_remote(task)
    pull_dir(task, local / "task")
    (local / "run.json").write_text(json.dumps(rec, indent=2) + "\n")
    print(f"[{eastern(utc_now())}] fanin_all: {exps}", flush=True)
    return rec


def cmd_run(args: argparse.Namespace) -> int:
    stage = Path(args.stage).resolve()
    stage.mkdir(parents=True, exist_ok=True)
    cohort_path = Path(args.cohort)
    cohort = cohort_path.read_bytes()
    names = [n for n in (args.only.split(",") if args.only else [c.name for c in CASES]) if n]
    unknown = [n for n in names if n not in CASE_BY_NAME and n != "fanin_all"]
    if unknown:
        sys.exit(f"unknown case(s): {unknown}")
    meta = {"started_utc": utc_now(), "container": CONTAINER, "suffix": args.suffix,
            "cohort": str(cohort_path), "cohort_sha256": hashlib.sha256(cohort).hexdigest(),
            "cases": names}
    if any(CASE_BY_NAME[n].custom for n in names if n in CASE_BY_NAME):
        dexec(f"rm -rf {LIBRARY}")       # this driver's own throwaway library only
        meta["custom_library_cleared"] = LIBRARY
    (stage / "stage.json").write_text(json.dumps(meta, indent=2) + "\n")
    failures = []
    for n in names:
        if n == "fanin_all":
            continue
        try:
            rec = run_case(CASE_BY_NAME[n], cohort, stage, args.suffix)
            if not rec["success"]:
                failures.append(n)
        except Exception as exc:          # record and continue with the next case
            failures.append(n)
            (stage / n).mkdir(parents=True, exist_ok=True)
            (stage / n / "driver_error.txt").write_text(repr(exc) + "\n")
            print(f"[{eastern(utc_now())}] {n}: DRIVER ERROR {exc!r}", flush=True)
    if "fanin_all" in names or not args.only:
        try:
            run_fanin_all(cohort, stage, args.suffix)
        except Exception as exc:
            failures.append("fanin_all")
            (stage / "fanin_all").mkdir(parents=True, exist_ok=True)
            (stage / "fanin_all" / "driver_error.txt").write_text(repr(exc) + "\n")
            print(f"[{eastern(utc_now())}] fanin_all: DRIVER ERROR {exc!r}", flush=True)
    meta["ended_utc"] = utc_now()
    meta["failures"] = failures
    (stage / "stage.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps({"failures": failures}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="run the cases in the container and stage their task dirs")
    p.add_argument("--stage", required=True)
    p.add_argument("--only", default="", help="comma-separated case names (default: all + fanin_all)")
    p.add_argument("--suffix", default="", help="task-dir suffix, e.g. -rep2 for a repeat pass")
    p.add_argument("--cohort", default=str(DEFAULT_COHORT))
    p.set_defaults(func=cmd_run)

    try:                                             # collect/compare need pyarrow and pandas
        from golden_collect import add_subcommands
    except ImportError as exc:
        add_subcommands = None
        missing = exc
    if add_subcommands is not None:
        add_subcommands(sub)
    args = ap.parse_args(argv)
    if args.func is None:                            # pragma: no cover
        sys.exit(f"collect/compare unavailable: {missing!r}")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
