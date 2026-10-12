"""Gate A3 on real web task outputs (local only; skipped unless configured).

R1 is the set of complete web task directories the JAMIA paper validated
(spacescans-paper results/data/validation/pipeline_outputs). Their C4
values derive from licensed sources, so they are never copied into this
repository; point the test at them instead:

    SPACESCANS_R1_DIR=<...>/pipeline_outputs \
    SPACESCANS_R1_COHORT=<...>/cohorts/synth_1k.csv \
    pytest tests/test_web_parity_r1.py

R1 kept no input.csv; the paper copied synth_1k.csv verbatim. For each
task, the partial rebuilt from R1's own C4 parquet must equal R1's
result_<exp>.csv byte for byte. Nothing under SPACESCANS_R1_DIR is
written: files are copied to a temporary directory first.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from spacescans import catalog
from spacescans.orchestrate import merge

R1 = os.environ.get("SPACESCANS_R1_DIR")
COHORT = os.environ.get("SPACESCANS_R1_COHORT")
CATALOG = catalog.payload()["variables"]

pytestmark = pytest.mark.skipif(
    not (R1 and COHORT), reason="set SPACESCANS_R1_DIR and SPACESCANS_R1_COHORT")

# Task directory -> (experiment, C4 file per variable, renames). From the web
# runners' _PARQUET_MAP / step names at 3991c3b.
TASKS = {
    "bgwi": ("bg_ndi_wi", {"ndi": "c4_ndi.parquet", "walkability": "c4_wi.parquet"}, None),
    "faqsd": ("faqsd", {"faqsd": "c4_tract_faqsd.parquet"},
              {"o3": "faqsd_o3", "pm25": "faqsd_pm25"}),
    "fara": ("fara_tract", {"fara_tract": "c4_tract_fara.parquet"}, None),
    "noise": ("noise", {"noise": "c4_noise.parquet"}, None),
    "tiger": ("tiger_proximity", {"tiger_proximity": "c4_tiger_roads.parquet"}, None),
    "vnl": ("vnl", {"vnl": "c4_vnl.parquet"}, None),
    "custom": ("custom", None, None),
}


@pytest.mark.parametrize("task", sorted(TASKS))
def test_partial_rebuilt_from_r1_matches_r1(task, tmp_path):
    src = Path(R1) / task
    config = json.loads((src / "config.json").read_text())
    experiment, parquet_map, renames = TASKS[task]
    catalog = {**CATALOG, **config.get("custom_variables", {})}
    variables = config["variables"]
    if parquet_map is None:            # custom: c4_<key>.parquet per variable
        parquet_map = {v: f"c4_{v}.parquet" for v in variables}

    run = tmp_path / task
    (run / "output").mkdir(parents=True)
    shutil.copyfile(COHORT, run / "input.csv")
    for fname in parquet_map.values():
        shutil.copyfile(src / "output" / fname, run / "output" / fname)

    merged, stats = merge.partial_from_dir(
        run, experiment, variables, parquet_map,
        {k: v["value_cols"] for k, v in catalog.items()},
        renames=renames, rewrite_in_place=renames is not None)
    out = run / "output" / merge.partial_csv_name(experiment)
    merged.to_csv(out, index=False)
    assert out.read_bytes() == (src / "output" / out.name).read_bytes()
    assert stats.cohort_n == 1001
