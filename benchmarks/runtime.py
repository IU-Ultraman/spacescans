#!/usr/bin/env python3
"""Measure SPACESCANS wall-clock time, peak memory and time by step, per source and cohort size.

Runs the linkage inside the backend container of the spacescans-web application (docker exec)
and writes one row per run to --out (default benchmarks/results/runtime.csv) and the hardware
of the machine to <stem>_machine.csv beside it. results/runtime_server.csv holds the runs made
on a server node with four worker processes, one initial run and one cached run per source.

Design
  * Every source (10 experiment modules covering the 11 catalog sources) is run on a synthetic
    cohort (cohorts/synth_<size>.csv, written by synth_cohort.py) REPEATS times in two modes:
      cold   the spatial-weight cache cannot be hit: each cold run uses a copy of the cohort
             whose participant identifiers carry a prefix unique to the (source, repeat) pair
             (c<r>s<k>_P...), so the bytes differ while the locations and dates do not.
      warm   the same task directory is run again with its output emptied, so the spatial
             step is served from the cache the cold run wrote and only aggregation runs.
  * Wall-clock is measured around the runner process from a wrapper inside the container; peak
    memory is the largest resident set of any process the runner waited for (RUSAGE_CHILDREN
    ru_maxrss). Time by step is read from the runner's logs.jsonl: c3_* steps are the spatial
    computation, c4_* steps the exposure-value aggregation, and everything before the first
    step is preparation.
  * Runs are sequential, one at a time; run this with nothing else on the machine. The script
    refuses to start while another runner is alive in the container.
  * --workers sets the number of worker processes for the spatial computation
    (SPACESCANS_PIPELINE_WORKERS); peak_rss_gb stays the largest single process, which with
    several workers is not the total memory.

    python3 benchmarks/runtime.py --sizes 100k --repeats 1 --workers 4 \
        --out benchmarks/results/runtime_server.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
COHORTS = HERE / "cohorts"
OUT = HERE / "results"
# The backend container of the web app (docker compose project "spacescans-web" by default);
# set SPACESCANS_CONTAINER or pass --container when the compose project has another name.
CONTAINER = os.environ.get("SPACESCANS_CONTAINER", "spacescans-web-backend-1")
PY = "/opt/conda/bin/python"

# experiment module -> variable keys it serves (the dispatcher's grouping)
EXPERIMENTS = {
    "acag": ["acag"], "faqsd": ["faqsd"], "nhd_bluespace": ["nhd_bluespace"], "noise": ["noise"],
    "temis": ["temis"], "vnl": ["vnl"], "bg_ndi_wi": ["walkability", "ndi"],
    "fara_tract": ["fara_tract"], "tiger_proximity": ["tiger_proximity"], "zcta5_cbp": ["cbp_zcta5"],
    # the two block-group sources timed one at a time (each has its own row in the results);
    # they run through the bg_ndi_wi module, and their own keys give their cohort copies their own
    # prefix, so neither can hit the other's cached weights
    "walkability": ["walkability"], "ndi": ["ndi"],
}
MODULE = {"walkability": "bg_ndi_wi", "ndi": "bg_ndi_wi"}   # experiment key -> app.experiments module

WRAPPER = r'''
import json, os, resource, subprocess, sys, time
task, exp, vars_, workers = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
env = dict(os.environ, SPACESCANS_PIPELINE_WORKERS=workers)
t0 = time.time()
rc = subprocess.call(["%(py)s", "-m", "app.experiments." + exp, "run", task, "--variables", vars_],
                     cwd="/app", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
wall = time.time() - t0
ru = resource.getrusage(resource.RUSAGE_CHILDREN)
print(json.dumps({"rc": rc, "wall_s": wall, "maxrss_kb": ru.ru_maxrss, "cpu_s": ru.ru_utime + ru.ru_stime}))
''' % {"py": PY}


def dexec(cmd: str, input_text: str | None = None, check=True) -> str:
    r = subprocess.run(["docker", "exec", "-i", CONTAINER, "sh", "-c", cmd],
                       input=input_text, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"docker exec failed ({r.returncode}): {cmd}\n{r.stderr[-2000:]}")
    return r.stdout


def other_runners_alive() -> bool:
    # no `ps` in the container: scan /proc for runner or pipeline command lines
    out = dexec("for p in /proc/[0-9]*; do tr '\\0' ' ' < $p/cmdline 2>/dev/null; echo; done "
                "| grep -E 'app\\.experiments|spacescans run' | grep -v grep || true")
    return bool(out.strip())


def make_task(name: str, cohort_csv: Path, prefix: str, variables: list[str]) -> str:
    task = f"/app/data/tasks/{name}"
    dexec(f"rm -rf {task} && mkdir -p {task}/output")
    # cohort copy with prefixed pids, streamed into the container
    with cohort_csv.open() as f:
        rows = f.read().splitlines()
    header, body = rows[0], rows[1:]
    text = "\n".join([header] + [prefix + r for r in body]) + "\n"
    dexec(f"cat > {task}/input.csv", input_text=text)
    cfg = {"experiment": "auto", "buffer": {"shape": "circle", "size": 270, "raster_res_m": 25},
           "variables": variables, "version": 1, "input_file": "input.csv"}
    dexec(f"cat > {task}/config.json", input_text=json.dumps(cfg))
    return task


LOCK_MSG = "another task acquired the run lock first"


def run_once(task: str, exp: str, variables: list[str], workers: int = 1) -> dict:
    """Run the runner once. The application serialises runners with a global lock
    (/app/data/.run_lock); a run refused by the lock exits 1 immediately with a status
    message, which is not a measurement — wait for the other runner and try again."""
    dexec(f"cat > /tmp/spacescans_runtime_wrapper.py", input_text=WRAPPER)
    for attempt in range(60):
        while other_runners_alive():
            time.sleep(10)
        dexec(f"rm -f {task}/status.json {task}/logs.jsonl")
        out = dexec(f"cd /app && {PY} /tmp/spacescans_runtime_wrapper.py {task} {MODULE.get(exp, exp)} "
                    f"{','.join(variables)} {workers}")
        res = json.loads(out.strip().splitlines()[-1])
        status = dexec(f"cat {task}/status.json 2>/dev/null || true")
        if res["rc"] != 0 and LOCK_MSG in status:
            time.sleep(15)
            continue
        res.update(stage_times(task))
        return res
    raise RuntimeError(f"{task}: could not acquire the run lock after 60 attempts")


def _log_time(ts) -> datetime | None:
    """A logs.jsonl timestamp as an aware UTC datetime: ISO text (the runner's records) or epoch
    seconds (some merge-step records, e.g. the low-linkage warning); None for anything else."""
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return datetime.fromtimestamp(ts, timezone.utc)
    if isinstance(ts, str):
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return None


def stage_times(task: str) -> dict:
    log = dexec(f"cat {task}/logs.jsonl 2>/dev/null || true")
    spawns, ends = {}, {}
    first_ts = None
    cache_hit = False
    for line in log.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _log_time(e.get("ts"))
        if ts is None:
            continue
        first_ts = first_ts or ts
        msg = e.get("msg", "")
        if e.get("source") == "runner" and msg.startswith("spawning "):
            spawns[msg.split()[1].rstrip(":")] = ts
        elif e.get("source") == "runner" and msg.startswith("step ") and "exit code" in msg:
            ends[msg.split()[1]] = ts
        if "cache hit" in msg:
            cache_hit = True
    c3 = sum((ends[s] - spawns[s]).total_seconds() for s in spawns if s.startswith("c3") and s in ends)
    c4 = sum((ends[s] - spawns[s]).total_seconds() for s in spawns if s.startswith("c4") and s in ends)
    prep = (min(spawns.values()) - first_ts).total_seconds() if spawns and first_ts else None
    return {"stage_prep_s": prep, "stage_c3_s": c3, "stage_c4_s": c4, "c3_cache_hit": cache_hit,
            "n_steps": len(ends)}


DOCKER_SETTINGS = Path.home() / "Library/Group Containers/group.com.docker/settings-store.json"


def docker_vm_memory_gib() -> float | None:
    """Memory allocated to Docker Desktop's Linux VM (Settings > Resources), in GiB."""
    try:
        return json.loads(DOCKER_SETTINGS.read_text())["MemoryMiB"] / 1024
    except (OSError, KeyError, ValueError, TypeError):
        return None


def _out(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def host_cpu_and_memory() -> tuple[str, str]:
    """(CPU model, RAM in GiB) of the machine running this script: sysctl on macOS, /proc or
    lscpu on Linux; empty strings when neither answers."""
    if sys.platform == "darwin":
        mem = _out(["sysctl", "-n", "hw.memsize"])
        return (_out(["sysctl", "-n", "machdep.cpu.brand_string"]),
                f"{int(mem) / 2**30:.0f}" if mem.isdigit() else "")
    cpu = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    if not cpu:   # arm64 /proc/cpuinfo has no model name
        cpu = next((l.split(":", 1)[1].strip() for l in _out(["lscpu"]).splitlines()
                    if l.startswith("Model name:")), "")
    mem = ""
    try:
        kb = next(l.split()[1] for l in Path("/proc/meminfo").read_text().splitlines()
                  if l.startswith("MemTotal:"))
        mem = f"{int(kb) / 2**20:.0f}"
    except (OSError, StopIteration, ValueError):
        pass
    return cpu, mem


def container_memory_limit_gib() -> float | None:
    """The backend container's cgroup memory limit in GiB; None when it has none ("max")."""
    raw = dexec("cat /sys/fs/cgroup/memory.max 2>/dev/null || true", check=False).strip()
    return int(raw) / 2**30 if raw.isdigit() else None


def machine_row(measured_utc: str | None = None) -> list[list[str]]:
    host_cpu, host_mem = host_cpu_and_memory()
    ncpu = dexec("nproc").strip()
    mem = dexec("grep MemTotal /proc/meminfo").split()[1]
    kern = dexec("uname -sr").strip()
    ver = dexec(f"{PY} -c 'import spacescans; print(spacescans.__version__)'").strip()
    # container_memory_gb is the allocation (what Docker Desktop shows); the kernel keeps a
    # few hundred MiB of it, so MemTotal inside the VM is smaller (recorded as *_available_gb).
    # On Linux there is no VM: the limit is the container's cgroup limit if one is set, else
    # the host's RAM (MemTotal inside the container).
    available = int(mem) / 2**20
    allocated = docker_vm_memory_gib() or container_memory_limit_gib() or available
    return [["host_cpu", host_cpu], ["host_memory_gb", host_mem],
            ["container_cpus", ncpu], ["container_memory_gb", f"{allocated:.0f}"],
            ["container_memory_available_gb", f"{available:.1f}"],
            ["container_kernel", kern], ["pipeline_version", ver],
            ["measured_utc", measured_utc or datetime.now(timezone.utc).isoformat(timespec="seconds")]]


def machine_path(out: Path) -> Path:
    """The hardware of a run goes to <stem>_machine.csv beside the rows file."""
    return out.with_name(f"{out.stem}_machine.csv")


def main(argv=None) -> int:
    global CONTAINER
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sizes", default="1k,10k,100k")
    ap.add_argument("--container", default=CONTAINER,
                    help="backend container to run in (default: $SPACESCANS_CONTAINER or spacescans-web-backend-1)")
    ap.add_argument("--experiments", default=",".join(EXPERIMENTS))
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--first-repeat", type=int, default=1,
                    help="number of the first repeat (default 1). The repeat is part of the cohort "
                         "prefix, so a second pass with --first-repeat 2 times cold runs again "
                         "after a first pass cached its weights")
    ap.add_argument("--cold-only", action="store_true",
                    help="time only the cold run of each repeat, not the warm run reusing its weights")
    ap.add_argument("--out", type=Path, default=OUT / "runtime.csv",
                    help="rows file (default benchmarks/results/runtime.csv); the hardware goes to "
                         "<stem>_machine.csv beside it")
    ap.add_argument("--force", action="store_true", help="run even if another runner is alive")
    ap.add_argument("--workers", type=int, default=1,
                    help="worker processes per pipeline step (SPACESCANS_PIPELINE_WORKERS); "
                         "default 1, one process per step")
    ap.add_argument("--replace", action="store_true",
                    help="drop existing rows for the (size, experiment, workers) triples about to be timed, "
                         "from repeat --first-repeat on")
    ap.add_argument("--machine-only", action="store_true",
                    help="rewrite the machine file of --out for the running container, timing nothing; "
                         "measured_utc is kept from the existing file")
    args = ap.parse_args(argv)
    CONTAINER = args.container
    if args.workers < 1:
        sys.exit("--workers must be at least 1")
    if args.first_repeat < 1:
        sys.exit("--first-repeat must be at least 1")

    if args.machine_only:
        machine = machine_path(args.out)
        kept = {r["metric"]: r["value"] for r in csv.DictReader(machine.open())} if machine.exists() else {}
        rows = machine_row(kept.get("measured_utc"))
        with machine.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["metric", "value"])
            w.writerows(rows)
        print(f"wrote {machine}")
        return 0

    if other_runners_alive() and not args.force:
        sys.exit("another pipeline runner is alive in the container; benchmarks must run alone")

    if args.replace and args.out.exists():
        pairs = {(sz, ex) for sz in args.sizes.split(",") for ex in args.experiments.split(",")}
        with args.out.open() as f:
            # rows written before the workers column have none: they are single-process runs
            kept = [r for r in csv.DictReader(f)
                    if (r["cohort_size"], r["experiment"]) not in pairs
                    or int(r.get("workers") or 1) != args.workers
                    or int(r["repeat"]) < args.first_repeat]
            header = None
        with args.out.open() as f:
            header = next(csv.reader(f))
        with args.out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader(); w.writerows(kept)
        print(f"--replace: removed the rows of {sorted(pairs)} with {args.workers} worker(s) from repeat "
              f"{args.first_repeat} on; kept {len(kept)}")

    fieldnames = ["cohort_size", "n_episodes", "experiment", "variables", "mode", "repeat", "wall_s",
                  "peak_rss_gb", "stage_prep_s", "stage_c3_s", "stage_c4_s", "c3_cache_hit", "n_steps",
                  "rc", "started_utc", "cpu_s", "workers"]
    # cpu_s (user + system CPU seconds of the runner and its steps) was added on 2026-10-14 and
    # workers on 2026-10-25; earlier rows keep them empty (workers: a single process)
    if args.out.exists():
        with args.out.open() as f:
            old = list(csv.DictReader(f))
            header = list(old[0].keys()) if old else []
        if header and header != fieldnames:
            with args.out.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader(); w.writerows({k: r.get(k, "") for k in fieldnames} for r in old)
    exists = args.out.exists()
    with args.out.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            w.writeheader()
        for size in args.sizes.split(","):
            cohort = COHORTS / f"synth_{size}.csv"
            n_eps = sum(1 for _ in cohort.open()) - 1
            for exp in args.experiments.split(","):
                variables = EXPERIMENTS[exp]
                last = args.first_repeat + args.repeats - 1
                for r in range(args.first_repeat, last + 1):
                    k = list(EXPERIMENTS).index(exp)
                    w_tag = "" if args.workers == 1 else f"w{args.workers}"
                    name = f"bench-RT-{size}-{exp}-r{r}" + (f"-{w_tag}" if w_tag else "")
                    task = make_task(name, cohort, f"c{r}s{k}{w_tag}_", variables)
                    for mode in ("cold",) if args.cold_only else ("cold", "warm"):
                        if mode == "warm":
                            dexec(f"rm -rf {task}/output {task}/logs.jsonl {task}/status.json && mkdir -p {task}/output")
                        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        res = run_once(task, exp, variables, args.workers)
                        row = {"cohort_size": size, "n_episodes": n_eps, "experiment": exp,
                               "variables": ";".join(variables), "mode": mode, "repeat": r,
                               "wall_s": round(res["wall_s"], 2),
                               "peak_rss_gb": round(res["maxrss_kb"] / 2**20, 3),
                               "stage_prep_s": None if res["stage_prep_s"] is None else round(res["stage_prep_s"], 2),
                               "stage_c3_s": round(res["stage_c3_s"], 2), "stage_c4_s": round(res["stage_c4_s"], 2),
                               "c3_cache_hit": res["c3_cache_hit"], "n_steps": res["n_steps"],
                               "rc": res["rc"], "started_utc": started, "cpu_s": round(res["cpu_s"], 2),
                               "workers": args.workers}
                        w.writerow(row)
                        f.flush()
                        print(f"{size} {exp} r{r} {mode} w{args.workers}: wall={row['wall_s']}s cpu={row['cpu_s']}s rss={row['peak_rss_gb']}GB "
                              f"c3={row['stage_c3_s']}s c4={row['stage_c4_s']}s hit={row['c3_cache_hit']} rc={row['rc']}")
                    # free disk: keep only the last repeat's outputs
                    if r < last:
                        dexec(f"rm -rf {task}/output")
    rows = machine_row()
    with machine_path(args.out).open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        w.writerows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
