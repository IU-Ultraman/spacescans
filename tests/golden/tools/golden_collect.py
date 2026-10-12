"""`collect` and `compare` subcommands of web_goldens.py.

collect: turn a staged pass (web_goldens.py run --stage DIR) into tests/golden/web-0.2.7/:
         one directory per run with the files parity tests need, plus manifest.json.
compare: measure run-to-run variation between two staged passes of the same code
         (critique #4: row order and float bits), written as determinism.json.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import golden_compare as gc  # noqa: E402
from web_goldens import CASES, CASE_BY_NAME, LARGE_BYTES  # noqa: E402

ENV_DIR = HERE.parent / "env"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def csv_shape(path: Path) -> dict:
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    return {"rows": max(len(rows) - 1, 0), "columns": rows[0] if rows else []}


def file_record(path: Path, root: Path) -> dict:
    rec = {"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "sha256": sha256(path)}
    if path.suffix == ".csv":
        rec.update(csv_shape(path))
    elif path.suffix == ".parquet":
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(path)
        rec["rows"] = pf.metadata.num_rows
        rec["columns"] = [f"{x.name}:{x.type}" for x in pf.schema_arrow]
    return rec


def collect_run(name: str, stage: Path, out_root: Path, cohort_sha: str) -> dict:
    src = stage / name
    dst = out_root / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    if (src / "driver_error.txt").exists() and not (src / "run.json").exists():
        err = (src / "driver_error.txt").read_text()
        (dst / "driver_error.txt").write_text(err)
        return {"success": False, "error": err.strip()}
    run = json.loads((src / "run.json").read_text())
    remote = run.pop("remote_files", [])
    library = run.pop("custom_library_files", None)
    task = src / "task"
    experiment = run.get("experiment")

    # verbatim copies
    for rel in ("config.json", "status.json", "logs.jsonl", "input.parquet"):
        if (task / rel).exists():
            shutil.copy2(task / rel, dst / rel)
    if (task / "pipeline_configs").is_dir():
        shutil.copytree(task / "pipeline_configs", dst / "pipeline_configs")
    for rel in ("runner.stdout.txt", "runner.stderr.txt", "custom_definition.json"):
        if (src / rel).exists():
            shutil.copy2(src / rel, dst / rel)
    if (src / "upload").is_dir():                      # the uploaded table, when small
        shutil.copytree(src / "upload", dst / "upload")
    out = task / "output"
    (dst / "output").mkdir()
    if out.is_dir():
        for p in sorted(out.iterdir()):
            if p.suffix == ".parquet" and p.stat().st_size < LARGE_BYTES:
                shutil.copy2(p, dst / "output" / p.name)
            elif p.name in ("result.csv", "feature_dictionary.csv"):
                shutil.copy2(p, dst / "output" / p.name)
    # per-experiment partial: as written, and canonical
    # fanin_all's partials are copies of the per-run partials (recorded by hash in remote_files)
    partials = sorted(out.glob("result_*.csv")) if out.is_dir() and name != "fanin_all" else []
    result_info = {}
    for p in partials:
        stem = p.stem                                  # result_<exp>
        written = dst / "output" / f"{stem}.as_written.csv"
        shutil.copy2(p, written)
        canon = gc.canonical_result_file(p, dst / "output" / f"{stem}.sorted.csv")
        result_info[stem] = {
            "as_written_sha256": sha256(written), "sorted_sha256": sha256(canon),
            "already_sorted": sha256(written) == sha256(canon), **csv_shape(canon)}
    # the private C3 cache: meta sidecars verbatim, parquet described (it equals output/<step>)
    cache = task / "golden_c3_cache"
    cache_entries = []
    if cache.is_dir():
        (dst / "c3_cache").mkdir()
        for meta in sorted(cache.glob("*.meta.json")):
            shutil.copy2(meta, dst / "c3_cache" / meta.name)
        for pq_ in sorted(cache.glob("*.parquet")):
            key = pq_.name[: -len(".parquet")]
            rec = {"key": key, "sha256": sha256(pq_), "bytes": pq_.stat().st_size}
            same = [f.name for f in out.glob("c3_*.parquet") if sha256(f) == rec["sha256"]]
            rec["identical_to_output"] = same
            cache_entries.append(rec)
    # input.csv must be the cohort, byte for byte (not copied: see inputs/)
    input_ok = (task / "input.csv").exists() and sha256(task / "input.csv") == cohort_sha
    large = [r for r in remote if r["bytes"] >= LARGE_BYTES]
    if large:
        (dst / "large_files.json").write_text(json.dumps(large, indent=2) + "\n")
    if library is not None:
        (dst / "custom_library_files.json").write_text(json.dumps(library, indent=2) + "\n")
    (dst / "run.json").write_text(json.dumps(run, indent=2) + "\n")

    files = [file_record(p, dst) for p in sorted(dst.rglob("*")) if p.is_file()]
    steps = {}
    for step, s in (run.get("log_span") or {}).get("steps", {}).items():
        steps[step] = {k: s[k] for k in ("seconds", "rc", "cache_hit") if k in s}
    return {
        "experiment": experiment,
        "variables": run.get("variables"),
        "success": run.get("success"),
        "rc": run.get("rc"),
        "wall_s": run.get("wall_s"),
        "started_eastern": run.get("started_eastern"),
        "ended_eastern": run.get("ended_eastern"),
        "cache": run.get("cache"),
        "note": run.get("note") or None,
        "input_csv_is_cohort": input_ok,
        "steps": steps,
        "results": result_info,
        "c3_cache_entries": cache_entries,
        "large_files": large,
        "custom_library_files": library,
        "files": files,
        "remote_task_dir": run.get("task_dir"),
        **({"fanin_sources": run.get("sources"), "dispatch_plan": run.get("dispatch_plan"),
            "partials_in": [r for r in remote if r["path"].startswith("output/result_")]}
           if name == "fanin_all" else {}),
    }


def cmd_collect(args) -> int:
    stage = Path(args.stage).resolve()
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    stage_meta = json.loads((stage / "stage.json").read_text())
    cohort_src = Path(stage_meta["cohort"])
    (out_root / "inputs").mkdir(exist_ok=True)
    cohort_dst = out_root / "inputs" / cohort_src.name
    shutil.copy2(cohort_src, cohort_dst)
    cohort_sha = sha256(cohort_dst)
    if cohort_sha != stage_meta["cohort_sha256"]:
        sys.exit("cohort changed since the stage was run")
    rows = list(csv.DictReader(open(cohort_dst, newline="")))
    env = json.loads((ENV_DIR / "versions.json").read_text())
    image = json.loads((ENV_DIR / "image.json").read_text())

    names = [c.name for c in CASES if (stage / c.name).exists()]
    if (stage / "fanin_all").exists():
        names.append("fanin_all")
    runs = {n: collect_run(n, stage, out_root, cohort_sha) for n in names}
    manifest = {
        "schema": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": "tests/golden/tools/web_goldens.py (run, then collect)",
        "stage": {k: stage_meta.get(k) for k in ("started_utc", "ended_utc", "container", "failures",
                                                 "custom_library_cleared")},
        "environment": {
            "image": image,
            "web_commit": (ENV_DIR / "web_commit.txt").read_text().split()[1],
            "spacescans.__version__": env["spacescans.__version__"],
            "spacescans_commit": env["spacescans-pipeline direct_url"]["vcs_info"]["commit_id"],
            "python": env["python"].split()[0],
            "packages": env["packages"],
            "native": {k: v for k, v in env["native"].items() if "blas" not in k},
            "cpu_count": env["cpu_count"],
            "meminfo": env["meminfo_first_line"],
        },
        "cohort": {"path": str(cohort_dst.relative_to(out_root)),
                   "source": str(cohort_src), "sha256": cohort_sha,
                   "episodes": len(rows), "patients": len({r["pid"] for r in rows})},
        "params": {"buffer_m": 270, "raster_res_m": 25, "buffer_shape": "circle",
                   "config_base": {"experiment": "auto", "version": 1, "input_file": "input.csv"}},
        "sort_rule": gc.SORT_RULE,
        "large_file_threshold_bytes": LARGE_BYTES,
        "runs": runs,
    }
    (out_root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    bad = [n for n, r in runs.items() if not r.get("success")]
    print(json.dumps({"runs": len(runs), "failed": bad}))
    return 0


# ---------------------------------------------------------------------------
# compare: run-to-run variation between two passes
# ---------------------------------------------------------------------------

def _compare_case(name: str, a: Path, b: Path) -> dict:
    import pandas as pd
    out: dict = {}
    ta, tb = a / name / "task" / "output", b / name / "task" / "output"
    if not (ta.is_dir() and tb.is_dir()):
        return {"skipped": "missing in one pass"}
    for pa in sorted(ta.glob("result_*.csv")):
        pb = tb / pa.name
        if not pb.exists():
            out[pa.name] = {"missing_in_b": True}
            continue
        raw_equal = pa.read_bytes() == pb.read_bytes()
        ca = gc.canonical_result_csv(pa.read_text())
        cb = gc.canonical_result_csv(pb.read_text())
        rec = {"as_written_identical": raw_equal, "sorted_identical": ca == cb}
        if ca != cb:
            import io
            fa = pd.read_csv(io.StringIO(ca))
            fb = pd.read_csv(io.StringIO(cb))
            floats = [c for c in fa.columns if pd.api.types.is_float_dtype(fa[c])]
            rec["max_diffs"] = gc.max_diffs(fa, fb, floats) if len(fa) == len(fb) else "row count differs"
        out[pa.name] = rec
    for pa in sorted(ta.glob("*.parquet")):
        pb = tb / pa.name
        if not pb.exists():
            continue
        rec = {"bytes_identical": pa.read_bytes() == pb.read_bytes()}
        if not rec["bytes_identical"]:
            fa, fb = pd.read_parquet(pa), pd.read_parquet(pb)
            rec["row_order_identical"] = bool(fa.equals(fb))
            sa, sb = gc.canonical_table(fa), gc.canonical_table(fb)
            rec["equal_after_sort"] = bool(sa.equals(sb))
            if not rec["equal_after_sort"] and len(sa) == len(sb):
                floats = [c for c in sa.columns if pd.api.types.is_float_dtype(sa[c])]
                rec["max_diffs"] = gc.max_diffs(sa, sb, floats)
        out[pa.name] = rec
    return out


def cmd_compare(args) -> int:
    a, b = Path(args.a).resolve(), Path(args.b).resolve()
    names = [c.name for c in CASES if (a / c.name).exists() and (b / c.name).exists()]
    report = {
        "what": "run-to-run variation of the 0.2.7 web runners: two cold passes of the same "
                "image on the same cohort and data, compared file by file",
        "pass_a": json.loads((a / "stage.json").read_text()),
        "pass_b": json.loads((b / "stage.json").read_text()),
        "cases": {n: _compare_case(n, a, b) for n in names},
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    summary = {}
    for n, c in report["cases"].items():
        summary[n] = {k: {kk: vv for kk, vv in v.items() if kk != "max_diffs"} if isinstance(v, dict) else v
                      for k, v in c.items()}
    print(json.dumps(summary, indent=1))
    return 0


def add_subcommands(sub) -> None:
    p = sub.add_parser("collect", help="build the golden directory and manifest from a stage")
    p.add_argument("--stage", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_collect)
    p = sub.add_parser("compare", help="run-to-run variation between two staged passes")
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_compare)
