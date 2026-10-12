#!/usr/bin/env python3
"""Render-only goldens (gates A0 and A2): YAML and legacy cache keys over a parameter matrix.

The run goldens cover one parameter setting. This calls the 0.2.7 web runners' own
`plan()`, `render_yaml()` and `_cache_key()` inside the backend container, without spawning the
pipeline and without the run lock. It covers every runner × variable subset and order ×
buffer {270, 500, 1000} m × raster_res_m {25, 50}, plus upload definitions on all four
polygon boundaries (static and yearly), several uploads in one task, and the raster upload's
C3 step.

Cache keys are computed over web-0.2.7/noise/input.parquet (sha8 12269dbe), the input every
run golden shares. The Tract, ZCTA5 and County upload definitions are synthesised from the
real BG one; render reads only their fields, never their files. A raster upload's C4 render
reads pixels (`_materialise_raster_values`), so only its C3 step is rendered here; the run
golden custom_raster has the rest.

Usage (host, stdlib only):
    python3 tests/golden/tools/render_matrix.py --golden tests/golden/web-0.2.7 \
        --out tests/golden/web-0.2.7/render_matrix
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

CONTAINER = "spacescans-web-backend-1"
PY = "/opt/conda/bin/python"
ROOT = "/app/data/tasks/tier2-golden-render"

IN_CONTAINER = r'''
import copy, importlib, json, shutil, sys
sys.path.insert(0, "/app")
from pathlib import Path
from app import custom_exposomes as lib

root = Path(sys.argv[1]); input_parquet = Path(sys.argv[2])
bg = json.loads(Path(sys.argv[3]).read_text()); raster = json.loads(Path(sys.argv[4]).read_text())
BUFFERS, RASTERS = (270, 500, 1000), (25, 50)
base = {"experiment": "auto", "version": 1, "input_file": "input.csv"}

def synth(boundary, n, yearly=False):
    d = copy.deepcopy(bg)
    d["dataset_id"] = f"0000000{n}"; d["variable_key"] = lib.variable_key(d["dataset_id"])
    d["boundary"] = boundary; d["join_col"] = lib.BOUNDARY_SPEC[boundary]["join_col"]
    d["values_path"] = f"/app/data/custom_exposomes/user-9997/{d['dataset_id']}/values.csv"
    if yearly:
        d["year_col"] = "year"; d["coverage_years"] = [2014, 2017]; d["temporal"] = "yearly"
    return d

defs = {"bg": bg, "tract": synth("Tract", 1), "zcta5": synth("ZCTA5", 2), "county": synth("County", 3),
        "bg_yearly": synth("BG", 4, yearly=True), "tract_yearly": synth("Tract", 5, yearly=True)}
CATALOG = {
    "bg_ndi_wi": [["ndi"], ["walkability"], ["ndi", "walkability"], ["walkability", "ndi"]],
    "zcta5_cbp": [["cbp_zcta5"]], "tiger_proximity": [["tiger_proximity"]],
    "nhd_bluespace": [["nhd_bluespace"]], "noise": [["noise"]], "vnl": [["vnl"]],
    "temis": [["temis"]], "fara_tract": [["fara_tract"]], "acag": [["acag"]], "faqsd": [["faqsd"]],
}
CUSTOM = [["bg"], ["tract"], ["zcta5"], ["county"], ["bg_yearly"], ["tract_yearly"],
          ["bg", "tract", "bg_yearly"], ["raster"]]

out = []
def one(exp, label, variables, custom_cfg, b, r):
    mod = importlib.import_module(f"app.experiments.{exp}")
    combo = f"{exp}__{label}__b{b}_r{r}"
    task = root / combo
    shutil.rmtree(task, ignore_errors=True); (task / "output").mkdir(parents=True)
    cfg = {**base, "buffer": {"shape": "circle", "size": b, "raster_res_m": r}, "variables": variables}
    if custom_cfg:
        cfg["custom_variables"] = custom_cfg
    rec = {"combo": combo, "experiment": exp, "variables": variables, "buffer_m": b,
           "raster_res_m": r, "task_dir": str(task), "steps": []}
    for step in mod.plan(cfg):
        s = {"name": step.name, "template": step.template_relpath, "is_c3": step.is_c3}
        if exp == "custom" and not step.is_c3 and lib.is_custom_key(step.name[len("c4_"):]) \
                and custom_cfg[step.name[len("c4_"):]].get("geometry") == "raster":
            s["rendered"] = False                  # would materialise pixels; see run golden
            rec["steps"].append(s); continue
        path = mod.render_yaml(step, task, cfg)
        s["yaml"] = path.read_text(); s["rendered"] = True
        if step.is_c3:
            if exp == "custom":
                s["cache_key"] = mod._cache_key(input_parquet, mod._boundary_tag_for_step(step), cfg)
            else:
                s["cache_key"] = mod._cache_key(input_parquet, step, cfg)
        rec["steps"].append(s)
    out.append(rec)

for exp, subsets in CATALOG.items():
    for variables in subsets:
        for b in BUFFERS:
            for r in RASTERS:
                one(exp, "-".join(variables), variables, None, b, r)
for picks in CUSTOM:
    chosen = [raster if p == "raster" else defs[p] for p in picks]
    variables = [d["variable_key"] for d in chosen]
    for b in BUFFERS:
        for r in RASTERS:
            one("custom", "-".join(picks), variables, {d["variable_key"]: d for d in chosen}, b, r)
print(json.dumps(out))
'''


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--golden", required=True, help="the collected run goldens (web-0.2.7)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    golden, out = Path(args.golden), Path(args.out)
    # stage the shared input.parquet and the two upload definitions inside the container
    subprocess.run(["docker", "exec", CONTAINER, "sh", "-c", f"rm -rf {ROOT} && mkdir -p {ROOT}"], check=True)
    for src, dst in ((golden / "noise" / "input.parquet", "input.parquet"),
                     (golden / "custom_bg_csv" / "custom_definition.json", "bg.json"),
                     (golden / "custom_raster" / "custom_definition.json", "raster.json")):
        subprocess.run(["docker", "exec", "-i", CONTAINER, "sh", "-c", f"cat > {ROOT}/{dst}"],
                       input=src.read_bytes(), check=True)
    r = subprocess.run(["docker", "exec", "-i", CONTAINER, "sh", "-c",
                        f"cd /app && {PY} - {ROOT}/combos {ROOT}/input.parquet {ROOT}/bg.json {ROOT}/raster.json"],
                       input=IN_CONTAINER.encode(), capture_output=True)
    if r.returncode != 0:
        sys.exit(r.stderr.decode()[-4000:])
    combos = json.loads(r.stdout.decode().strip().splitlines()[-1])
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    index = []
    for c in combos:
        d = out / c["combo"]
        d.mkdir()
        steps = []
        for s in c["steps"]:
            if s.get("rendered"):
                text = s.pop("yaml")
                (d / f"{s['name']}.yaml").write_text(text)
                s["sha256"] = hashlib.sha256(text.encode()).hexdigest()
            steps.append(s)
        index.append({**{k: c[k] for k in ("combo", "experiment", "variables", "buffer_m",
                                           "raster_res_m", "task_dir")}, "steps": steps})
    (out / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    print(json.dumps({"combos": len(index), "yaml": sum(1 for _ in out.rglob("*.yaml"))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
