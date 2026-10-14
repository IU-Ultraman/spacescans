"""Gate A4 on the host: ``orchestrate.link`` against every golden web 0.2.7 run (opt-in, slow).

For each run in the goldens' manifest (the 11 catalog variables in 11 runs, plus the
raster and block-group CSV uploads; ``fanin_all`` is a fan-in, not a run) this calls
``orchestrate.link`` on the golden synth_1k cohort with the run's variables, in its
request order, buffer 270 m and raster_res_m 25, against the real data root, with a
fresh legacy-v1 weight cache (``bg_ndi_wi_rev`` reuses ``bg_ndi_wi``'s cache, as its
golden did, so both of its C3 steps are cache hits). Then it compares, with
``tests/golden/golden_compare.py`` at ``CROSS_ENV`` (keys and missing-value masks exact):

* every rendered ``pipeline_configs/*.yaml`` (A0 normalisation; same set of files);
* the runner-authored ``logs.jsonl`` lines (``runner_log_lines``);
* ``input.parquet`` as a frame, every C3, C4 and materialised-values table;
* the partial ``result_<exp>.csv`` (canonical order) and ``result.csv`` (input order),
  and ``feature_dictionary.csv`` byte for byte;
* the legacy cache keys and ``.meta.json`` fields (all but ``created_at`` and
  ``wall_clock_seconds``).

Host-versus-image substitutions, applied to the new side only (golden README rules 5-8):
the interpreter and CLI in the spawn line (``/opt/conda/bin/python /opt/conda/bin/spacescans``),
the data root (``/project``), the upload files' container paths, and the cohort sha
(``12269dbe``: the host's pandas embeds another version string in ``input.parquet``,
whose frame is asserted equal first). FAQSD's C4 table is compared after its recipe
renames, which the web writes back into the parquet and ``link`` applies in memory.

The data root is only read: before each run a stamp is touched in the output dir and
afterwards no file under the data root may be newer (macOS ``.DS_Store`` files, which
Finder writes, are reported but not counted). Wall time and each step's peak RSS go to
``a4_summary.json``.

Opt-in: ``SPACESCANS_A4=1``, ``SPACESCANS_GOLDEN_DIR`` (complete goldens) and
``SPACESCANS_REAL_DATA_DIR`` (the data root), read at import. ``SPACESCANS_A4_OUT`` keeps
the run directories there (default: a pytest temp dir); ``SPACESCANS_A4_ONLY=noise,vnl``
runs a subset. The runs take about half an hour (acag's C4 alone about 15 minutes) and run
one at a time.

The same test runs in an ephemeral container of the frozen image (optional SAME_ENV
check): ``SPACESCANS_A4_TOL=same`` compares at ``SAME_ENV`` and ``SPACESCANS_A4_PREFIX``
sets the steps' command prefix (the web's ``/opt/conda/bin/python /opt/conda/bin/spacescans``,
with this checkout's ``src`` on ``PYTHONPATH``).
"""
from __future__ import annotations

import importlib.util
import json
import os
import resource
import shutil
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_GOLDEN_ENV = os.environ.get("SPACESCANS_GOLDEN_DIR")
GOLDEN = Path(_GOLDEN_ENV) if _GOLDEN_ENV else None
_DATA_ENV = os.environ.get("SPACESCANS_REAL_DATA_DIR")
DATA = Path(_DATA_ENV) if _DATA_ENV else None
ENABLED = os.environ.get("SPACESCANS_A4") == "1"
_OUT_ENV = os.environ.get("SPACESCANS_A4_OUT")
_ONLY = [s for s in os.environ.get("SPACESCANS_A4_ONLY", "").split(",") if s]
#: "same" holds the run to SAME_ENV (in the frozen image), default CROSS_ENV (another env).
_TOL = os.environ.get("SPACESCANS_A4_TOL", "cross")
#: The steps' command prefix (default: this interpreter, ``python -I -u -m spacescans``);
#: in the frozen image the web's ``/opt/conda/bin/python /opt/conda/bin/spacescans``.
_PREFIX = os.environ.get("SPACESCANS_A4_PREFIX")

WEB_PREFIX = "/opt/conda/bin/python /opt/conda/bin/spacescans"
WEB_DATA = "/project"
WEB_SHA8 = "12269dbe"
DS_STORE = ".DS_Store"


def _manifest() -> dict:
    try:
        return json.loads((GOLDEN / "manifest.json").read_text())
    except (OSError, TypeError, ValueError):
        return {}


MANIFEST = _manifest() if GOLDEN is not None else {}
RUNS = [name for name, run in MANIFEST.get("runs", {}).items()
        if name != "fanin_all" and (GOLDEN / name / "output").is_dir()
        and (not _ONLY or name in _ONLY)]

READY = (ENABLED and GOLDEN is not None and bool(RUNS)
         and DATA is not None and DATA.is_dir())

pytestmark = [
    pytest.mark.slow,
    pytest.mark.geo,
    pytest.mark.extras,
    pytest.mark.skipif(not READY, reason="gate A4 is opt-in: set SPACESCANS_A4=1, "
                                         "SPACESCANS_GOLDEN_DIR (complete goldens) and "
                                         "SPACESCANS_REAL_DATA_DIR (the data root)"),
    pytest.mark.skipif(os.name != "posix", reason="POSIX only"),
]


def _golden_compare():
    if "golden_compare" in sys.modules:
        return sys.modules["golden_compare"]
    spec = importlib.util.spec_from_file_location(
        "golden_compare", REPO / "tests" / "golden" / "golden_compare.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["golden_compare"] = module
    spec.loader.exec_module(module)
    return module


def _newer_files(root: Path, stamp: Path) -> list[str]:
    """Files under ``root`` modified after ``stamp`` (``find root -newer stamp``)."""
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


class A4:
    """Runs ``link`` once per golden run (memoised) and keeps what the checks need."""

    def __init__(self, out: Path):
        self.out = out
        self.data = DATA.resolve()
        self.inputs = out / "inputs"
        self.inputs.mkdir(parents=True, exist_ok=True)
        self.cohort_csv = GOLDEN / "inputs" / "synth_1k.csv"
        self.done: dict[str, dict] = {}
        self.summary_path = out / "a4_summary.json"
        self.summary: dict = {"runs": {}}
        self.global_stamp = out / "a4.stamp"
        self.global_stamp.touch()
        time.sleep(0.01)

    # -- configuration --------------------------------------------------------

    def custom_for(self, name: str) -> tuple[dict | None, list[tuple[str, str]]]:
        """The run's upload definitions with host paths, and (host, container) path pairs."""
        cfg = json.loads((GOLDEN / name / "config.json").read_text())
        defs = cfg.get("custom_variables")
        if not defs:
            return None, []
        subs: list[tuple[str, str]] = []
        out = {}
        for key, d in defs.items():
            d = json.loads(json.dumps(d))
            if d.get("geometry") == "raster":
                for r in d["rasters"]:
                    host = self.raster_on_host(r["sha256"])
                    subs.append((str(host), r["path"]))
                    r["path"] = str(host)
            else:
                src = GOLDEN / name / "upload" / Path(d["values_path"]).name
                host = self.inputs / name / src.name
                host.parent.mkdir(parents=True, exist_ok=True)
                if not host.exists():
                    shutil.copyfile(src, host)
                subs.append((str(host), d["values_path"]))
                d["values_path"] = str(host)
            out[key] = d
        return out, subs

    def raster_on_host(self, sha256: str) -> Path:
        """The raster upload is the catalog's own noise grid (golden README); find it."""
        candidate = self.data / "Noise" / "C3" / "CONUS_L50dBA_sumDay_exi.tif"
        lib = MANIFEST["runs"]["custom_raster"].get("custom_library_files") or []
        recorded = {f.get("sha256") for f in lib}
        assert sha256 in recorded, "the raster's sha256 is not the recorded upload's"
        return candidate

    def cache_dir_for(self, name: str) -> Path:
        """A fresh cache per run, except where the golden pointed at another run's cache."""
        run_json = json.loads((GOLDEN / name / "run.json").read_text())
        override = (run_json.get("env_overrides") or {}).get("C3_CACHE_DIR", "")
        owner = name
        for part in Path(override).parts:
            if part.startswith("tier2-golden-"):
                owner = part[len("tier2-golden-"):]
        return self.out / "cache" / owner

    # -- running ----------------------------------------------------------------

    def run(self, name: str) -> dict:
        if name in self.done:
            return self.done[name]
        golden_run = MANIFEST["runs"][name]
        cache_dir = self.cache_dir_for(name)
        if cache_dir.name != name and cache_dir.name not in self.done:
            self.run(cache_dir.name)           # bg_ndi_wi_rev needs bg_ndi_wi's warm cache
        if cache_dir.name == name and cache_dir.exists():
            shutil.rmtree(cache_dir)           # a cold run: its own cache starts empty
        from spacescans.orchestrate import LogWriter, WeightCache, link
        from spacescans.orchestrate.execute import _maxrss_to_bytes

        run_dir = self.out / "runs" / f"tier2-golden-{name}"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True, mode=0o700)
        custom, subs = self.custom_for(name)
        stamp = run_dir.parent / f"{name}.stamp"
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        time.sleep(0.01)
        t0 = time.monotonic()
        executor = "subprocess"
        if _PREFIX:
            import shlex

            from spacescans.orchestrate import SubprocessExecutor
            executor = SubprocessExecutor(cmd_prefix=shlex.split(_PREFIX))
        result = link(self.cohort_csv, list(golden_run["variables"]), data_dir=self.data,
                      run_dir=run_dir, buffer_m=MANIFEST["params"]["buffer_m"],
                      raster_res_m=MANIFEST["params"]["raster_res_m"], custom=custom,
                      cache=WeightCache(cache_dir, key_format="legacy-v1"),
                      executor=executor, on_event=LogWriter.for_run_dir(run_dir))
        wall = time.monotonic() - t0
        newer = _newer_files(self.data, stamp)
        record = {"result": result, "run_dir": result.run_dir, "subs": subs,
                  "prefix": (" ".join(executor.cmd_prefix) if _PREFIX else None),
                  "cache_dir": cache_dir, "wall_s": wall,
                  "data_newer": [p for p in newer if Path(p).name != DS_STORE],
                  "data_newer_ds_store": [p for p in newer if Path(p).name == DS_STORE],
                  "self_maxrss_bytes": _maxrss_to_bytes(
                      resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)}
        self.done[name] = record
        return record

    def write_summary(self, name: str, entry: dict) -> None:
        self.summary["runs"][name] = entry
        self.summary_path.write_text(json.dumps(self.summary, indent=2, default=str) + "\n")


@pytest.fixture(scope="session")
def a4(tmp_path_factory) -> A4:
    out = Path(_OUT_ENV) if _OUT_ENV else tmp_path_factory.mktemp("a4")
    out.mkdir(parents=True, exist_ok=True)
    return A4(out)


# ---------------------------------------------------------------------------
# comparisons
# ---------------------------------------------------------------------------

def _host_to_web(text: str, rec: dict, a4: A4, *, sha8: str | None) -> str:
    from spacescans.orchestrate.execute import default_prefix

    text = text.replace(rec.get("prefix") or " ".join(default_prefix()), WEB_PREFIX)
    for host, container in rec["subs"]:
        text = text.replace(host, container)
    text = text.replace(str(a4.data), WEB_DATA)
    if sha8 and sha8 != WEB_SHA8:
        text = text.replace(f"{sha8}__", f"{WEB_SHA8}__")
    return text


def _compare_run(name: str, rec: dict, a4: A4) -> tuple[list[str], dict]:
    import pandas as pd

    from spacescans import catalog

    gc = _golden_compare()
    tol = gc.SAME_ENV if _TOL == "same" else gc.CROSS_ENV
    golden_run = MANIFEST["runs"][name]
    gdir = GOLDEN / name
    result = rec["result"]
    run_dir = Path(rec["run_dir"])
    remote = golden_run["remote_task_dir"]
    problems: list[str] = []
    evidence: dict = {}

    if result.failed or result.skipped:
        problems.append(f"failed={result.failed} skipped={result.skipped} errors={result.errors}")
        return problems, evidence

    # input.parquet: the same frame (A1 across environments); its sha8 names the cache keys
    from spacescans.orchestrate.cache import sha256_file
    new_in, gold_in = run_dir / "input.parquet", gdir / "input.parquet"
    if not pd.read_parquet(new_in).equals(pd.read_parquet(gold_in)):
        problems.append("input.parquet: frame differs from the golden one")
    sha8 = sha256_file(new_in)[:8]
    evidence["input_sha8"] = sha8

    # rendered YAML (A0 normalisation); a cache hit renders none
    gold_yaml = {p.name: p for p in sorted((gdir / "pipeline_configs").glob("*.yaml"))}
    new_yaml = {p.name: p for p in sorted((run_dir / "pipeline_configs").glob("*.yaml"))}
    if set(gold_yaml) != set(new_yaml):
        problems.append(f"pipeline_configs: files {sorted(new_yaml)} != golden {sorted(gold_yaml)}")
    for fname in sorted(set(gold_yaml) & set(new_yaml)):
        new_text = _host_to_web(new_yaml[fname].read_text(), rec, a4, sha8=None)
        if gc.normalise_yaml(new_text, str(run_dir)) != gc.normalise_yaml(
                gold_yaml[fname].read_text(), remote):
            problems.append(f"pipeline_configs/{fname}: differs after normalisation")
    evidence["yaml_files"] = len(gold_yaml)

    # runner-authored log lines
    gold_lines = gc.runner_log_lines(gdir / "logs.jsonl", remote)
    new_lines = [_host_to_web(line, rec, a4, sha8=sha8)
                 for line in gc.runner_log_lines(run_dir / "logs.jsonl", str(run_dir))]
    if new_lines != gold_lines:
        problems.append("logs.jsonl runner lines differ:\n  new:    "
                        + "\n          ".join(new_lines) + "\n  golden: "
                        + "\n          ".join(gold_lines))
    evidence["runner_log_lines"] = len(gold_lines)

    # C3, C4 and materialised-value tables
    rec_of = catalog.recipe(golden_run["experiment"])
    tables = {}
    for gpath in sorted((gdir / "output").glob("*.parquet")):
        npath = run_dir / "output" / gpath.name
        if not npath.is_file():
            problems.append(f"output/{gpath.name}: missing")
            continue
        g, n = pd.read_parquet(gpath), pd.read_parquet(npath)
        if rec_of.rename_in_place and rec_of.renames and gpath.name.startswith("c4_"):
            n = n.rename(columns=dict(rec_of.renames))
        diffs = gc.compare_tables(g, n, tol=tol, c3=gpath.name.startswith("c3_"))
        if diffs:
            problems.append(f"output/{gpath.name}: " + "; ".join(diffs))
        floats = [c for c in g.columns if pd.api.types.is_float_dtype(g[c])]
        if floats and len(g) == len(n) and list(g.columns) == list(n.columns):
            gs, ns = gc.canonical_table(g), gc.canonical_table(n)
            md = gc.max_diffs(gs, ns, floats)
            tables[gpath.name] = {"rows": len(g),
                                  "max_abs": max((v["max_abs"] or 0.0) for v in md.values()),
                                  "max_rel": max(v["max_rel"] for v in md.values()),
                                  "differing": sum(v["differing"] for v in md.values())}
        else:
            tables[gpath.name] = {"rows": len(g)}
    evidence["tables"] = tables

    # partial result_<exp>.csv (canonical order)
    exp = golden_run["experiment"]
    part_path = run_dir / "output" / f"result_{exp}.csv"
    result.partials[exp].to_csv(part_path, index=False)
    diffs = gc.compare_result_csv(gdir / "output" / f"result_{exp}.as_written.csv", part_path,
                                  tol=tol, canonical=True)
    if diffs:
        problems.append(f"result_{exp}.csv: " + "; ".join(diffs))

    # result.csv: the cohort as uploaded plus link's exposures, in input order
    cohort_text = pd.read_csv(a4.cohort_csv, dtype=str, keep_default_na=False)
    exposures = result.exposures
    if list(exposures["episode_id"]) != list(range(len(cohort_text))):
        problems.append("exposures: episode_id is not 0..n-1 in input order")
    full = pd.concat([cohort_text, exposures.drop(columns=["episode_id"])], axis=1)
    res_path = run_dir / "output" / "result.csv"
    full.to_csv(res_path, index=False)
    diffs = gc.compare_result_csv(gdir / "output" / "result.csv", res_path, tol=tol,
                                  canonical=False)
    if diffs:
        problems.append("result.csv: " + "; ".join(diffs))
    g_res = gc.read_text_frame(gdir / "output" / "result.csv")
    n_res = gc.read_text_frame(res_path)
    num_cols = [c for c in g_res.columns if c not in cohort_text.columns]
    if list(g_res.columns) == list(n_res.columns) and len(g_res) == len(n_res):
        try:
            md = gc.max_diffs(g_res.replace("", "nan"), n_res.replace("", "nan"), num_cols)
            evidence["result_csv"] = {c: {k: v[k] for k in ("cells", "differing", "max_abs",
                                                            "max_rel", "nan_mask_equal")}
                                      for c, v in md.items()}
        except ValueError:      # a non-numeric exposure column: compared as text above
            pass

    # feature dictionary, byte for byte: the web's lookup by column name, which the web keeps
    # until the cutover (phase-C decision 3)
    feat_path = run_dir / "output" / "feature_dictionary.csv"
    catalog.write_feature_csv(list(full.columns), feat_path)
    if feat_path.read_bytes() != (gdir / "output" / "feature_dictionary.csv").read_bytes():
        problems.append("feature_dictionary.csv differs")
    # LinkResult.features (phase-C decision 3): a catalog column carries the web's row for its
    # name (the golden dictionary); a custom column carries the row made from the user's
    # definition (label, units, description), not the catalog row of a column so named
    from spacescans import custom as _custom

    gold_rows = {r["feature_name"]: r for r in pd.read_csv(
        gdir / "output" / "feature_dictionary.csv", dtype=str,
        keep_default_na=False).to_dict(orient="records")}
    definitions, _subs = a4.custom_for(name)
    custom_cols = {c: d for d in (definitions or {}).values() for c in d.get("value_cols") or []}
    want_rows = []
    for col in [c for c in exposures.columns if c != "episode_id"]:
        if col in custom_cols:
            want_rows.extend(_custom.feature_rows(custom_cols[col], [col]))
        elif col in gold_rows:
            want_rows.append(gold_rows[col])
    got_rows = result.features.to_dict(orient="records")
    if got_rows != want_rows:
        problems.append(f"LinkResult.features rows {got_rows} != expected {want_rows}")
    evidence["feature_rows"] = {"catalog": sum(c not in custom_cols for c in exposures.columns
                                               if c != "episode_id"),
                                "custom": len(custom_cols)}

    # legacy cache keys and meta (A2 fields)
    cache_dir = rec["cache_dir"]
    for gmeta in sorted((gdir / "c3_cache").glob("*.meta.json")):
        key = gmeta.name[: -len(".meta.json")]
        host_key = key.replace(f"{WEB_SHA8}__", f"{sha8}__")
        nmeta = cache_dir / f"{host_key}.meta.json"
        if not nmeta.is_file():
            problems.append(f"cache: no entry {host_key}")
            continue
        g, n = json.loads(gmeta.read_text()), json.loads(nmeta.read_text())
        # Across environments the input's and the cached parquet's bytes differ (they embed
        # library versions): sha_full and file_size_bytes are checked against the new files.
        drop = ("created_at", "wall_clock_seconds", "sha_full", "file_size_bytes")
        if [k for k in g] != [k for k in n] or {k: v for k, v in g.items() if k not in drop} != \
                {k: v for k, v in n.items() if k not in drop}:
            problems.append(f"cache meta {key}: {n} != golden {g}")
        if n["sha_full"] != sha256_file(new_in):
            problems.append(f"cache meta {key}: sha_full is not the input's sha256")
        if n["file_size_bytes"] != (cache_dir / f"{host_key}.parquet").stat().st_size:
            problems.append(f"cache meta {key}: file_size_bytes is not the entry's size")
    statuses = {s.name: s.status for s in result.steps}
    evidence["steps"] = [s.to_dict() for s in result.steps]
    if name == "bg_ndi_wi_rev":
        for c3 in ("c3_bg", "c3_bg_2020"):
            if statuses.get(c3) != "cache_hit":
                problems.append(f"{c3}: status {statuses.get(c3)}, expected cache_hit")

    # coverage counts episodes; with_value equals the partial's matched episodes
    cov = result.coverage
    if list(cov["variable"]) != list(golden_run["variables"]):
        problems.append(f"coverage variables {list(cov['variable'])}")
    if (cov["episodes"] != len(cohort_text)).any():
        problems.append("coverage: episodes is not the cohort's row count")

    # the data root is read-only
    if rec["data_newer"]:
        problems.append(f"data dir written: {rec['data_newer'][:10]}")
    return problems, evidence


@pytest.mark.parametrize("name", RUNS)
def test_link_matches_the_golden_run(name: str, a4: A4) -> None:
    rec = a4.run(name)
    problems, evidence = _compare_run(name, rec, a4)
    result = rec["result"]
    step_rss = [s.maxrss_bytes for s in result.steps if s.maxrss_bytes]
    a4.write_summary(name, {
        "tolerance": _TOL,
        "wall_s": round(rec["wall_s"], 2),
        "max_step_rss_bytes": max(step_rss) if step_rss else None,
        "orchestrator_maxrss_bytes": rec["self_maxrss_bytes"],
        "steps": evidence.get("steps"),
        "evidence": {k: v for k, v in evidence.items() if k != "steps"},
        "problems": problems,
        "data_newer": rec["data_newer"],
        "data_newer_ds_store": rec["data_newer_ds_store"],
        "run_dir": str(rec["run_dir"]),
    })
    assert not problems, f"{name}:\n" + "\n".join(problems)


def test_fan_in_of_every_run_matches_fanin_all(a4: A4) -> None:
    """link's fan-in over the runs' partials gives the golden ``fanin_all`` result.csv.

    ``fanin_all`` is the dispatcher's fan-in of 11 partials (all catalog variables plus
    the raster upload): it checks link's source order (``catalog.group``, the
    dispatcher's) and the cross-source name rule (``l50dba_exi_custom_dup``). It reads
    the partial CSVs the runs above wrote (pid as text, values typed) and needs no
    pipeline run; it skips until every source run has written its partial.
    """
    import importlib

    import pandas as pd

    from spacescans import catalog

    gc = _golden_compare()
    tol = gc.SAME_ENV if _TOL == "same" else gc.CROSS_ENV
    fan = MANIFEST["runs"].get("fanin_all")
    if not fan:
        pytest.skip("no fanin_all in the goldens")
    partials: dict[str, pd.DataFrame] = {}
    for run in fan["fanin_sources"]:
        exp = MANIFEST["runs"][run]["experiment"]
        path = a4.out / "runs" / f"tier2-golden-{run}" / "output" / f"result_{exp}.csv"
        if not path.is_file():
            pytest.skip(f"run {run} has not written its partial in {a4.out}")
        partials[exp] = pd.read_csv(path, dtype={"pid": str})
    overlay, _subs = a4.custom_for("custom_raster")
    variables = list(fan["variables"])
    grouped = catalog.group(variables, overlay=overlay)
    assert list(grouped) == list(partials) == list(fan["dispatch_plan"])
    link_mod = importlib.import_module("spacescans.orchestrate.link")
    cohort_text = pd.read_csv(a4.cohort_csv, dtype=str, keep_default_na=False)
    keys = pd.DataFrame({"pid": cohort_text["pid"], "episode_id": range(len(cohort_text))})
    exposures, _names = link_mod._fan_in(keys, partials, list(cohort_text.columns))
    full = pd.concat([cohort_text, exposures.drop(columns=["episode_id"])], axis=1)
    out = a4.out / "fanin_all"
    out.mkdir(parents=True, exist_ok=True)
    full.to_csv(out / "result.csv", index=False)
    diffs = gc.compare_result_csv(GOLDEN / "fanin_all" / "output" / "result.csv",
                                  out / "result.csv", tol=tol, canonical=False)
    assert diffs == []
    catalog.write_feature_csv(list(full.columns), out / "feature_dictionary.csv")
    assert (out / "feature_dictionary.csv").read_bytes() == (
        GOLDEN / "fanin_all" / "output" / "feature_dictionary.csv").read_bytes()
    assert "l50dba_exi_custom_dup" in full.columns


def test_the_data_dir_was_not_written(a4: A4) -> None:
    """After every run above: nothing under the data root is newer than the first stamp."""
    newer = _newer_files(a4.data, a4.global_stamp)
    a4.summary["data_dir_check"] = {"stamp": str(a4.global_stamp), "data_dir": str(a4.data),
                                    "newer": newer}
    a4.summary_path.write_text(json.dumps(a4.summary, indent=2, default=str) + "\n")
    assert not [p for p in newer if Path(p).name != DS_STORE], newer
