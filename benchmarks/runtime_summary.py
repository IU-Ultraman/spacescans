#!/usr/bin/env python3
"""Summarise a runs file written by runtime.py into <stem>_summary.csv and <stem>_metrics.csv.

One row per cohort size x experiment x mode: number of successful runs, median / min / max
wall-clock seconds, median peak resident memory (GB), median seconds by step (preparation,
spatial computation c3, exposure-value aggregation c4) and the share of wall-clock spent in c3.
Runs with a non-zero exit code are excluded and counted in n_failed. The metrics file (long
format: model, metric, value) holds, per cohort size, the number of sources timed, the fastest
and slowest initial run, the totals, the median / min / max saving from reusing cached spatial
weights ((cold - warm) / cold), the start-up overhead beyond the two steps, and the largest and
smallest peak memory, each with its source.

Single-process runs (workers 1) are summarised when present, and runs with N > 1 workers then
add w<N>_* metrics comparing the two. A file whose runs all use the same number of workers, such
as results/runtime_server.csv (four workers), is summarised from those runs.

    python3 benchmarks/runtime_summary.py [--src benchmarks/results/runtime_server.csv]
"""
from __future__ import annotations

import argparse
import csv
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "results" / "runtime_server.csv"
SIZE_ORDER = {"1k": 1, "10k": 2, "100k": 3}
# Display names of the sources, as in the catalog (PM~2.5~ is Markdown subscript).
LABEL = {"acag": "ACAG PM~2.5~ Composition", "faqsd": "EPA FAQSD Air Quality",
         "nhd_bluespace": "NHD Bluespace", "noise": "NPS Soundscape Noise", "temis": "TEMIS UV Exposure",
         "vnl": "VIIRS Nighttime Lights", "bg_ndi_wi": "Neighborhood Deprivation Index and EPA Walkability Index",
         "fara_tract": "FARA Food Access", "tiger_proximity": "TIGER Road Distance",
         "zcta5_cbp": "Community Organization Density", "walkability": "EPA Walkability Index",
         "ndi": "Neighborhood Deprivation Index"}
# A combined job that is also timed source by source: where its parts have runs at a cohort size,
# the parts stand for it in that size's metrics (the table shows the parts as two rows)
SUPERSEDED = {"bg_ndi_wi": ("walkability", "ndi")}
# Sources whose Step 1 is polygon overlap (boundary_overlap_fast), the step the workers speed up
POLYGON_STEP1 = ("walkability", "ndi", "bg_ndi_wi", "zcta5_cbp", "fara_tract", "faqsd")


def _workers(row: dict) -> int:
    """A run's worker processes; rows written before the column existed are single-process."""
    return int(row.get("workers") or 1)


def _prose_list(items: list[str]) -> str:
    """"a", "a and b", "a, b, and c" — the labels are read inside a sentence."""
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", type=Path, default=SRC,
                    help="runs file (default benchmarks/results/runtime_server.csv)")
    args = ap.parse_args(argv)
    src = args.src
    out_path = src.with_name(f"{src.stem}_summary.csv")
    met_path = src.with_name(f"{src.stem}_metrics.csv")
    if not src.exists():
        sys.exit(f"{src} missing — run benchmarks/runtime.py first")
    every = list(csv.DictReader(src.open()))
    rows = [r for r in every if _workers(r) == 1]
    parallel = [r for r in every if _workers(r) > 1]
    workers = 1   # the runs the summary describes
    if not rows and parallel:
        counts = sorted({_workers(r) for r in parallel})
        if len(counts) > 1:
            sys.exit(f"{src}: no single-process run and runs with {counts} workers; nothing to summarise")
        workers, rows, parallel = counts[0], parallel, []
    groups: dict[tuple, list] = defaultdict(list)
    failed: dict[tuple, int] = defaultdict(int)
    failed_rows: dict[tuple, list] = defaultdict(list)
    for r in rows:
        key = (r["cohort_size"], r["experiment"], r["mode"])
        if r["rc"] != "0":
            failed[key] += 1
            failed_rows[key].append(r)
            continue
        groups[key].append(r)

    def med(rs, col):
        vals = [float(x[col]) for x in rs if x[col] not in ("", "None")]
        return round(st.median(vals), 2) if vals else ""

    out = []
    # groups whose every run failed still get a row (n_runs 0), so a failure is visible; the
    # spatial-weighting time is kept when that step finished before the run was killed
    for key, n in failed.items():
        if key not in groups:
            groups[key] = []
    for key in sorted(groups, key=lambda k: (SIZE_ORDER.get(k[0], 9), k[1], k[2])):
        if not groups[key]:
            out.append({"cohort_size": key[0], "n_episodes": "", "experiment": key[1], "variables": "",
                        "mode": key[2], "n_runs": 0, "n_failed": failed[key], "wall_median_s": "",
                        "wall_min_s": "", "wall_max_s": "", "peak_rss_median_gb": "", "prep_median_s": "",
                        "c3_median_s": med(failed_rows[key], "stage_c3_s"), "c4_median_s": "",
                        "c3_share_pct": "", "workers": workers})
            continue
        rs = groups[key]
        walls = [float(x["wall_s"]) for x in rs]
        c3 = med(rs, "stage_c3_s"); wall = round(st.median(walls), 2)
        out.append({
            "cohort_size": key[0], "n_episodes": rs[0]["n_episodes"], "experiment": key[1],
            "variables": rs[0]["variables"], "mode": key[2], "n_runs": len(rs), "n_failed": failed.get(key, 0),
            "wall_median_s": wall, "wall_min_s": round(min(walls), 2), "wall_max_s": round(max(walls), 2),
            "peak_rss_median_gb": med(rs, "peak_rss_gb"),
            "prep_median_s": med(rs, "stage_prep_s"), "c3_median_s": c3, "c4_median_s": med(rs, "stage_c4_s"),
            "c3_share_pct": round(100 * float(c3) / wall, 1) if c3 != "" and wall else "",
            "workers": workers,
        })
    if not out:
        sys.exit(f"{src}: no run to summarise")
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader(); w.writerows(out)

    # prose-level metrics per cohort size (100k was timed only for the faster sources, so each
    # size reports its own set: n_sources says how many are behind the numbers)
    sizes = sorted({o["cohort_size"] for o in out}, key=lambda s: SIZE_ORDER.get(s, 9))
    metrics = []
    for size in sizes:
        here = {o["experiment"] for o in out if o["cohort_size"] == size}
        gone = {job for job, parts in SUPERSEDED.items() if all(p in here for p in parts)}
        cold = {o["experiment"]: o for o in out if o["cohort_size"] == size and o["mode"] == "cold" and o["n_runs"]
                and o["experiment"] not in gone}
        warm = {o["experiment"]: o for o in out if o["cohort_size"] == size and o["mode"] == "warm" and o["n_runs"]
                and o["experiment"] not in gone}
        both = [e for e in cold if e in warm]
        failed_here = sorted({o["experiment"] for o in out if o["cohort_size"] == size and not o["n_runs"]})
        if not both:
            continue
        savings = {e: 100 * (cold[e]["wall_median_s"] - warm[e]["wall_median_s"]) / cold[e]["wall_median_s"]
                   for e in both if cold[e]["wall_median_s"]}
        # start-up and output merging: the initial run beyond its two steps
        overhead = [cold[e]["wall_median_s"] - float(cold[e]["c3_median_s"]) - float(cold[e]["c4_median_s"])
                    for e in both if cold[e]["c3_median_s"] != "" and cold[e]["c4_median_s"] != ""]
        fastest = min(both, key=lambda e: cold[e]["wall_median_s"])
        slowest = max(both, key=lambda e: cold[e]["wall_median_s"])
        metrics += [
            (size, "n_episodes", cold[both[0]]["n_episodes"]),
            (size, "n_sources", len(both)),
            (size, "cold_min_s", cold[fastest]["wall_median_s"]), (size, "cold_min_source", fastest),
            (size, "cold_max_s", cold[slowest]["wall_median_s"]), (size, "cold_max_source", slowest),
            (size, "cold_max_min", round(cold[slowest]["wall_median_s"] / 60)),   # the prose adds minutes
            (size, "overhead_min_s", round(min(overhead), 1)), (size, "overhead_max_s", round(max(overhead), 1)),
            (size, "cold_total_s", round(sum(cold[e]["wall_median_s"] for e in both), 1)),
            (size, "warm_total_s", round(sum(warm[e]["wall_median_s"] for e in both), 1)),
            (size, "saving_median_pct", round(st.median(savings.values()), 1)),
            (size, "saving_min_pct", round(min(savings.values()), 1)),
            (size, "saving_min_source", min(savings, key=savings.get)),
            (size, "saving_max_pct", round(max(savings.values()), 1)),
            (size, "saving_max_source", max(savings, key=savings.get)),
            (size, "peak_rss_max_gb", max(float(cold[e]["peak_rss_median_gb"]) for e in both)),
            (size, "peak_rss_max_source", max(both, key=lambda e: float(cold[e]["peak_rss_median_gb"]))),
            (size, "n_failed_sources", len(failed_here)),
            (size, "failed_sources", ";".join(failed_here)),
        ]
        # 4.2 states ranges, not only the largest values: the smallest peak memory and the
        # fastest and slowest run reusing cached weights over the same completed sources
        warm_fast = min(both, key=lambda e: warm[e]["wall_median_s"])
        warm_slow = max(both, key=lambda e: warm[e]["wall_median_s"])
        metrics += [
            (size, "peak_rss_min_gb", min(float(cold[e]["peak_rss_median_gb"]) for e in both)),
            (size, "peak_rss_min_source", min(both, key=lambda e: float(cold[e]["peak_rss_median_gb"]))),
            (size, "warm_min_s", warm[warm_fast]["wall_median_s"]), (size, "warm_min_source", warm_fast),
            (size, "warm_max_s", warm[warm_slow]["wall_median_s"]), (size, "warm_max_source", warm_slow),
        ]
        # readable names for the prose, one per *_source metric
        metrics += [(m, k.replace("_source", "_label"), _prose_list([LABEL.get(x, x) for x in str(v).split(";") if x]))
                    for m, k, v in list(metrics) if m == size and (k.endswith("_source") or k == "failed_sources")]
        metrics.append((size, "workers", workers))   # worker processes per step in the runs above

    # worker processes: each source against its own single-process run at the same size
    for size in sizes:
        base = {o["experiment"]: o for o in out
                if o["cohort_size"] == size and o["mode"] == "cold" and o["n_runs"]}
        for n in sorted({_workers(r) for r in parallel if r["cohort_size"] == size}):
            par: dict[str, list] = defaultdict(list)
            for r in parallel:
                if r["cohort_size"] == size and _workers(r) == n and r["mode"] == "cold" and r["rc"] == "0":
                    par[r["experiment"]].append(r)
            gone = {job for job, parts in SUPERSEDED.items() if all(p in par for p in parts)}
            both = [e for e in par if e in base and e not in gone]
            tag = f"w{n}_"
            for e in both:
                wall = round(st.median(float(x["wall_s"]) for x in par[e]), 2)
                metrics += [(size, f"{tag}{e}_cold_s", wall), (size, f"{tag}{e}_base_cold_s", base[e]["wall_median_s"]),
                            (size, f"{tag}{e}_c3_s", med(par[e], "stage_c3_s")),
                            (size, f"{tag}{e}_base_c3_s", base[e]["c3_median_s"]),
                            (size, f"{tag}{e}_c4_s", med(par[e], "stage_c4_s")),
                            (size, f"{tag}{e}_base_c4_s", base[e]["c4_median_s"]),
                            (size, f"{tag}{e}_speedup", round(base[e]["wall_median_s"] / wall, 1))]
            if both:
                stepped = [e for e in both if med(par[e], "stage_c3_s") not in ("", 0)
                           and base[e]["c3_median_s"] not in ("", 0)]
                speed = {e: float(base[e]["c3_median_s"]) / float(med(par[e], "stage_c3_s")) for e in stepped}
                total = sum(round(st.median(float(x["wall_s"]) for x in par[e]), 2) for e in both)
                total_base = sum(base[e]["wall_median_s"] for e in both)
                metrics += [(size, f"{tag}sources", ";".join(both)),
                            (size, f"{tag}sources_label", _prose_list([LABEL.get(e, e) for e in both])),
                            (size, f"{tag}n_sources", len(both)),
                            (size, f"{tag}cold_total_s", round(total, 1)),
                            (size, f"{tag}base_cold_total_s", round(total_base, 1)),
                            (size, f"{tag}cold_total_min", round(total / 60)),
                            (size, f"{tag}base_cold_total_min", round(total_base / 60))]
                if speed:
                    metrics += [(size, f"{tag}c3_speedup_min", round(min(speed.values()), 1)),
                                (size, f"{tag}c3_speedup_max", round(max(speed.values()), 1))]
            poly = [e for e in both if e in POLYGON_STEP1
                    and med(par[e], "stage_c3_s") != "" and base[e]["c3_median_s"] != ""]
            if poly:
                c3 = {e: float(med(par[e], "stage_c3_s")) for e in poly}
                c3_base = {e: float(base[e]["c3_median_s"]) for e in poly}
                speedup = {e: c3_base[e] / c3[e] for e in poly}
                metrics += [(size, f"{tag}poly_sources", ";".join(poly)),
                            (size, f"{tag}poly_label", _prose_list([LABEL.get(e, e) for e in poly])),
                            (size, f"{tag}poly_c3_min_s", round(min(c3.values()), 1)),
                            (size, f"{tag}poly_c3_max_s", round(max(c3.values()), 1)),
                            (size, f"{tag}poly_base_c3_min_s", round(min(c3_base.values()), 1)),
                            (size, f"{tag}poly_base_c3_max_s", round(max(c3_base.values()), 1)),
                            (size, f"{tag}poly_c3_speedup_min", round(min(speedup.values()), 1)),
                            (size, f"{tag}poly_c3_speedup_max", round(max(speedup.values()), 1))]
    with met_path.open("w", newline="") as f:
        w = csv.writer(f); w.writerow(["model", "metric", "value"]); w.writerows(metrics)
    print(f"{len(out)} summary rows -> {out_path}; metrics -> {met_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
