"""Regenerate the web-parity fixtures by running the ORIGINAL web functions.

The fixtures under this directory pin what spacescans-web's own code does,
so the ports in spacescans.cohort and spacescans.orchestrate can be checked
against it in CI without the web repository.

    <python> -I tests/fixtures/web_parity/generate.py \
        --web-backend <dir containing the web's app/ package> \
        --web-commit <sha>

Run it with an interpreter that has the web backend's dependencies
(pandas, pyarrow, numpy, pydantic-settings, jsonschema, pyyaml) and NOT
spacescans: the fixtures must come from the web's code alone. Use the same
pandas/pyarrow/numpy versions as the test environment if parquet bytes are
to be compared; the versions and platform used are recorded in manifest.json
and the tests compare decoded tables instead when they differ.

Every value here is synthetic: random coordinates, dates and exposure
values with the real column names and dtypes of the web's C4 outputs. No
real exposure data and no derived values from licensed sources.

What is produced (all paths relative to this directory):
  cohorts/*.csv                  edge-case cohorts (inputs)
  (no catalog copy: the web catalog the runs used is the package's
   spacescans/catalog/variables.json; manifest.json records its sha256)
  overlay.json                   synthetic custom entries (task overlay)
  merge/<case>/                  input.csv, output/*.parquet, case.json
  fan_in/<case>/                 input.csv, output/result_<exp>.csv, case.json
  expected/parquet/              csv_to_parquet: <cohort>__<variant>.json, and
                                 blobs/<sha256[:16]>.parquet (one per content)
  expected/upload/<cohort>.json  save_upload: summary, rewritten input.csv, error
  expected/coverage/<case>.json  compute_coverage output or error
  expected/merge/<case>/         write_partial: result_<exp>.csv, warnings.json,
                                 rewritten parquet (FAQSD)
  expected/fan_in/<case>/        fan_in: result.csv
  manifest.json                  provenance: web commit, file hashes, versions
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import io
import json
import os
import platform
import random
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SEED = 20261012

# ---------------------------------------------------------------------------
# Synthetic cohorts
# ---------------------------------------------------------------------------

# Points outside CONUS (and one inside the box but outside the US).
_OUTSIDE = [
    (-149.900278, 61.218056),   # Anchorage, AK
    (-157.858333, 21.306944),   # Honolulu, HI
    (-66.105735, 18.466333),    # San Juan, PR
    (-79.383184, 43.653226),    # Toronto: inside the CONUS box, not in the US
]


def _episodes(rng: random.Random, n_patients: int, *, start_year: int = 2009,
              end_year: int = 2021, outside_every: int = 9):
    rows = []
    k = 0
    for p in range(1, n_patients + 1):
        n_ep = rng.choice([1, 1, 2, 2, 3, 4])
        day = datetime(rng.randint(start_year, end_year - 2), rng.randint(1, 12),
                       rng.randint(1, 28))
        for _ in range(n_ep):
            length = rng.randint(30, 900)
            end = day + timedelta(days=length)
            k += 1
            if k % outside_every == 0:
                lon, lat = _OUTSIDE[(k // outside_every) % len(_OUTSIDE)]
            else:
                lon = round(rng.uniform(-124.0, -67.5), 6)
                lat = round(rng.uniform(25.0, 49.0), 6)
            rows.append({"pid": f"P{p:07d}", "start": day, "end": end,
                         "longitude": lon, "latitude": lat})
            day = end + timedelta(days=1)
    return rows


def _iso(d):
    return d.strftime("%Y-%m-%d")


def _us(d):
    return f"{d.month}/{d.day}/{d.year}"


def _write_csv(path: Path, header, rows, *, newline="\n", bom=False):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator=newline)
    w.writerow(header)
    w.writerows(rows)
    data = buf.getvalue().encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)


def make_cohorts(out: Path) -> dict[str, Path]:
    rng = random.Random(SEED)
    out.mkdir(parents=True, exist_ok=True)
    base = _episodes(rng, 25)
    H = ["pid", "startDate", "endDate", "longitude", "latitude"]

    def rows(fmt=_iso, pid=lambda r: r["pid"]):
        return [[pid(r), fmt(r["start"]), fmt(r["end"]), r["longitude"], r["latitude"]]
                for r in base]

    files: dict[str, Path] = {}

    def put(name, header, body, **kw):
        files[name] = out / f"{name}.csv"
        _write_csv(files[name], header, body, **kw)

    put("iso", H, rows())
    put("us_slash", H, rows(_us))
    put("crlf", H, rows(), newline="\r\n")
    put("bom", H, rows(), bom=True)

    # Numeric ids, half of them zero-padded: pandas infers int64 and drops
    # the zeros in the legacy path.
    def num_pid(r):
        n = int(r["pid"][1:])
        return f"{1000 + n}" if n % 2 else f"{n:05d}"
    put("numeric_pid", H, rows(pid=num_pid))

    fips_rows = []
    states = ["06", "36", "01", "48", "17"]
    for i, r in enumerate(base):
        st = states[i % len(states)]
        county = f"{st}{(i * 7) % 200:03d}"
        tract = f"{county}{(i * 131) % 999999:06d}"
        bg = f"{tract}{i % 9}"
        zcta = f"{(i * 1373) % 99999:05d}"
        cells = [st, county, tract, bg, zcta]
        if i % 11 == 3:
            cells = ["", "", "", "", ""]       # blank FIPS on some rows
        fips_rows.append([r["pid"], _iso(r["start"]), _iso(r["end"]),
                          r["longitude"], r["latitude"]] + cells)
    put("fips", H + ["state_fips", "county_fips", "tract_geoid", "bg_geoid", "zcta5"],
        fips_rows)

    ep_rows = [[r[0], (i * 37) % 5] + r[1:] for i, r in enumerate(rows())]
    put("episode_id_col", ["pid", "episode_id"] + H[1:], ep_rows)

    blank = rows()
    blank[2][2] = ""                 # blank endDate
    blank[5][2] = ""
    blank[7][1] = ""                 # blank startDate
    blank[9][1] = f" {blank[9][1]} "  # padded with spaces
    blank[10][2] = f"{blank[10][2]}  "
    blank[12] = blank[12][:2]        # short row: no endDate, no coordinates
    put("blank_dates", H, blank)

    put("all_blank_dates", H, [[r[0], "", "", r[3], r[4]] for r in rows()])
    put("patid_upper", ["PATID"] + H[1:], rows())
    put("patid_mixed", ["PatID"] + H[1:], rows())
    put("both_ids", ["pid", "PATID"] + H[1:],
        [[r[0], f"X{i}"] + r[1:] for i, r in enumerate(rows())])
    put("missing_pid", ["subject"] + H[1:], rows())
    put("missing_lat", H[:4], [r[:4] for r in rows()])

    bad = rows()
    bad[0][1] = "19/8/2017"
    put("bad_format", H, bad)
    mixed = [[r[0], r[1], _us(b["end"]), r[3], r[4]] for r, b in zip(rows(), base)]
    put("mixed_formats", H, mixed)
    put("datetime_values", H,
        [[r[0], r[1] + " 00:00:00", r[2] + " 00:00:00", r[3], r[4]] for r in rows()])
    put("empty", H, [])

    notes = ["plain", "has, comma", 'has "quotes"', "Zoë", "São Paulo", ""]
    extra = [r + [20 + i % 60, "FM"[i % 2], notes[i % len(notes)], round(0.1 * i, 3)]
             for i, r in enumerate(rows())]
    put("extra_cols", H + ["age", "sex", "notes", "ndi"], extra)

    rng2 = random.Random(SEED + 1)
    outside = []
    for i, r in enumerate(rows()):
        if i % 3:
            lon, lat = _OUTSIDE[i % len(_OUTSIDE)]
            r = r[:3] + [lon + rng2.uniform(-0.1, 0.1), lat + rng2.uniform(-0.1, 0.1)]
        outside.append(r)
    put("outside", H, outside)

    rng3 = random.Random(SEED + 2)
    old = _episodes(rng3, 10, start_year=2000, end_year=2011)
    put("old_dates", H, [[r["pid"], _iso(r["start"]), _iso(r["end"]),
                          r["longitude"], r["latitude"]] for r in old])
    return files


# ---------------------------------------------------------------------------
# Synthetic C4 outputs (real column names and dtypes, random values)
# ---------------------------------------------------------------------------


def _c4(pd, np, keys, cols, rng, *, drop_every=0, nan_every=0, shuffle=True,
        patid_dtype="str"):
    """A C4 output for the cohort keys: PATID, geoid, value columns."""
    recs = []
    for i, (pid, eid) in enumerate(keys):
        if drop_every and i % drop_every == drop_every - 1:
            continue
        rec = {"PATID": pid, "geoid": eid}
        for j, c in enumerate(cols):
            if nan_every and (i + j) % nan_every == 0:
                rec[c] = float("nan")
            else:
                rec[c] = rng.uniform(-3.0, 250.0)
        recs.append(rec)
    if shuffle:
        rng.shuffle(recs)
    df = pd.DataFrame(recs, columns=["PATID", "geoid"] + list(cols))
    df["geoid"] = df["geoid"].astype("int64")
    if patid_dtype == "int64":
        df["PATID"] = df["PATID"].astype("int64")
    for c in cols:
        df[c] = df[c].astype("float64")
    return df


def make_merge_cases(pd, np, out: Path, cohorts: dict[str, Path], meta: dict,
                     overlay: dict):
    rng = random.Random(SEED + 10)
    out.mkdir(parents=True, exist_ok=True)
    cases = {}

    def keys_of(cohort_name, pid_numeric=False):
        df = pd.read_csv(cohorts[cohort_name], dtype=str)
        pids = df["pid"].tolist()
        if pid_numeric:
            pids = [int(p) for p in pids]
        return list(zip(pids, range(len(pids))))

    def vc(var):
        entry = overlay.get(var) or meta["variables"][var]
        return entry["value_cols"]

    def case(name, experiment, variables, outputs, *, cohort="iso",
             renames=None, rewrite=False, custom=False):
        d = out / name
        (d / "output").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cohorts[cohort], d / "input.csv")
        parquet_map = {}
        for var, (fname, df) in outputs.items():
            df.to_parquet(d / "output" / fname, index=False)
            parquet_map[var] = fname
        spec = {"experiment": experiment, "variables": variables,
                "parquet_map": parquet_map, "renames": renames or {},
                "rewrite_in_place": rewrite, "cohort": cohort,
                "uses_overlay": custom}
        (d / "case.json").write_text(json.dumps(spec, indent=2) + "\n")
        cases[name] = spec

    k = keys_of("iso")
    ndi = _c4(pd, np, k, vc("ndi"), rng, drop_every=13, nan_every=17)
    wi = _c4(pd, np, k, vc("walkability"), rng, drop_every=7)
    case("bg_ndi_wi", "bg_ndi_wi", ["ndi", "walkability"],
         {"ndi": ("c4_ndi.parquet", ndi), "walkability": ("c4_wi.parquet", wi)})
    case("bg_ndi_wi_reversed", "bg_ndi_wi", ["walkability", "ndi"],
         {"ndi": ("c4_ndi.parquet", ndi), "walkability": ("c4_wi.parquet", wi)})

    # The FAQSD reader emits o3/pm25; the web renames them in the parquet.
    faq = _c4(pd, np, k, ["o3", "pm25"], rng, nan_every=19, shuffle=False)
    case("faqsd", "faqsd", ["faqsd"], {"faqsd": ("c4_tract_faqsd.parquet", faq)},
         renames={"o3": "faqsd_o3", "pm25": "faqsd_pm25"}, rewrite=True)

    fara_cols = ["LAPOP1_10share", "LILATracts_1And10", "LA1and10", "LATracts1",
                 "LATracts10", "HUNVFlag", "GroupQuartersFlag", "LowIncomeTracts"]
    fara = _c4(pd, np, k, fara_cols, rng, shuffle=False)
    case("fara_tract", "fara_tract", ["fara_tract"],
         {"fara_tract": ("c4_tract_fara.parquet", fara)})

    # Hash-ordered single variable, a third of the episodes missing: the
    # partial keeps C4 order and the match falls below 90%.
    noise = _c4(pd, np, k, vc("noise"), rng, drop_every=3)
    case("noise_low_match", "noise", ["noise"], {"noise": ("c4_noise.parquet", noise)})

    cbp_cols = vc("cbp_zcta5") + ["r_unused", "n_establishments"]
    cbp = _c4(pd, np, k, cbp_cols, rng, nan_every=5)
    case("zcta5_cbp", "zcta5_cbp", ["cbp_zcta5"],
         {"cbp_zcta5": ("c4_county_cbp.parquet", cbp)})

    tiger = _c4(pd, np, k, vc("tiger_proximity") + ["dist_unused"], rng)
    case("tiger_proximity", "tiger_proximity", ["tiger_proximity"],
         {"tiger_proximity": ("c4_tiger_roads.parquet", tiger)})

    acag = _c4(pd, np, k, vc("acag"), rng, nan_every=4, drop_every=29)
    case("acag", "acag", ["acag"], {"acag": ("c4_acag.parquet", acag)})

    # Two custom variables that both produce `val`: the first one wins.
    ca = _c4(pd, np, k, ["val", "val_a"], rng, drop_every=5)
    cb = _c4(pd, np, k, ["val", "val_b"], rng, drop_every=4)
    case("custom_dup_cols", "custom", ["custom_aaaa1111", "custom_bbbb2222"],
         {"custom_aaaa1111": ("c4_custom_aaaa1111.parquet", ca),
          "custom_bbbb2222": ("c4_custom_bbbb2222.parquet", cb)}, custom=True)

    case("no_variables", "noise", [], {})

    # Numeric ids: input.csv is read as text, the C4 PATID is int64. The web
    # merge fails on the dtype mismatch; the fixture records that.
    kn = keys_of("numeric_pid", pid_numeric=True)
    num = _c4(pd, np, kn, vc("vnl"), rng, patid_dtype="int64")
    case("numeric_pid", "vnl", ["vnl"], {"vnl": ("c4_vnl.parquet", num)},
         cohort="numeric_pid")

    # Cohort with passthrough columns, including one named `ndi`.
    kx = keys_of("extra_cols")
    case("extra_cols_vnl", "vnl", ["vnl"],
         {"vnl": ("c4_vnl.parquet", _c4(pd, np, kx, vc("vnl"), rng, drop_every=6))},
         cohort="extra_cols")
    return cases


# ---------------------------------------------------------------------------
# Running the web code
# ---------------------------------------------------------------------------


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _error(e: BaseException) -> dict:
    return {"type": type(e).__name__, "message": str(e),
            "args0": str(e.args[0]) if e.args else None}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--web-backend", required=True, type=Path,
                    help="directory holding the web's app/ package")
    ap.add_argument("--web-commit", required=True)
    ap.add_argument("--out", type=Path, default=HERE)
    args = ap.parse_args(argv)

    out: Path = args.out
    for sub in ("cohorts", "merge", "fan_in", "expected"):
        shutil.rmtree(out / sub, ignore_errors=True)

    work = Path(tempfile.mkdtemp(prefix="ss-web-parity-"))
    os.environ["DATA_DIR"] = str(work / "data")
    os.environ["TASKS_DIR"] = str(work / "data" / "tasks")
    os.environ["DB_PATH"] = str(work / "data" / "t.db")
    (work / "data" / "tasks").mkdir(parents=True)
    sys.path.insert(0, str(args.web_backend.resolve()))

    import numpy as np
    import pandas as pd
    import pyarrow

    import app.config
    importlib.reload(app.config)
    from app import cohort_dates  # noqa: F401
    import app.task_manager as tm
    import app.variable_registry as vr
    from app.experiments import _merge, bg_ndi_wi, zcta5_cbp

    # The registry's version probe and data pre-flights need the pipeline and
    # a data root; neither matters to the merge or coverage logic. Schema
    # validation, the catalog JSON and the task overlay stay real.
    vr._assert_pipeline_version_compatible = lambda: None
    for name in dir(vr):
        if name.startswith("_assert_") and name.endswith("_data_present"):
            setattr(vr, name, lambda payload: None)

    web_app = args.web_backend / "app"
    meta = json.loads((web_app / "data" / "variable_metadata.json").read_text())

    # Synthetic task overlay: two custom polygons, one static dataset with a
    # region other than CONUS (the web never makes one; it exercises the
    # all-points-inside branch) and one yearly raster.
    overlay = {
        "custom_aaaa1111": {
            "label": "Synthetic custom A", "boundary": "Tract",
            "spatial_method": "areal", "coverage_years": [2014, 2018],
            "coverage_region": "CONUS", "experiment": "custom",
            "temporal": "yearly", "variable_type": "continuous",
            "display_unit": "units", "value_cols": ["val", "val_a"]},
        "custom_bbbb2222": {
            "label": "Synthetic custom B", "boundary": "BG",
            "spatial_method": "areal", "coverage_years": [2013, 2019],
            "coverage_region": "CONUS", "experiment": "custom",
            "temporal": "static", "variable_type": "continuous",
            "display_unit": "units", "value_cols": ["val", "val_b"]},
        "custom_cccc3333": {
            "label": "Synthetic global static", "boundary": "Point",
            "spatial_method": "grid", "coverage_years": [2013, 2019],
            "coverage_region": "Global", "experiment": "custom",
            "temporal": "static", "variable_type": "continuous",
            "display_unit": "units", "value_cols": ["g"]},
        "custom_dddd4444": {
            "label": "Synthetic yearly, no temporal key", "boundary": "Point",
            "spatial_method": "grid", "coverage_years": [2016, 2017],
            "coverage_region": "Puerto Rico", "experiment": "custom",
            "variable_type": "continuous",
            "display_unit": "units", "value_cols": ["h"]},
    }
    (out / "overlay.json").write_text(json.dumps(overlay, indent=2) + "\n")

    cohorts = make_cohorts(out / "cohorts")
    exp_dir = out / "expected"

    # -- csv_to_parquet, both variants ------------------------------------
    pq_dir = exp_dir / "parquet"
    pq_dir.mkdir(parents=True)
    runners = {"bg_ndi_wi": bg_ndi_wi.csv_to_parquet,
               "zcta5": zcta5_cbp.csv_to_parquet}
    # Parquet files are stored once per content: blobs/<sha256[:16]>.parquet.
    (pq_dir / "blobs").mkdir()
    for name, src in sorted(cohorts.items()):
        for variant, fn in runners.items():
            dst = work / f"{name}__{variant}.parquet"
            rec = {"cohort": name, "variant": variant}
            try:
                fn(src, dst)
            except Exception as e:  # noqa: BLE001 — record what the web raises
                rec["error"] = _error(e)
            else:
                t = pyarrow.parquet.read_schema(dst)
                rec["sha256"] = _sha256_file(dst)
                rec["blob"] = f"blobs/{rec['sha256'][:16]}.parquet"
                rec["schema"] = [[f.name, str(f.type)] for f in t]
                rec["rows"] = pyarrow.parquet.read_metadata(dst).num_rows
                shutil.copyfile(dst, pq_dir / rec["blob"])
            (pq_dir / f"{name}__{variant}.json").write_text(
                json.dumps(rec, indent=2) + "\n")

    # -- save_upload --------------------------------------------------------
    up_dir = exp_dir / "upload"
    up_dir.mkdir(parents=True)
    for name, src in sorted(cohorts.items()):
        task_id = f"up-{name}"
        tdir = app.config.settings.TASKS_DIR / f"task-{task_id}"
        tdir.mkdir(parents=True)
        (tdir / "meta.json").write_text(json.dumps({"id": task_id}))
        content = src.read_bytes()
        rec = {"cohort": name, "filename": f"{name}.csv"}
        try:
            summary = tm.save_upload(task_id, content, f"{name}.csv")
        except Exception as e:  # noqa: BLE001
            rec["error"] = _error(e)
        else:
            rec["summary"] = summary
            rec["meta_data_summary"] = json.loads(
                (tdir / "meta.json").read_text())["data_summary"]
        inp = tdir / "input.csv"
        rec["input_csv_exists"] = inp.exists()
        rec["input_csv_changed"] = inp.exists() and inp.read_bytes() != content
        rec["input_csv_text"] = (inp.read_text() if rec["input_csv_changed"] else None)
        (up_dir / f"{name}.json").write_text(
            json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # -- compute_coverage ----------------------------------------------------
    cov_dir = exp_dir / "coverage"
    cov_dir.mkdir(parents=True)
    all_vars = list(meta["variables"]) + list(overlay)

    def coverage_case(case_name, cohort_name, keys, *, with_input=True):
        task_id = f"cov-{case_name}"
        tdir = app.config.settings.TASKS_DIR / f"task-{task_id}"
        tdir.mkdir(parents=True)
        (tdir / "config.json").write_text(json.dumps({"custom_variables": overlay}))
        if with_input:
            shutil.copyfile(cohorts[cohort_name], tdir / "input.csv")
        rec = {"case": case_name, "cohort": cohort_name, "variables": keys,
               "with_input": with_input}
        try:
            rec["result"] = json.loads(json.dumps(tm.compute_coverage(task_id, keys)))
        except Exception as e:  # noqa: BLE001
            rec["error"] = _error(e)
        (cov_dir / f"{case_name}.json").write_text(
            json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for name in ("iso", "us_slash", "crlf", "bom", "fips", "numeric_pid",
                 "blank_dates", "all_blank_dates", "extra_cols", "outside",
                 "old_dates", "empty", "episode_id_col", "patid_upper"):
        coverage_case(name, name, all_vars)
    coverage_case("iso_subset_order", "iso", ["walkability", "ndi", "custom_cccc3333"])
    coverage_case("iso_duplicate_keys", "iso", ["ndi", "ndi"])
    coverage_case("unknown_keys", "iso", ["pm25", "ndi", "foo"])
    coverage_case("no_input", "iso", ["ndi"], with_input=False)
    coverage_case("bad_format", "bad_format", ["ndi"])
    coverage_case("mixed_formats", "mixed_formats", ["noise"])
    coverage_case("empty_unknown", "empty", ["ndi", "nope"])

    # -- write_partial ---------------------------------------------------------
    mcases = make_merge_cases(pd, np, out / "merge", cohorts, meta, overlay)
    mexp = exp_dir / "merge"
    partial_files: dict[str, Path] = {}
    for name, spec in sorted(mcases.items()):
        src_case = out / "merge" / name
        tdir = work / "merge" / name
        shutil.copytree(src_case, tdir)
        if spec["uses_overlay"]:
            (tdir / "config.json").write_text(
                json.dumps({"custom_variables": overlay}))
        rec = {"case": name}
        try:
            if spec["experiment"] == "faqsd":
                # faqsd.merge_results: in-place rename, then write_partial.
                from app.experiments import faqsd
                path = faqsd.merge_results(tdir, spec["variables"])
            else:
                path = _merge.write_partial(
                    task_dir=tdir, experiment_key=spec["experiment"],
                    variables=spec["variables"], parquet_map=spec["parquet_map"])
        except Exception as e:  # noqa: BLE001
            rec["error"] = _error(e)
        else:
            rec["partial"] = path.name
            d = mexp / name
            d.mkdir(parents=True)
            shutil.copyfile(path, d / path.name)
            partial_files[name] = d / path.name
            if spec["rewrite_in_place"]:
                for fname in spec["parquet_map"].values():
                    shutil.copyfile(tdir / "output" / fname, d / fname)
                    rec.setdefault("rewritten", {})[fname] = _sha256_file(d / fname)
        logs = tdir / "logs.jsonl"
        warnings = []
        if logs.exists():
            for line in logs.read_text().splitlines():
                r = json.loads(line)
                r.pop("ts", None)
                warnings.append(r)
        rec["warnings"] = warnings
        (mexp / name).mkdir(parents=True, exist_ok=True)
        (mexp / name / "result.json").write_text(json.dumps(rec, indent=2) + "\n")

    # -- fan_in ----------------------------------------------------------------
    fdir = out / "fan_in"
    fexp = exp_dir / "fan_in"

    def fan_case(name, cohort_name, parts: list[tuple[str, str]]):
        """parts: (experiment key, merge case whose partial to use)."""
        d = fdir / name
        (d / "output").mkdir(parents=True)
        shutil.copyfile(cohorts[cohort_name], d / "input.csv")
        for exp, mcase in parts:
            shutil.copyfile(partial_files[mcase], d / "output" / f"result_{exp}.csv")
        spec = {"cohort": cohort_name, "experiments": [e for e, _ in parts]}
        (d / "case.json").write_text(json.dumps(spec, indent=2) + "\n")
        tdir = work / "fan_in" / name
        shutil.copytree(d, tdir)
        rec = {"case": name}
        try:
            path = _merge.fan_in(tdir, spec["experiments"])
        except Exception as e:  # noqa: BLE001
            rec["error"] = _error(e)
        else:
            (fexp / name).mkdir(parents=True)
            shutil.copyfile(path, fexp / name / "result.csv")
        (fexp / name).mkdir(parents=True, exist_ok=True)
        (fexp / name / "result.json").write_text(json.dumps(rec, indent=2) + "\n")

    fan_case("multi_source", "iso", [("bg_ndi_wi", "bg_ndi_wi"), ("faqsd", "faqsd"),
                                     ("acag", "acag"), ("custom", "custom_dup_cols"),
                                     ("noise", "noise_low_match")])
    fan_case("reversed_order", "iso", [("noise", "noise_low_match"),
                                       ("bg_ndi_wi", "bg_ndi_wi_reversed")])
    fan_case("single", "iso", [("tiger_proximity", "tiger_proximity")])
    fan_case("none", "iso", [])
    # input.csv with a column named `ndi` and text with commas and quotes.
    fan_case("input_collision", "extra_cols", [("vnl", "extra_cols_vnl"),
                                               ("bg_ndi_wi", "bg_ndi_wi")])
    # The same partial joined twice: every value column collides.
    fan_case("same_twice", "iso", [("noise", "noise_low_match"),
                                   ("noise2", "noise_low_match")])
    fan_case("patid_header", "patid_upper", [("fara_tract", "fara_tract")])
    fan_case("episode_id_input", "episode_id_col", [("zcta5_cbp", "zcta5_cbp")])

    # -- provenance --------------------------------------------------------------
    used = ["cohort_dates.py", "experiments/_merge.py", "experiments/bg_ndi_wi.py",
            "experiments/zcta5_cbp.py", "experiments/faqsd.py", "task_manager.py",
            "variable_registry.py", "feature_dictionary.py",
            "data/variable_metadata.json", "data/variable_metadata.schema.json"]
    manifest = {
        "generator": "tests/fixtures/web_parity/generate.py",
        "web_repo": "spacescans-web",
        "web_commit": args.web_commit,
        "web_files_sha256": {f"backend/app/{u}": _sha256_file(web_app / u) for u in used},
        "python": sys.version.split()[0],
        "platform": f"{sys.platform}-{platform.machine()}",
        "pandas": pd.__version__,
        "pyarrow": pyarrow.__version__,
        "numpy": np.__version__,
        "seed": SEED,
        "generated_at": datetime.now(ZoneInfo("America/New_York")).isoformat(
            timespec="seconds"),
        "note": "All values are synthetic. Parquet bytes are comparable only "
                "with the same pandas and pyarrow versions on the same platform.",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.rmtree(work, ignore_errors=True)
    print(f"fixtures written to {out}")


if __name__ == "__main__":
    main()
