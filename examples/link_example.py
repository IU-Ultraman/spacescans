#!/usr/bin/env python3
"""Link a synthetic cohort to a catalog source and to two registered datasets, then check it.

The sections marked ``# --- S2.n <title> ---`` are the worked example; the rest of this file runs
them reproducibly and checks their results independently:

1. The example runs against the data directory in ``$SPACESCANS_DATA_DIR`` (or ``--data-dir``),
   with the dataset registry in a new temporary directory (``--registry`` to choose one), so
   your own registry is never touched and names never clash.
2. The checks recompute the results: participant-level values equal the contributing-day-weighted
   mean of the location-episode values, the county table's location-episode values equal the
   day-weighted county values (every 270 m buffer lies at least 13 km inside one county), and
   the distances equal nearest distances computed here in EPSG:5070.
3. Outputs go to examples/outputs (``--out`` to choose another directory): the linked tables
   (python_*.csv), what the example printed (python_console.txt, and per section in
   python_console_sections.json), the checks (python_checks.csv) and the run facts
   (python_run.csv). The exit code is 1 when any check fails.

Requires the spacescans package with its ``geo`` extra. The first run downloads the NPS
Soundscape Noise rasters (about 1.2 GB) and the 2010 county boundaries (about 75 MB) from the
project mirror into the data directory. Synthetic data only.

    SPACESCANS_DATA_DIR=~/spacescans-data python examples/link_example.py
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "outputs"


class _Tee:
    """Writes to the terminal and keeps what each section prints."""

    def __init__(self, stream):
        self.stream = stream
        self.sections: dict[str, list[str]] = {}
        self.current: str | None = None

    def section(self, name: str) -> None:
        self.current = name
        self.sections.setdefault(name, [])

    def write(self, text):
        self.stream.write(text)
        if self.current is not None:
            self.sections[self.current].append(text)
        return len(text)

    def flush(self):
        self.stream.flush()


ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("--data-dir", help="data directory (default: $SPACESCANS_DATA_DIR)")
ap.add_argument("--registry", help="dataset registry directory (default: a new temporary one)")
ap.add_argument("--out", default=str(OUT), help="output directory (default: examples/outputs)")
args = ap.parse_args()
if args.data_dir:
    os.environ["SPACESCANS_DATA_DIR"] = str(Path(args.data_dir).expanduser())
if not os.environ.get("SPACESCANS_DATA_DIR"):
    sys.exit("set SPACESCANS_DATA_DIR (or pass --data-dir): the example downloads data into it")
registry = Path(args.registry).expanduser() if args.registry else Path(
    tempfile.mkdtemp(prefix="spacescans-example-registry-"))
os.environ["SPACESCANS_DATASETS_DIR"] = str(registry)
out_dir = Path(args.out)
out_dir.mkdir(parents=True, exist_ok=True)
tee = _Tee(sys.__stdout__)
sys.stdout = tee

# === harness: keep what each section prints ===
tee.section("S2.1")
# --- S2.1 A synthetic cohort of location episodes ---
import pandas as pd

cohort = pd.DataFrame({
    "pid":       ["P1", "P1", "P2", "P3", "P3"],
    "startDate": ["2013-01-01", "2015-07-01", "2014-03-15", "2013-05-01", "2016-01-01"],
    "endDate":   ["2015-06-30", "2019-12-31", "2018-09-30", "2014-12-31", "2019-06-30"],
    "longitude": [-86.1581, -87.6359, -86.5264, -82.9988, -86.1600],
    "latitude":  [39.7684, 41.8789, 39.1653, 39.9612, 39.8100],
})

# === harness ===
tee.section("S2.2")
# --- S2.2 Catalog entry, data, and coverage before linkage ---
from spacescans import catalog, custom, data
from spacescans.orchestrate import link

noise = catalog.variable("noise")
print(noise.label, "|", noise.temporal, "|", ", ".join(noise.value_cols))
data.fetch(["noise"])                    # into $SPACESCANS_DATA_DIR, once
coverage = catalog.coverage(cohort, ["noise"])
print("location episodes covered:", coverage["variables"]["noise"]["coverage_pct"], "%")

# === harness ===
tee.section("S2.3")
# --- S2.3 Participant-level linkage (the default) ---
result = link(cohort, ["noise"], buffer_m=270)
print(result.exposures)                  # one row per participant: pid and the noise columns
print(result.coverage)                   # linkage rates: share of participants with a value
# result.features: the feature dictionary; result.provenance: versions, settings, data receipts

# === harness ===
tee.section("S2.4")
# --- S2.4 Location-episode output (the option) ---
episodes = link(cohort, ["noise"], buffer_m=270, output_level="episode")
print(episodes.exposures)                # one row per location episode, in input order

# === harness ===
tee.section("S2.5")
# --- S2.5 Register user-supplied datasets once, then link them by name ---
import json
import tempfile
from pathlib import Path

work = Path(tempfile.mkdtemp())

# A yearly table keyed by 2010 county FIPS code (synthetic values)
heat_days = {"18097": [8, 11, 9, 14, 10, 12, 7],     # Marion County, IN
             "17031": [5, 7, 6, 9, 8, 6, 4],         # Cook County, IL
             "18105": [10, 12, 9, 15, 11, 13, 8],    # Monroe County, IN
             "39049": [7, 9, 8, 12, 9, 11, 6]}       # Franklin County, OH
heat = pd.DataFrame([{"county_fips": fips, "year": year, "heat_days": days}
                     for fips, values in heat_days.items()
                     for year, days in zip(range(2013, 2020), values)])
heat.to_csv(work / "county_heat.csv", index=False)

# Point features (synthetic clinic sites), linked as the distance to the nearest one
sites = [("C1", -86.1500, 39.7750), ("C2", -87.6250, 41.8850), ("C3", -86.5350, 39.1700),
         ("C4", -83.0100, 39.9700), ("C5", -86.1700, 39.8200)]
clinics = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"site": site},
     "geometry": {"type": "Point", "coordinates": [lon, lat]}} for site, lon, lat in sites]}
(work / "clinics.geojson").write_text(json.dumps(clinics))

custom.register(work / "county_heat.csv", name="county_heat", kind="polygon_table",
                temporal="yearly",
                key={"boundary": "county", "column": "county_fips", "year_column": "year"},
                variables=[{"name": "heat_days", "unit": "days",
                            "description": "Extreme-heat days per year (synthetic)"}],
                metadata={"source": "Synthetic example", "version": "1",
                          "description": "County table for the Supplement (synthetic)"})
custom.register(work / "clinics.geojson", name="clinics", kind="features", temporal="static",
                metadata={"source": "Synthetic example", "version": "1",
                          "description": "Clinic sites for the Supplement (synthetic)"})
for record in sorted(custom.list_datasets(), key=lambda r: r.name):   # listed by name
    print(record.name, "|", record.kind, "|", record.temporal, "|", record.visibility, "|",
          ", ".join(v["name"] for v in record.variables))

data.fetch(datasets=["county_boundaries"])   # the 2010 county polygons the table is keyed to
later = link(cohort, ["noise"], buffer_m=270, custom=["county_heat", "clinics"])
print(later.exposures)                   # noise, heat_days and clinics_dist_m per participant

# === Checks ==================================================
import numpy as np  # noqa: E402

sys.stdout = sys.__stdout__
printed = {name: "".join(parts) for name, parts in tee.sections.items()}
(out_dir / "python_console.txt").write_text("".join(printed.values()), encoding="utf-8")
(out_dir / "python_console_sections.json").write_text(json.dumps(printed, indent=1) + "\n",
                                                      encoding="utf-8")
checks: list[dict] = []


def check(name: str, got, want, tol: float) -> None:
    if got is None:
        checks.append({"check": name, "passed": False, "max_abs_diff": float("nan"),
                       "tolerance": tol, "values": 0})
        print(f"FAIL {name}: column missing from the output")
        return
    got, want = np.asarray(got, dtype=float), np.asarray(want, dtype=float)
    same_missing = bool(np.array_equal(np.isnan(got), np.isnan(want)))
    diff = float(np.nanmax(np.abs(got - want))) if np.isfinite(got).any() else 0.0
    ok = same_missing and diff <= tol
    checks.append({"check": name, "passed": ok, "max_abs_diff": diff, "tolerance": tol,
                   "values": int(np.isfinite(want).sum())})
    print(("PASS " if ok else "FAIL ") + f"{name}: max |diff| {diff:.3g} (tolerance {tol:g})")


starts = pd.to_datetime(cohort["startDate"])
ends = pd.to_datetime(cohort["endDate"])
days = ((ends - starts).dt.days + 1).to_numpy()          # start and end inclusive
order = list(dict.fromkeys(cohort["pid"]))                # participants, first appearance


def column(table, c):
    """A column of an output table, or None (reported as a failed check) when it is absent."""
    return table[c] if c in table.columns else None


def episode_table(res, cols):
    """Location-episode output in input order, with the cohort's pid (absent columns: NaN)."""
    t = res.exposures.sort_values("episode_id").reset_index(drop=True)
    if list(t["episode_id"]) != list(range(len(cohort))):
        raise SystemExit("episode-level output is not one row per input row")
    missing = [c for c in cols if c not in t.columns]
    if missing:
        print(f"FAIL location-episode output lacks column(s) {missing}; "
              f"it has {list(t.columns)}")
    return pd.concat([cohort[["pid"]], t.reindex(columns=cols)], axis=1)


def participant_reference(ep, cols):
    """The article's participant-level formula from location-episode values: every source
    here has a value on every day of an episode or on none (time-invariant sources, and a
    yearly table covering 2013-2019), so an episode's contributing days are its duration when
    its value is defined."""
    rows = []
    for pid in order:
        sel = (ep["pid"] == pid).to_numpy()
        row = {"pid": pid}
        for c in cols:
            x = ep.loc[sel, c].to_numpy(dtype=float)
            d = np.where(np.isnan(x), 0, days[sel])
            row[c] = float(np.nansum(d * np.nan_to_num(x)) / d.sum()) if d.sum() else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def heat_reference():
    """Day-weighted county values per location episode (each buffer lies in one county)."""
    county = ["18097", "17031", "18105", "39049", "18097"]
    out = []
    for e in range(len(cohort)):
        num = den = 0.0
        for year in range(starts[e].year, ends[e].year + 1):
            lo = max(starts[e], pd.Timestamp(year, 1, 1))
            hi = min(ends[e], pd.Timestamp(year, 12, 31))
            d = (hi - lo).days + 1
            num += d * heat_days[county[e]][year - 2013]
            den += d
        out.append(num / den)
    return np.array(out)


def distance_reference():
    """Nearest distance in EPSG:5070 from each location to the clinic sites."""
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    px, py = tr.transform(cohort["longitude"].to_numpy(), cohort["latitude"].to_numpy())
    sx, sy = tr.transform([s[1] for s in sites], [s[2] for s in sites])
    return np.array([min(np.hypot(x - a, y - b) for a, b in zip(sx, sy)) for x, y in zip(px, py)])


noise_cols = list(noise.value_cols)
part = result.exposures
if list(part["pid"]) != order:
    raise SystemExit(f"participant rows are not one per participant in first-appearance order: "
                     f"{len(part)} rows")
ep_noise = episode_table(episodes, noise_cols)
ref = participant_reference(ep_noise, noise_cols)
for c in noise_cols:
    check(f"noise participant {c} = day-weighted mean of location-episode values",
          column(part, c), ref[c], 1e-9 * max(1.0, float(np.nanmax(np.abs(ref[c])))))

later_cols = noise_cols + ["heat_days", "clinics_dist_m"]
episodes_later = link(cohort, ["noise"], buffer_m=270, custom=["county_heat", "clinics"],
                      output_level="episode")
ep_later = episode_table(episodes_later, later_cols)
check("county table, location-episode heat_days = day-weighted county values",
      ep_later["heat_days"], heat_reference(), 1e-6)
check("features, location-episode clinics_dist_m = nearest distance in EPSG:5070 (m)",
      ep_later["clinics_dist_m"], distance_reference(), 1.0)
if list(later.exposures["pid"]) != order:
    raise SystemExit("registered-dataset link: participant rows are not one per participant in "
                     "first-appearance order")
ref_later = participant_reference(ep_later, later_cols)
for c in later_cols:
    check(f"registered link participant {c} = day-weighted mean of location-episode values",
          column(later.exposures, c), ref_later[c],
          1e-9 * max(1.0, float(np.nanmax(np.abs(ref_later[c].fillna(0))))))
for c in noise_cols:
    check(f"noise {c} unchanged when registered datasets are linked too",
          column(later.exposures, c), part[c], 1e-9 * max(1.0, float(np.nanmax(np.abs(part[c])))))

# ---- outputs
import platform  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import spacescans  # noqa: E402

part.to_csv(out_dir / "python_noise_participant.csv", index=False)
result.coverage.to_csv(out_dir / "python_noise_linkage_rates.csv", index=False)
ep_noise.to_csv(out_dir / "python_noise_location_episode.csv", index=False)
later.exposures.to_csv(out_dir / "python_registered_participant.csv", index=False)
ep_later.to_csv(out_dir / "python_registered_location_episode.csv", index=False)
pd.DataFrame(checks).to_csv(out_dir / "python_checks.csv", index=False)
pd.DataFrame([{"spacescans_version": getattr(spacescans, "__version__", "unknown"),
               "python": platform.python_version(), "platform": platform.platform(),
               "os": (f"macOS {platform.mac_ver()[0]}" if sys.platform == "darwin"
                      else platform.platform(terse=True)),
               "machine": platform.machine(),
               "run_date": datetime.now().astimezone().date().isoformat(),
               "run_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "checks_passed": sum(c["passed"] for c in checks), "checks": len(checks)}]
             ).to_csv(out_dir / "python_run.csv", index=False)
if not args.registry:
    shutil.rmtree(registry, ignore_errors=True)
shutil.rmtree(work, ignore_errors=True)
failed = [c["check"] for c in checks if not c["passed"]]
print(f"{len(checks) - len(failed)} of {len(checks)} checks passed; outputs in {out_dir}")
sys.exit(1 if failed else 0)
