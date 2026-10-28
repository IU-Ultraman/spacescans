#!/usr/bin/env python3
"""Computational cost by source, pivoted from a summary written by runtime_summary.py.

One row per catalog source, with wall-clock medians at 1k, 10k and 100k episodes for an initial
run and for a run that reuses cached spatial weights, the two steps of the initial run at 100k
(c3: spatial computation, c4: exposure-value aggregation), and the peak memory of the initial
run. A size never timed says "NT"; a run that failed says "OOM" (out of memory). spatial, domain,
n_vars and example come from catalog_sources.csv.

Writes <stem>_table.csv beside --src <stem>_summary.csv.

    python3 benchmarks/runtime_table.py [--src benchmarks/results/runtime_server_summary.csv]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "results" / "runtime_server_summary.csv"
CATALOG = HERE / "catalog_sources.csv"   # spatial type, domain, n_value_cols, example per source

# Catalog keys behind each runtime experiment (one job links both block-group sources).
EXPERIMENT_KEYS = {
    "bg_ndi_wi": ["walkability", "ndi"], "zcta5_cbp": ["cbp_zcta5"], "fara_tract": ["fara_tract"],
    "faqsd": ["faqsd"], "noise": ["noise"], "vnl": ["vnl"], "acag": ["acag"], "temis": ["temis"],
    "nhd_bluespace": ["nhd_bluespace"], "tiger_proximity": ["tiger_proximity"],
    "walkability": ["walkability"], "ndi": ["ndi"],
}

# Catalog names, in the catalog's order (polygon, raster, line); the two block-group
# sources, one job in the application, are timed separately at 100k.
SOURCES = [
    ("walkability", "EPA Walkability Index"),       # timed alone since 2026-10-14 (100k);
    ("zcta5_cbp", "Community Organization Density"),
    ("fara_tract", "FARA Food Access"),
    ("ndi", "Neighborhood Deprivation Index"),      # the combined bg_ndi_wi rows stay in the data
    ("faqsd", "EPA FAQSD Air Quality"),
    ("noise", "NPS Soundscape Noise"),
    ("vnl", "VIIRS Nighttime Lights"),
    ("acag", "ACAG PM~2.5~ Composition"),   # Markdown subscript
    ("temis", "TEMIS UV Exposure"),
    ("nhd_bluespace", "NHD Bluespace"),
    ("tiger_proximity", "TIGER Road Distance"),
]
NOT_TIMED = "NT"                 # not timed at that size
OUT_OF_MEMORY = "OOM"


def seconds(x: str) -> str:
    v = float(x)
    return f"{v:,.0f}" if v >= 10 else f"{v:.1f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", type=Path, default=SRC,
                    help="summary file (default benchmarks/results/runtime_server_summary.csv)")
    args = ap.parse_args(argv)
    src = args.src
    if src.name.endswith("_summary.csv"):
        out = src.with_name(src.name[:-len("_summary.csv")] + "_table.csv")
    else:
        sys.exit(f"{src}: --src must be a *_summary.csv written by runtime_summary.py")
    if not src.exists():
        sys.exit(f"{src} missing — run benchmarks/runtime_summary.py first")
    rows = {(r["cohort_size"], r["experiment"], r["mode"]): r for r in csv.DictReader(src.open())}
    if not CATALOG.exists():
        sys.exit(f"{CATALOG} missing — it ships with this script")
    catalog = {r["key"]: r for r in csv.DictReader(CATALOG.open()) if r.get("key")}

    def cell(size: str, exp: str, mode: str, col: str, fmt, *, stage: bool = False) -> str:
        r = rows.get((size, exp, mode))
        if r is None:
            return NOT_TIMED
        if int(r["n_runs"] or 0) == 0:
            if stage and r[col] not in ("", None):      # the step finished before the kill
                return fmt(r[col])
            return OUT_OF_MEMORY if int(r["n_failed"] or 0) else NOT_TIMED
        return fmt(r[col])

    table = []
    for exp, label in SOURCES:
        if not any(k[1] == exp for k in rows):
            sys.exit(f"{exp}: no runtime rows in {src.name}")
        keys = EXPERIMENT_KEYS[exp]
        missing = [k for k in keys if k not in catalog]
        if missing:
            sys.exit(f"{exp}: catalog rows missing for {missing}")
        spatial = {catalog[k]["spatial"] for k in keys}
        if len(spatial) != 1:
            sys.exit(f"{exp}: one job spans spatial data types {sorted(spatial)}")
        domains = list(dict.fromkeys(catalog[k]["domain"] for k in keys))
        table.append({
            "spatial": spatial.pop(),
            "domain": "; ".join(domains),
            "source": label,
            "n_vars": str(sum(int(catalog[k]["n_value_cols"]) for k in keys)),
            "example": "; ".join(catalog[k]["example"] for k in keys),
            "cold_1k_s": cell("1k", exp, "cold", "wall_median_s", seconds),
            "cold_10k_s": cell("10k", exp, "cold", "wall_median_s", seconds),
            "cold_100k_s": cell("100k", exp, "cold", "wall_median_s", seconds),
            "c3_100k_s": cell("100k", exp, "cold", "c3_median_s", seconds, stage=True),
            "c4_100k_s": cell("100k", exp, "cold", "c4_median_s", seconds, stage=True),
            "warm_1k_s": cell("1k", exp, "warm", "wall_median_s", seconds),
            "warm_10k_s": cell("10k", exp, "warm", "wall_median_s", seconds),
            "warm_100k_s": cell("100k", exp, "warm", "wall_median_s", seconds),
            "peak_10k_gb": cell("10k", exp, "cold", "peak_rss_median_gb", lambda x: f"{float(x):.1f}"),
            "peak_100k_gb": cell("100k", exp, "cold", "peak_rss_median_gb", lambda x: f"{float(x):.1f}"),
        })
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(table[0].keys()))
        w.writeheader(); w.writerows(table)
    print(f"{len(table)} rows -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
