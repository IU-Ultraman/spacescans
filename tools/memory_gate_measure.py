#!/opt/conda/bin/python
"""Container side of ``tools/memory_gate.py``: run one case and record wall time and peak RSS.

Runs inside an ephemeral container of the frozen web image (``/app`` is the 0.2.7 web
backend, ``/project`` the read-only data root, ``/app/data`` a private scratch mount) in
one of two modes:

``old``
    the 0.2.7 web runner, ``python -m app.experiments.<exp> run <task_dir> --variables ...``
    with ``cwd=/app`` (the paper's ``runtime.py`` invocation, same wrapper position);
``new``
    ``spacescans.orchestrate.link`` from the package mounted at ``/src`` (``PYTHONPATH``),
    run in a child process (``inner``) so that the same wrapper measures it.

The wrapper records, around the child: wall time, ``RUSAGE_CHILDREN`` ``ru_maxrss`` (the
largest resident set of any process it waited for: the runner or orchestrator, and the
pipeline steps they waited for), and a sampler's view of ``/proc/<pid>/status`` ``VmHWM``
for every pipeline step process (keyed by the step's config name), for the runner or
orchestrator process itself, and ``MemAvailable`` at its lowest. ``inner`` adds what
``link`` reports: every step's ``maxrss_bytes`` from the executor (``wait4``), the
orchestrator's own ``RUSAGE_SELF``, failed sources and their error entries (kind, step,
exit code, hint; the raw log tail is counted, not copied).

No cohort values are read or written here beyond the files the pipeline itself writes
into the scratch mount; the record holds counts, step names, exit codes, bytes and seconds.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

PROC = Path("/proc")
PY = "/opt/conda/bin/python"
WEB_PREFIX = "/opt/conda/bin/python /opt/conda/bin/spacescans"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# /proc sampler
# ---------------------------------------------------------------------------

def _read_status(pid: str) -> dict[str, int] | None:
    try:
        text = (PROC / pid / "status").read_text()
    except OSError:
        return None
    out: dict[str, int] = {}
    for line in text.splitlines():
        if line.startswith(("VmHWM:", "VmRSS:")):
            key, _, value = line.partition(":")
            out[key] = int(value.split()[0]) * 1024          # kB -> bytes
    return out


def _cmdline(pid: str) -> list[str]:
    try:
        raw = (PROC / pid / "cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def _key_for(argv: list[str]) -> str | None:
    """Which process this is: a pipeline step (its config stem), the runner, the orchestrator."""
    if not argv:
        return None
    if "run" in argv and any(a.endswith("/spacescans") or a == "spacescans" for a in argv[:4]):
        yamls = [a for a in argv if a.endswith(".yaml")]
        return Path(yamls[-1]).stem if yamls else "<step>"
    joined = " ".join(argv)
    if "app.experiments." in joined:
        return "<runner>"
    if "memory_gate_measure.py" in joined and "inner" in argv:
        return "<orchestrator>"
    return None


class Sampler(threading.Thread):
    """Polls /proc every ``interval`` seconds for the processes of this measurement."""

    def __init__(self, interval: float = 0.25) -> None:
        super().__init__(name="memory-gate-sampler", daemon=True)
        self.interval = interval
        self.peaks: dict[str, dict] = {}
        self.samples = 0
        self.min_mem_available_bytes: int | None = None
        self._halt = threading.Event()
        self._self = str(os.getpid())

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=5.0)

    def run(self) -> None:
        while not self._halt.is_set():
            self.sample()
            self._halt.wait(self.interval)
        self.sample()

    def sample(self) -> None:
        self.samples += 1
        try:
            for line in (PROC / "meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) * 1024
                    self.min_mem_available_bytes = (avail if self.min_mem_available_bytes is None
                                                    else min(self.min_mem_available_bytes, avail))
                    break
        except OSError:
            pass
        try:
            pids = [p.name for p in PROC.iterdir() if p.name.isdigit() and p.name != self._self]
        except OSError:
            return
        for pid in pids:
            key = _key_for(_cmdline(pid))
            if key is None:
                continue
            st = _read_status(pid)
            if not st or "VmHWM" not in st:
                continue
            rec = self.peaks.setdefault(key, {"vmhwm_bytes": 0, "pids": [], "samples": 0})
            rec["vmhwm_bytes"] = max(rec["vmhwm_bytes"], st["VmHWM"])
            rec["samples"] += 1
            if pid not in rec["pids"]:
                rec["pids"].append(pid)

    def report(self) -> dict:
        return {"interval_s": self.interval, "samples": self.samples,
                "min_mem_available_bytes": self.min_mem_available_bytes,
                "peaks": self.peaks}


# ---------------------------------------------------------------------------
# logs.jsonl
# ---------------------------------------------------------------------------

def parse_log(run_dir: Path) -> dict:
    """Per-step spawn/exit spans and cache hits from the runner-authored log lines."""
    path = run_dir / "logs.jsonl"
    steps: dict[str, dict] = {}
    first_ts = None
    hits: list[str] = []
    lines = 0
    if not path.exists():
        return {"steps": steps, "cache_hits": hits, "lines": 0}
    for line in path.read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        lines += 1
        ts = rec.get("ts")
        try:
            t = datetime.fromisoformat(ts) if isinstance(ts, str) else None
        except ValueError:
            t = None
        if t is not None and first_ts is None:
            first_ts = t
        msg = str(rec.get("msg", ""))
        if rec.get("source") == "runner" and msg.startswith("spawning "):
            name = msg.split(":")[0][len("spawning "):]
            steps.setdefault(name, {})["spawn_ts"] = ts
        elif rec.get("source") == "runner" and msg.startswith("step ") and " exit code " in msg:
            name = msg[len("step "):msg.index(" exit code ")]
            try:
                rc: int | None = int(msg.rsplit(" ", 1)[-1])
            except ValueError:
                rc = None
            steps.setdefault(name, {}).update(exit_ts=ts, rc=rc)
        elif "cache hit:" in msg or "reused " in msg:
            hits.append(str(rec.get("source")))
    for s in steps.values():
        if "spawn_ts" in s and "exit_ts" in s:
            try:
                s["seconds"] = round((datetime.fromisoformat(s["exit_ts"])
                                      - datetime.fromisoformat(s["spawn_ts"])).total_seconds(), 3)
            except ValueError:
                pass
    return {"steps": steps, "cache_hits": hits, "lines": lines,
            "first_ts": first_ts.isoformat() if first_ts else None}


def _status_summary(task: Path) -> dict | None:
    """status.json without anything but status, progress and messages."""
    path = task / "status.json"
    if not path.exists():
        return None
    try:
        st = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"unreadable": True}
    out = {k: st.get(k) for k in ("progress", "current_step", "total_steps", "steps")}
    exps = st.get("experiments") or {}
    out["experiments"] = {k: {f: v.get(f) for f in ("status", "progress", "message", "current_step")}
                          for k, v in exps.items() if isinstance(v, dict)}
    return out


def _wrap(cmd: list[str], *, cwd: str, stdout: Path, stderr: Path) -> dict:
    """Run ``cmd`` to completion; wall time, RUSAGE_CHILDREN and the /proc sampler around it."""
    sampler = Sampler()
    started = utc_now()
    with open(stdout, "ab") as so, open(stderr, "ab") as se:
        sampler.start()
        t0 = time.monotonic()
        rc = subprocess.call(cmd, cwd=cwd, stdout=so, stderr=se, stdin=subprocess.DEVNULL)
        wall = time.monotonic() - t0
        sampler.stop()
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {"command": cmd, "cwd": cwd, "rc": rc, "wall_s": round(wall, 3),
            "started_utc": started, "ended_utc": utc_now(),
            "children_maxrss_bytes": int(ru.ru_maxrss) * 1024,       # Linux: kB
            "sampler": sampler.report()}


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------

def mode_old(args: argparse.Namespace) -> dict:
    task = Path(args.task)
    cmd = [PY, "-m", f"app.experiments.{args.experiment}", "run", str(task),
           "--variables", args.variables]
    rec = _wrap(cmd, cwd="/app", stdout=task / "runner.stdout.txt", stderr=task / "runner.stderr.txt")
    rec.update(mode="old", experiment=args.experiment, variables=args.variables.split(","),
               task_dir=str(task), log=parse_log(task), status=_status_summary(task))
    result = task / "output" / f"result_{args.experiment}.csv"
    rec["result_exists"] = result.is_file()
    if rec["result_exists"]:
        with open(result, "rb") as f:
            rec["result_rows"] = max(sum(1 for _ in f) - 1, 0)
    return rec


def mode_new(args: argparse.Namespace) -> dict:
    run_dir = Path(args.run_dir)
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    inner_out = run_dir.parent / "inner.json"
    cmd = [sys.executable, os.path.abspath(__file__), "inner", "--cohort", args.cohort,
           "--variables", args.variables, "--run-dir", args.run_dir, "--cache-dir", args.cache_dir,
           "--prefix", args.prefix, "--out", str(inner_out)]
    if args.custom:
        cmd += ["--custom", args.custom]
    rec = _wrap(cmd, cwd="/app", stdout=run_dir.parent / "orchestrator.stdout.txt",
                stderr=run_dir.parent / "orchestrator.stderr.txt")
    rec.update(mode="new", variables=args.variables.split(","), run_dir=str(run_dir))
    if inner_out.exists():
        rec["link"] = json.loads(inner_out.read_text())
    else:
        rec["link"] = None
    rec["log"] = parse_log(run_dir)
    return rec


def mode_inner(args: argparse.Namespace) -> dict:
    import shlex

    from spacescans.orchestrate import LogWriter, SubprocessExecutor, WeightCache, link

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    custom = json.loads(Path(args.custom).read_text()) if args.custom else None
    executor = SubprocessExecutor(cmd_prefix=shlex.split(args.prefix))
    started = utc_now()
    t0 = time.monotonic()
    result = link(args.cohort, args.variables.split(","), data_dir="/project", run_dir=run_dir,
                  buffer_m=270, raster_res_m=25, custom=custom,
                  cache=WeightCache(Path(args.cache_dir), key_format="legacy-v1"),
                  executor=executor, on_event=LogWriter.for_run_dir(run_dir))
    link_wall = time.monotonic() - t0
    # what the web adapter writes after link: the partial CSVs and the fan-in result
    out_dir = run_dir / "output"
    out_dir.mkdir(exist_ok=True)
    for exp, table in result.partials.items():
        table.to_csv(out_dir / f"result_{exp}.csv", index=False)
    result.exposures.to_csv(out_dir / "result.csv", index=False)
    wall = time.monotonic() - t0
    errors = []
    for e in result.errors:
        entry = {k: v for k, v in e.items() if k != "log_tail"}
        entry["log_tail_lines"] = len(e.get("log_tail") or [])
        errors.append(entry)
    ru_self = resource.getrusage(resource.RUSAGE_SELF)
    ru_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "started_utc": started, "ended_utc": utc_now(),
        "link_wall_s": round(link_wall, 3), "wall_s": round(wall, 3),
        "ok": result.ok, "failed": list(result.failed), "skipped": list(result.skipped),
        "steps": [s.to_dict() for s in result.steps],
        "errors": errors,
        "warnings": [{"code": w.get("code"), "message": w.get("message")} for w in result.warnings],
        "exposure_rows": len(result.exposures),
        "exposure_columns": [str(c) for c in result.exposures.columns],
        "self_maxrss_bytes": int(ru_self.ru_maxrss) * 1024,
        "children_maxrss_bytes": int(ru_children.ru_maxrss) * 1024,
        "provenance": result.provenance,
        "executor_prefix": list(executor.cmd_prefix),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    p_old = sub.add_parser("old")
    p_old.add_argument("--task", required=True)
    p_old.add_argument("--experiment", required=True)
    p_old.add_argument("--variables", required=True)
    p_old.add_argument("--out", required=True)
    for name in ("new", "inner"):
        p = sub.add_parser(name)
        p.add_argument("--cohort", required=True)
        p.add_argument("--variables", required=True)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--cache-dir", required=True)
        p.add_argument("--prefix", default=WEB_PREFIX)
        p.add_argument("--custom", default=None)
        p.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.mode == "old":
        rec = mode_old(args)
    elif args.mode == "new":
        rec = mode_new(args)
    else:
        rec = mode_inner(args)
    rec["python"] = sys.version.split()[0]
    rec["pid"] = os.getpid()
    Path(args.out).write_text(json.dumps(rec, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
