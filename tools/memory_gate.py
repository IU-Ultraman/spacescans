#!/usr/bin/env python3
"""Gate M (design section 6, phase-C contract 4.5): memory and wall time, old web runner against
``orchestrate.link``, per source and cohort size, measured in the frozen web image.

``run`` drives one ephemeral container per (cohort size, case, mode), serially, each under the
host-wide heavy lock, and keeps a ``record.json`` per run under ``--stage``:

* ``old``: the 0.2.7 web runner from the image's own ``/app``
  (``python -m app.experiments.<exp> run <task_dir> --variables ...``), a private task dir
  and C3 cache in a scratch bind mount at ``/app/data`` (so every run is cold);
* ``new``: ``orchestrate.link`` from the package tree mounted read-only at ``/src``
  (``PYTHONPATH=/src``, inherited by the step subprocesses, which the executor starts with
  the web's prefix ``/opt/conda/bin/python /opt/conda/bin/spacescans``), a fresh run dir and
  a fresh legacy-v1 cache in the same kind of scratch mount.

Both modes are measured by the same wrapper (``tools/memory_gate_measure.py``, mounted at
``/tools``): wall time and ``RUSAGE_CHILDREN`` ``ru_maxrss`` around the runner or orchestrator
process, as the paper's ``runtime.py`` measures, plus a ``/proc`` ``VmHWM`` sampler per step
process; ``new`` adds the executor's ``maxrss_bytes`` per step and the orchestrator's own
``RUSAGE_SELF``. Containers are ``--rm``, named ``tier2-c-memory-<n>``, have no network, mount
the data root and the web templates read-only, and get ``--oom-score-adj 500`` so that the
kernel's out-of-memory killer prefers them to the live containers. Before each run a stamp is
touched; afterwards every file under the data root newer than the stamp is listed in the record
(there must be none). Unless ``--keep`` is given, everything with cohort data (the task or run
dir, the cache, the cohort copy) is deleted after each run; logs, status, configs, cache
sidecars and the records stay.

``report`` turns the records into ``docs/memory-<date>.csv`` (every run and step),
``docs/memory.md`` (the table variable x cohort size, GiB and wall time, and the gate),
and ``src/spacescans/orchestrate/memory_table.csv`` (what the out-of-memory hint reads).
Old-runner numbers that were not re-measured come from the paper's
``results/data/runtime.csv`` (same image contents, same container memory, same wrapper
position): the median of its cold repeats per size and experiment.

Examples::

    python3 tools/memory_gate.py run --stage /scratch/stage --sizes 10k --modes new
    python3 tools/memory_gate.py run --stage /scratch/stage --sizes 10k --cases custom_raster,custom_bg_csv --modes old,new
    python3 tools/memory_gate.py report --stage /scratch/stage --date 2026-10-13

Only the standard library and the docker CLI are needed on the host.
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

ROOT = Path("/Users/xai/Desktop/spacescans-all")
IMAGE = "spacescans-web-backend:frozen-2026-10-12"
DATA_DIR = ROOT / "spacescans-web" / "pipeline-data"
CONFIGS_DIR = ROOT / "spacescans-web" / "configs"
COHORTS_DIR = ROOT / "spacescans-paper" / "results" / "data" / "cohorts"
GOLDEN_DIR = ROOT / "tier2-artifacts" / "goldens" / "web-0.2.7"
OLD_CSV = ROOT / "spacescans-paper" / "results" / "data" / "runtime.csv"
HEAVY_LOCK = ROOT / "tier2-artifacts" / ".heavy.lock"
CONTAINER_PREFIX = "tier2-c-memory"
WEB_PREFIX = "/opt/conda/bin/python /opt/conda/bin/spacescans"
CONTAINER_GIB = 24.0            # Docker Desktop's allocation; MemTotal inside is 23.4 GiB
TOLERANCE = 1.05                # gate: new <= old * 1.05

BASE_CONFIG = {"experiment": "auto", "buffer": {"shape": "circle", "size": 270, "raster_res_m": 25},
               "version": 1, "input_file": "input.csv"}
EPISODES = {"1k": 1001, "10k": 10002, "100k": 100000}
DS_STORE = ".DS_Store"


@dataclass(frozen=True)
class Case:
    name: str
    experiment: str
    variables: tuple[str, ...]
    custom: str | None = None       # golden run whose config.json holds the upload definition
    expect_oom: tuple[str, ...] = ()  # cohort sizes where the old runner was OOM-killed


# Fast cases first; the two upload cases next (both modes are measured for them); ACAG last.
CASES: tuple[Case, ...] = (
    Case("noise", "noise", ("noise",)),
    Case("temis", "temis", ("temis",), expect_oom=("100k",)),
    Case("vnl", "vnl", ("vnl",)),
    Case("custom_raster", "custom", (), custom="custom_raster"),
    Case("custom_bg_csv", "custom", (), custom="custom_bg_csv"),
    Case("nhd_bluespace", "nhd_bluespace", ("nhd_bluespace",)),
    Case("tiger_proximity", "tiger_proximity", ("tiger_proximity",)),
    Case("fara_tract", "fara_tract", ("fara_tract",)),
    Case("zcta5_cbp", "zcta5_cbp", ("cbp_zcta5",)),
    Case("bg_ndi_wi", "bg_ndi_wi", ("ndi", "walkability")),
    Case("faqsd", "faqsd", ("faqsd",), expect_oom=("100k",)),
    Case("acag", "acag", ("acag",)),
)
CASE_BY_NAME = {c.name: c for c in CASES}
CATALOG_CASES = tuple(c.name for c in CASES if not c.custom)
UPLOAD_CASES = tuple(c.name for c in CASES if c.custom)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def eastern(iso_utc: str) -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromisoformat(iso_utc).astimezone(ZoneInfo("America/New_York")).isoformat(
            timespec="seconds")
    except Exception:  # noqa: BLE001 - zoneinfo data missing; keep UTC  # pragma: no cover
        return iso_utc


def say(msg: str) -> None:
    print(f"[{eastern(utc_now())}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# heavy lock
# ---------------------------------------------------------------------------

class HeavyLock:
    """The host-wide lock every heavy run takes (contract 2.8); held only around the container."""

    def __init__(self, path: Path = HEAVY_LOCK) -> None:
        self.path = path
        self.waited_s = 0.0
        self._f = None

    def __enter__(self) -> HeavyLock:  # noqa: PYI034 - a plain context manager
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "w")
        t0 = time.monotonic()
        fcntl.flock(self._f, fcntl.LOCK_EX)
        self.waited_s = round(time.monotonic() - t0, 3)
        return self

    def __exit__(self, *exc) -> None:
        if self._f is not None:
            fcntl.flock(self._f, fcntl.LOCK_UN)
            self._f.close()
            self._f = None


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------

def upload_definitions(golden_run: str) -> dict:
    """The upload definition the golden run used, container paths under /app/data unchanged."""
    cfg = json.loads((GOLDEN_DIR / golden_run / "config.json").read_text())
    defs = cfg.get("custom_variables") or {}
    if not defs:
        raise SystemExit(f"golden run {golden_run} has no custom_variables")
    return defs


def stage_upload_files(appdata: Path, defs: dict) -> list[str]:
    """Put the uploaded files where the definitions say (/app/data/custom_exposomes/...)."""
    placed = []
    for d in defs.values():
        if d.get("geometry") == "raster":
            for r in d["rasters"]:
                dest = appdata / Path(r["path"]).relative_to("/app/data")
                dest.parent.mkdir(parents=True, exist_ok=True)
                # the raster upload is the catalog's own noise GeoTIFF (golden README): a symlink
                # to its path inside the container saves a 432 MB copy; sha256 is unchanged
                if dest.is_symlink() or dest.exists():
                    dest.unlink()
                dest.symlink_to("/project/Noise/C3/CONUS_L50dBA_sumDay_exi.tif")
                placed.append(str(dest))
        else:
            src = GOLDEN_DIR / "custom_bg_csv" / "upload" / Path(d["values_path"]).name
            dest = appdata / Path(d["values_path"]).relative_to("/app/data")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            placed.append(str(dest))
    return placed


def newer_files(root: Path, stamp: Path) -> list[str]:
    limit = stamp.stat().st_mtime_ns
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            try:
                if p.lstat().st_mtime_ns > limit:
                    out.append(str(p))
            except FileNotFoundError:
                continue
    return sorted(out)


KEEP_SUFFIXES = (".jsonl", ".json", ".yaml", ".txt")


def keep_evidence(appdata: Path, keep_dir: Path) -> list[str]:
    """Copy logs, status, configs, cache sidecars and records out of the scratch mount."""
    kept = []
    for p in sorted(appdata.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        if p.suffix not in KEEP_SUFFIXES or p.name in ("config.json", "custom.json", "manifest.json"):
            continue
        rel = p.relative_to(appdata)
        dest = keep_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dest)
        kept.append(str(rel))
    return kept


_CURRENT: dict[str, str | None] = {"container": None}


def _on_signal(signum, _frame):  # pragma: no cover - interactive use
    name = _CURRENT["container"]
    if name:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
    raise SystemExit(128 + signum)


def image_id(image: str) -> str | None:
    r = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                       capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


# ---------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------

def next_container_name(stage: Path) -> str:
    counter = stage / ".container_seq"
    n = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(n))
    return f"{CONTAINER_PREFIX}-{n}"


def run_one(stage: Path, size: str, case: Case, mode: str, *, src: Path, keep: bool,
            redo: bool, dry_run: bool, image: str) -> dict | None:
    rdir = stage / size / case.name / mode
    record_path = rdir / "record.json"
    if record_path.exists() and not redo:
        say(f"{size} {case.name} {mode}: record exists, skipping (use --redo)")
        return None
    rdir.mkdir(parents=True, exist_ok=True)
    appdata = rdir / "appdata"
    if appdata.exists():
        shutil.rmtree(appdata)
    appdata.mkdir()
    cohort = COHORTS_DIR / f"synth_{size}.csv"
    defs = upload_definitions(case.custom) if case.custom else None
    variables = list(defs) if defs else list(case.variables)
    measure = "/tools/memory_gate_measure.py"
    if mode == "old":
        task = appdata / "tasks" / f"{CONTAINER_PREFIX}-{size}-{case.name}"
        (task / "output").mkdir(parents=True)
        shutil.copyfile(cohort, task / "input.csv")
        cfg = {**BASE_CONFIG, "variables": variables}
        if defs:
            cfg["custom_variables"] = defs
        (task / "config.json").write_text(json.dumps(cfg))
        cmd_in = [measure, "old", "--task", f"/app/data/tasks/{task.name}",
                  "--experiment", case.experiment, "--variables", ",".join(variables),
                  "--out", "/app/data/record.json"]
    elif mode == "new":
        shutil.copyfile(cohort, appdata / "cohort.csv")
        cmd_in = [measure, "new", "--cohort", "/app/data/cohort.csv",
                  "--variables", ",".join(variables), "--run-dir", "/app/data/run",
                  "--cache-dir", "/app/data/c3_cache", "--prefix", WEB_PREFIX,
                  "--out", "/app/data/record.json"]
        if defs:
            (appdata / "custom.json").write_text(json.dumps(defs))
            cmd_in += ["--custom", "/app/data/custom.json"]
    else:
        raise SystemExit(f"unknown mode {mode}")
    placed = stage_upload_files(appdata, defs) if defs else []

    name = next_container_name(stage)
    docker = ["docker", "run", "--rm", "--name", name, "--network", "none",
              "--oom-score-adj", "500", "--entrypoint", "/opt/conda/bin/python", "-w", "/app",
              "-v", f"{DATA_DIR}:/project:ro", "-v", f"{CONFIGS_DIR}:/configs:ro",
              "-v", f"{appdata}:/app/data", "-v", f"{HERE}:/tools:ro",
              "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONUNBUFFERED=1"]
    if mode == "new":
        docker += ["-v", f"{src}:/src:ro", "-e", "PYTHONPATH=/src"]
    docker += [image, *cmd_in]
    if dry_run:
        print(" ".join(docker))
        shutil.rmtree(appdata)
        return None

    stamp = rdir / "data.stamp"
    stamp.touch()
    time.sleep(0.02)
    say(f"{size} {case.name} {mode}: waiting for the heavy lock")
    with HeavyLock() as lock:
        say(f"{size} {case.name} {mode}: lock held (waited {lock.waited_s}s); container {name}")
        _CURRENT["container"] = name
        started = utc_now()
        t0 = time.monotonic()
        with open(rdir / "docker.log", "ab") as log:
            proc = subprocess.run(docker, stdout=log, stderr=subprocess.STDOUT, check=False)
        docker_wall = round(time.monotonic() - t0, 3)
        _CURRENT["container"] = None
    ended = utc_now()
    newer = newer_files(DATA_DIR, stamp)
    rec_file = appdata / "record.json"
    record: dict = json.loads(rec_file.read_text()) if rec_file.exists() else {"missing_record": True}
    src_rev = (src.parent / "REV")
    record["meta"] = {
        "size": size, "episodes": EPISODES.get(size), "case": case.name,
        "experiment": case.experiment, "variables": variables, "upload": bool(defs), "mode": mode,
        "expect_oom": size in case.expect_oom,
        "container": name, "image": image, "image_id": image_id(image),
        "package_src": str(src) if mode == "new" else None,
        "package_rev": src_rev.read_text().strip() if (mode == "new" and src_rev.exists()) else None,
        "docker_rc": proc.returncode, "docker_wall_s": docker_wall, "lock_wait_s": lock.waited_s,
        "started_utc": started, "ended_utc": ended,
        "started_eastern": eastern(started), "ended_eastern": eastern(ended),
        "data_dir": str(DATA_DIR),
        "data_newer": [p for p in newer if Path(p).name != DS_STORE],
        "data_newer_ds_store": [p for p in newer if Path(p).name == DS_STORE],
        "upload_files_staged": placed,
    }
    record_path.write_text(json.dumps(record, indent=2, default=str) + "\n")
    with open(stage / "runs.jsonl", "a") as f:
        f.write(json.dumps(summarise(record), default=str) + "\n")
    kept = keep_evidence(appdata, rdir / "keep")
    if not keep:
        shutil.rmtree(appdata)
    s = summarise(record)
    say(f"{size} {case.name} {mode}: rc={s['rc']} wall={s['wall_s']}s peak={s['peak_gib']} GiB "
        f"failed={s['failed']} data_newer={len(record['meta']['data_newer'])} kept={len(kept)} files")
    return record


def summarise(record: dict) -> dict:
    meta = record.get("meta", {})
    peak = record.get("children_maxrss_bytes")
    link = record.get("link") or {}
    return {"size": meta.get("size"), "case": meta.get("case"), "mode": meta.get("mode"),
            "container": meta.get("container"), "rc": record.get("rc"),
            "docker_rc": meta.get("docker_rc"), "wall_s": record.get("wall_s"),
            "peak_gib": round(peak / 2**30, 3) if peak else None,
            "failed": link.get("failed") if link else (None if record.get("rc") == 0 else "rc"),
            "started_eastern": meta.get("started_eastern"), "ended_eastern": meta.get("ended_eastern")}


def cmd_run(args: argparse.Namespace) -> int:
    stage = Path(args.stage).resolve()
    stage.mkdir(parents=True, exist_ok=True)
    src = Path(args.src).resolve() if args.src else REPO / "src"
    if not (src / "spacescans" / "__init__.py").is_file():
        raise SystemExit(f"--src must hold the package: {src}/spacescans")
    sizes = [s for s in args.sizes.split(",") if s]
    names = [n for n in (args.cases.split(",") if args.cases != "all" else CATALOG_CASES + UPLOAD_CASES)
             if n]
    unknown = [n for n in names if n not in CASE_BY_NAME]
    if unknown:
        raise SystemExit(f"unknown case(s): {unknown}; known: {list(CASE_BY_NAME)}")
    modes = [m for m in args.modes.split(",") if m]
    for s in sizes:
        if not (COHORTS_DIR / f"synth_{s}.csv").is_file():
            raise SystemExit(f"no cohort synth_{s}.csv in {COHORTS_DIR}")
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    say(f"stage {stage}; sizes {sizes}; cases {names}; modes {modes}; image {args.image}; src {src}")
    for size in sizes:
        for n in names:
            for mode in modes:
                try:
                    run_one(stage, size, CASE_BY_NAME[n], mode, src=src, keep=args.keep,
                            redo=args.redo, dry_run=args.dry_run, image=args.image)
                except Exception as exc:  # noqa: BLE001 - record and go on with the next run
                    say(f"{size} {n} {mode}: DRIVER ERROR {exc!r}")
                    rdir = stage / size / n / mode
                    rdir.mkdir(parents=True, exist_ok=True)
                    (rdir / "driver_error.txt").write_text(repr(exc) + "\n")
    say("done")
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def load_records(stage: Path) -> list[dict]:
    out = []
    for p in sorted(stage.glob("*/*/*/record.json")):
        rec = json.loads(p.read_text())
        if rec.get("meta"):
            out.append(rec)
    return out


def old_from_paper(csv_path: Path) -> dict[tuple[str, str], dict]:
    """Median cold wall and peak per (size, experiment) from the paper's runtime.csv."""
    rows: dict[tuple[str, str], list[dict]] = {}
    with open(csv_path, newline="") as f:
        for r in csv.DictReader(f):
            if r["mode"] != "cold":
                continue
            rows.setdefault((r["cohort_size"], r["experiment"]), []).append(r)
    out = {}
    for key, rs in rows.items():
        peaks = [float(r["peak_rss_gb"]) for r in rs]
        walls = [float(r["wall_s"]) for r in rs]
        rcs = sorted({int(r["rc"]) for r in rs})
        out[key] = {"peak_gib": statistics.median(peaks), "wall_s": statistics.median(walls),
                    "runs": len(rs), "rc": rcs, "variables": rs[0]["variables"],
                    "episodes": int(rs[0]["n_episodes"]),
                    "started_utc": min(r["started_utc"] for r in rs),
                    "stage_c3_s": statistics.median(float(r["stage_c3_s"]) for r in rs),
                    "stage_c4_s": statistics.median(float(r["stage_c4_s"]) for r in rs)}
    return out


def gib(b: float | None) -> float | None:
    return None if b is None else round(b / 2**30, 3)


def fmt(v, nd=2) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def new_summary(rec: dict) -> dict:
    """What the gate needs from a ``new`` record."""
    link = rec.get("link") or {}
    steps = link.get("steps") or []
    step_peaks = {s["name"]: s.get("maxrss_bytes") for s in steps if s.get("maxrss_bytes")}
    worst = max(step_peaks, key=step_peaks.get) if step_peaks else None
    sampler = (rec.get("sampler") or {}).get("peaks") or {}
    return {
        "peak_bytes": rec.get("children_maxrss_bytes"),
        "orchestrator_bytes": link.get("self_maxrss_bytes"),
        "steps_bytes": step_peaks, "worst_step": worst,
        "sampler_bytes": {k: v.get("vmhwm_bytes") for k, v in sampler.items()},
        "wall_s": rec.get("wall_s"), "link_wall_s": link.get("link_wall_s"),
        "step_seconds": {s["name"]: s.get("seconds") for s in steps},
        "statuses": {s["name"]: s.get("status") for s in steps},
        "failed": link.get("failed") or [], "skipped": link.get("skipped") or [],
        "errors": link.get("errors") or [], "ok": link.get("ok"),
        "rc": rec.get("rc"),
    }


def old_summary(rec: dict) -> dict:
    sampler = (rec.get("sampler") or {}).get("peaks") or {}
    log = rec.get("log") or {}
    steps = log.get("steps") or {}
    return {"peak_bytes": rec.get("children_maxrss_bytes"), "wall_s": rec.get("wall_s"),
            "rc": rec.get("rc"), "result_exists": rec.get("result_exists"),
            "sampler_bytes": {k: v.get("vmhwm_bytes") for k, v in sampler.items()},
            "step_seconds": {k: v.get("seconds") for k, v in steps.items()},
            "step_rc": {k: v.get("rc") for k, v in steps.items()}}


def oom_check(ns: dict, case: Case) -> tuple[bool, str]:
    """An expected OOM must come back as step_failed with the memory hint, Step 1 done first."""
    errors = ns["errors"]
    if case.experiment not in ns["failed"] or not errors:
        return False, "the source did not fail"
    err = errors[0]
    if err.get("kind") != "step_failed":
        return False, f"error kind {err.get('kind')}"
    if err.get("rc") not in (-9, 137):
        return False, f"rc {err.get('rc')}"
    if "memory" not in (err.get("hint") or ""):
        return False, "hint does not mention memory"
    order = list(ns["statuses"])
    c3 = [s for s in order if s.startswith("c3")]
    if not c3 or any(ns["statuses"][s] not in ("ran", "cache_hit", "reused") for s in c3):
        return False, f"C3 did not complete: {ns['statuses']}"
    failed_steps = [s for s, st in ns["statuses"].items() if st == "failed"]
    if failed_steps != [err.get("step")] or not err["step"].startswith("c4"):
        return False, f"failed steps {failed_steps}"
    return True, f"step_failed in {err['step']} (rc {err['rc']}) after {', '.join(c3)} completed; hint: {err['hint']}"


def cmd_report(args: argparse.Namespace) -> int:
    stage = Path(args.stage).resolve()
    repo = Path(args.repo).resolve()
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    records = load_records(stage)
    paper = old_from_paper(Path(args.old_csv))
    new = {(r["meta"]["size"], r["meta"]["case"]): r for r in records if r["meta"]["mode"] == "new"}
    old = {(r["meta"]["size"], r["meta"]["case"]): r for r in records if r["meta"]["mode"] == "old"}
    sizes = [s for s in ("1k", "10k", "100k") if any(k[0] == s for k in list(new) + list(old) + list(paper))]

    # ---- long table: one row per run and per step --------------------------------------------
    long_rows: list[dict] = []
    gate_rows: list[dict] = []
    for case in CASES:
        for size in sizes:
            key = (size, case.name)
            o_rec, n_rec = old.get(key), new.get(key)
            o_src = None
            if o_rec is not None:
                o = old_summary(o_rec)
                o_src = "measured"
                o_peak, o_wall = gib(o["peak_bytes"]), o["wall_s"]
                for step, b in sorted(o["sampler_bytes"].items()):
                    long_rows.append({"date": date, "size": size, "episodes": EPISODES[size],
                                      "case": case.name, "experiment": case.experiment,
                                      "variables": ";".join(o_rec["meta"]["variables"]), "mode": "old",
                                      "source": "measured (VmHWM sampler)", "step": step,
                                      "peak_gib": gib(b), "wall_s": o["step_seconds"].get(step),
                                      "status": None, "rc": o["step_rc"].get(step), "outcome": None})
                long_rows.append({"date": date, "size": size, "episodes": EPISODES[size],
                                  "case": case.name, "experiment": case.experiment,
                                  "variables": ";".join(o_rec["meta"]["variables"]), "mode": "old",
                                  "source": "measured (RUSAGE_CHILDREN)", "step": "(total)",
                                  "peak_gib": o_peak, "wall_s": o_wall, "status": None, "rc": o["rc"],
                                  "outcome": "ok" if o["rc"] == 0 and o["result_exists"] else "failed"})
            elif (size, case.experiment) in paper and not case.custom:
                p = paper[(size, case.experiment)]
                o_src = f"paper runtime.csv (median of {p['runs']} cold run{'s' if p['runs'] > 1 else ''})"
                o_peak, o_wall = round(p["peak_gib"], 3), round(p["wall_s"], 2)
                long_rows.append({"date": date, "size": size, "episodes": EPISODES[size],
                                  "case": case.name, "experiment": case.experiment,
                                  "variables": p["variables"], "mode": "old", "source": o_src,
                                  "step": "(total)", "peak_gib": o_peak, "wall_s": o_wall,
                                  "status": None, "rc": ";".join(map(str, p["rc"])),
                                  "outcome": "ok" if p["rc"] == [0] else "oom" if size in case.expect_oom else "failed"})
            else:
                o_peak = o_wall = None
            n = None
            if n_rec is not None:
                n = new_summary(n_rec)
                vars_ = ";".join(n_rec["meta"]["variables"])
                for step, b in n["steps_bytes"].items():
                    long_rows.append({"date": date, "size": size, "episodes": EPISODES[size],
                                      "case": case.name, "experiment": case.experiment,
                                      "variables": vars_, "mode": "new",
                                      "source": "measured (executor wait4)", "step": step,
                                      "peak_gib": gib(b), "wall_s": n["step_seconds"].get(step),
                                      "status": n["statuses"].get(step), "rc": None, "outcome": None})
                for step, st in n["statuses"].items():
                    if step not in n["steps_bytes"]:
                        long_rows.append({"date": date, "size": size, "episodes": EPISODES[size],
                                          "case": case.name, "experiment": case.experiment,
                                          "variables": vars_, "mode": "new",
                                          "source": "measured (executor wait4)", "step": step,
                                          "peak_gib": None, "wall_s": n["step_seconds"].get(step),
                                          "status": st, "rc": None, "outcome": None})
                long_rows.append({"date": date, "size": size, "episodes": EPISODES[size],
                                  "case": case.name, "experiment": case.experiment,
                                  "variables": vars_, "mode": "new", "source": "measured (RUSAGE_SELF)",
                                  "step": "(orchestrator)", "peak_gib": gib(n["orchestrator_bytes"]),
                                  "wall_s": n["link_wall_s"], "status": None, "rc": None, "outcome": None})
                outcome = "ok" if n["ok"] else ("oom" if any(e.get("rc") in (-9, 137) for e in n["errors"]) else "failed")
                long_rows.append({"date": date, "size": size, "episodes": EPISODES[size],
                                  "case": case.name, "experiment": case.experiment,
                                  "variables": vars_, "mode": "new", "source": "measured (RUSAGE_CHILDREN)",
                                  "step": "(total)", "peak_gib": gib(n["peak_bytes"]), "wall_s": n["wall_s"],
                                  "status": None, "rc": n["rc"], "outcome": outcome})
            if n is not None or o_peak is not None:
                n_peak = gib(n["peak_bytes"]) if n else None
                ratio = (n_peak / o_peak) if (n_peak and o_peak) else None
                expect_oom = size in case.expect_oom
                if n is None:
                    verdict = "not measured"
                elif expect_oom:
                    ok, why = oom_check(n, case)
                    verdict = ("PASS: " if ok else "FAIL: ") + why
                elif ratio is None:
                    verdict = "no old baseline"
                else:
                    verdict = "PASS" if ratio <= TOLERANCE else "FAIL"
                gate_rows.append({"size": size, "episodes": EPISODES[size], "case": case.name,
                                  "experiment": case.experiment, "old_peak_gib": o_peak,
                                  "old_source": o_src, "new_peak_gib": n_peak,
                                  "new_orchestrator_gib": gib(n["orchestrator_bytes"]) if n else None,
                                  "new_worst_step": n["worst_step"] if n else None,
                                  "new_worst_step_gib": gib(n["steps_bytes"].get(n["worst_step"])) if n and n["worst_step"] else None,
                                  "ratio": round(ratio, 4) if ratio else None,
                                  "limit_gib": round(o_peak * TOLERANCE, 3) if o_peak else None,
                                  "old_wall_s": o_wall, "new_wall_s": n["wall_s"] if n else None,
                                  "wall_ratio": round(n["wall_s"] / o_wall, 3) if (n and o_wall) else None,
                                  "verdict": verdict,
                                  "container": n_rec["meta"]["container"] if n_rec else None,
                                  "data_newer": len(n_rec["meta"]["data_newer"]) if n_rec else None,
                                  "started_eastern": n_rec["meta"]["started_eastern"] if n_rec else None})

    docs = repo / "docs"
    docs.mkdir(exist_ok=True)
    long_path = docs / f"memory-{date}.csv"
    fields = ["date", "size", "episodes", "case", "experiment", "variables", "mode", "source", "step",
              "peak_gib", "wall_s", "status", "rc", "outcome"]
    with open(long_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(long_rows)

    # ---- the hint's table: per experiment and size, the measured peak -------------------------
    table_rows = []
    for g in gate_rows:
        peak = g["new_peak_gib"] if g["new_peak_gib"] is not None else g["old_peak_gib"]
        if peak is None:
            continue
        n_rec = new.get((g["size"], g["case"]))
        outcome = "ok"
        if n_rec is not None:
            ns = new_summary(n_rec)
            outcome = "ok" if ns["ok"] else ("oom" if any(e.get("rc") in (-9, 137) for e in ns["errors"]) else "failed")
            step = ns["worst_step"] or ""
            source = "orchestrate.link"
            wall = ns["wall_s"]
        else:
            outcome = "oom" if g["size"] in CASE_BY_NAME[g["case"]].expect_oom else "ok"
            step = ""
            source = "web-0.2.7 runner"
            wall = g["old_wall_s"]
        table_rows.append({"experiment": g["experiment"], "case": g["case"], "episodes": g["episodes"],
                           "peak_gib": fmt(peak, 3), "peak_step": step, "wall_s": fmt(wall, 1),
                           "outcome": outcome, "container_gib": f"{CONTAINER_GIB:.0f}",
                           "source": source, "measured": date})
    table_path = repo / "src" / "spacescans" / "orchestrate" / "memory_table.csv"
    with open(table_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(table_rows[0]) if table_rows else
                           ["experiment", "case", "episodes", "peak_gib", "peak_step", "wall_s",
                            "outcome", "container_gib", "source", "measured"])
        w.writeheader()
        w.writerows(table_rows)

    # ---- docs/memory.md -----------------------------------------------------------------------
    md = [f"# Memory and wall time per source and cohort size (gate M, measured {date})", ""]
    md.append("Peak resident set size (GiB) and wall time (s) of one cold run per source, old web runner "
              "(`spacescans-web-backend:frozen-2026-10-12`: web 3991c3b + pipeline 0.2.7) against the new "
              "`orchestrate.link` run in the same image with the package mounted at `/src`. One run at a "
              "time in an ephemeral container of that image, Docker Desktop with 24 GB (23.4 GiB "
              "`MemTotal`), 14 CPUs, buffer 270 m, raster resolution 25 m, cold C3 cache, cohorts "
              "`synth_1k` (1,001 episodes), `synth_10k` (10,002) and `synth_100k` (100,000).")
    md.append("")
    md.append("Peak = `RUSAGE_CHILDREN` `ru_maxrss` around the runner or orchestrator process: the largest "
              "resident set of any process it waited for (the pipeline steps run one at a time). Old-runner "
              "numbers marked *paper* are the medians of the cold repeats in the paper's "
              "`results/data/runtime.csv` (same image contents, same container memory, same wrapper "
              "position; 10k: 3 repeats, 100k: 1 run); the others were re-measured here. New-path step "
              "peaks come from the executor's `wait4`. The gate is new <= old + 5% per source and size.")
    md.append("")
    md.append("## Peak RSS (GiB)")
    md.append("")
    head = "| Source (variables) |" + "".join(f" {s} old | {s} new |" for s in sizes)
    md.append(head)
    md.append("|---|" + "---|---|" * len(sizes))
    for case in CASES:
        label = f"`{case.experiment}` ({', '.join(case.variables) if case.variables else case.name})"
        cells = []
        for size in sizes:
            g = next((g for g in gate_rows if g["size"] == size and g["case"] == case.name), None)
            if g is None:
                cells += ["", ""]
                continue
            o = fmt(g["old_peak_gib"]) + ("" if (g["old_source"] or "").startswith("measured") or g["old_peak_gib"] is None else "*")
            if g["new_peak_gib"] is None:
                nn = ""
            else:
                nn = fmt(g["new_peak_gib"])
                if g["verdict"].startswith("PASS") and size in case.expect_oom:
                    nn += " (OOM, step_failed)"
            cells += [o, nn]
        md.append(f"| {label} |" + "".join(f" {c} |" for c in cells))
    md.append("")
    md.append("\\* paper `runtime.csv` (old runner, not re-measured). An old-runner value at 100k for "
              "`temis` and `faqsd` is the peak before the kernel killed the C4 step.")
    md.append("")
    md.append("## Wall time (s)")
    md.append("")
    md.append(head)
    md.append("|---|" + "---|---|" * len(sizes))
    for case in CASES:
        label = f"`{case.experiment}` ({', '.join(case.variables) if case.variables else case.name})"
        cells = []
        for size in sizes:
            g = next((g for g in gate_rows if g["size"] == size and g["case"] == case.name), None)
            if g is None:
                cells += ["", ""]
                continue
            cells += [fmt(g["old_wall_s"], 1), fmt(g["new_wall_s"], 1)]
        md.append(f"| {label} |" + "".join(f" {c} |" for c in cells))
    md.append("")
    md.append("## Gate: new <= old + 5% (peak RSS)")
    md.append("")
    md.append("| Size | Source | Old GiB | Old from | New GiB | Orchestrator GiB | Worst step (GiB) | New/old | Old s | New s | Verdict |")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for g in gate_rows:
        if g["new_peak_gib"] is None and g["verdict"] == "not measured":
            continue
        worst = f"{g['new_worst_step']} ({fmt(g['new_worst_step_gib'])})" if g["new_worst_step"] else ""
        md.append(f"| {g['size']} | `{g['case']}` | {fmt(g['old_peak_gib'])} | {g['old_source'] or ''} | "
                  f"{fmt(g['new_peak_gib'])} | {fmt(g['new_orchestrator_gib'])} | {worst} | "
                  f"{fmt(g['ratio'], 3)} | {fmt(g['old_wall_s'], 1)} | {fmt(g['new_wall_s'], 1)} | {g['verdict']} |")
    not_measured = [g for g in gate_rows if g["verdict"] == "not measured"]
    if not_measured:
        md.append("")
        md.append("Not measured on the new path: " + ", ".join(f"{g['size']} `{g['case']}`" for g in not_measured) + ".")
    md.append("")
    md.append("## Per-step peaks on the new path (GiB, executor `wait4`)")
    md.append("")
    md.append("| Size | Source | Steps |")
    md.append("|---|---|---|")
    for case in CASES:
        for size in sizes:
            n_rec = new.get((size, case.name))
            if n_rec is None:
                continue
            ns = new_summary(n_rec)
            parts = []
            for step, st in ns["statuses"].items():
                b = ns["steps_bytes"].get(step)
                sec = ns["step_seconds"].get(step)
                parts.append(f"`{step}` {fmt(gib(b))} GiB, {fmt(sec, 1)} s, {st}" if b else f"`{step}` {st}")
            md.append(f"| {size} | `{case.name}` | {'; '.join(parts)} |")
    md.append("")
    md.append("## Files")
    md.append("")
    md.append(f"- `docs/memory-{date}.csv`: every run and step (old and new; the old-runner totals from the paper where not re-measured).")
    md.append("- `src/spacescans/orchestrate/memory_table.csv`: the per-source table the out-of-memory hint reads "
              "(`spacescans.orchestrate.memory`): a `step_failed` with exit code -9/137 then says how much memory "
              "that source needed at the nearest measured cohort size.")
    md.append("- `tools/memory_gate.py`, `tools/memory_gate_measure.py`: how the numbers were produced.")
    md.append("")
    (docs / "memory.md").write_text("\n".join(md) + "\n")

    gate_path = stage / "gate.json"
    gate_path.write_text(json.dumps(gate_rows, indent=2, default=str) + "\n")
    print(f"wrote {long_path} ({len(long_rows)} rows), {docs / 'memory.md'}, {table_path} "
          f"({len(table_rows)} rows), {gate_path}")
    for g in gate_rows:
        print(f"{g['size']:>4} {g['case']:<16} old={fmt(g['old_peak_gib'])} new={fmt(g['new_peak_gib'])} "
              f"ratio={fmt(g['ratio'], 3)} wall {fmt(g['old_wall_s'], 1)} -> {fmt(g['new_wall_s'], 1)} : {g['verdict']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="measure in ephemeral containers of the frozen image")
    p.add_argument("--stage", required=True, help="where records and kept evidence go")
    p.add_argument("--sizes", default="10k", help="comma-separated: 1k,10k,100k")
    p.add_argument("--cases", default="all", help=f"comma-separated case names or 'all'; known: {list(CASE_BY_NAME)}")
    p.add_argument("--modes", default="new", help="comma-separated: new,old")
    p.add_argument("--src", default=None, help="package tree to mount at /src (default: this repo's src)")
    p.add_argument("--image", default=IMAGE)
    p.add_argument("--keep", action="store_true", help="keep the scratch mount (cohort data) after a run")
    p.add_argument("--redo", action="store_true", help="re-run cases that already have a record")
    p.add_argument("--dry-run", action="store_true", help="print the docker command and stop")
    p.set_defaults(func=cmd_run)
    r = sub.add_parser("report", help="write docs/memory.md, docs/memory-<date>.csv and the hint table")
    r.add_argument("--stage", required=True)
    r.add_argument("--repo", default=str(REPO))
    r.add_argument("--old-csv", default=str(OLD_CSV))
    r.add_argument("--date", default=None)
    r.set_defaults(func=cmd_report)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
