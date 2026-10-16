# Golden references for tier 2 (P0)

Tier 2 moves orchestration out of spacescans-web into this package. These files record what
the **current** web runners produce, so the new package code can be held to them. Gates A0–A4
are in design §6, and critique #1, #2 and #4 apply.

- **Source:** web 3991c3b with pipeline v0.2.7, frozen as `spacescans-web-backend:frozen-2026-10-12`. See [ENVIRONMENT.md](ENVIRONMENT.md).
- **Cohort:** `synth_1k` (1,001 episodes, 510 patients), copied to `web-0.2.7/inputs/synth_1k.csv`.
- **Parameters:** buffer 270 m (circle), `raster_res_m` 25.
- **Coverage:** all 11 catalog variables, plus the upload path with one block-group CSV and one GeoTIFF.
- **Times:** US Eastern.

## Running the golden tests

The goldens are kept out of git (decision of 2026-10-12). The complete copy (13 MB, with the
`output/` directories) lives outside the repository; point `SPACESCANS_GOLDEN_DIR` at it:

```sh
SPACESCANS_GOLDEN_DIR=/Users/xai/Desktop/spacescans-all/tier2-artifacts/goldens/web-0.2.7 \
  .venv/bin/python -m pytest -q -m golden
```

- Tests that need the goldens carry the `golden` marker, or request the `golden_dir` fixture
  (which adds the marker). `tests/conftest.py` skips them, without failing, unless
  `SPACESCANS_GOLDEN_DIR` names a directory with a `manifest.json`. The `web-0.2.7/` copy in
  this tree is incomplete and is never used implicitly.
- CI deselects them with `-m "not golden"`.
- A copy that is named but incomplete fails the integrity tests; that is a broken copy, not a
  missing one.
- `golden_compare` keeps the value the caller exported, so `gc.golden_root()` and
  `gc.golden_dir()` still see it inside a test, after `tests/conftest.py` has removed the
  `SPACESCANS_*` variables for that test.

> **Do not push these files to a public remote yet.** They hold exposure values linked from
> licensed sources (TEMIS, VNL, ACAG, FAQSD and others), computed on a synthetic cohort.
> Critique #6 asks for a licensing ruling (D3/D4) before such derived values are shared.
> Until then, keep them on a private branch or move them to a private store.

## Layout

```text
tests/golden/
  README.md                 this file
  ENVIRONMENT.md            frozen image, web commit, versions; env/ holds the raw records
  golden_compare.py         the comparison rules parity tests must use (sort rule, tolerances)
  test_golden_integrity.py  checks the stored files against the manifest (runs in pytest)
  tools/web_goldens.py      driver: run (in the container), collect, compare
  tools/golden_collect.py   collect/compare implementation
  tools/inventory.py        template hashes and data-root fingerprint (env/)
  tools/render_matrix.py    render-only goldens: YAML and cache keys over a parameter matrix (A0/A2)
  web-0.2.7/
    manifest.json           every run: versions, parameters, timings, and per file the sha256, bytes, rows, columns
    determinism.json        run-to-run variation (two cold passes of the same image)
    inputs/synth_1k.csv
    render_matrix/          index.json + <combo>/<step>.yaml for 126 runner × variables × buffer × raster combinations
    <run>/                  one directory per run (table below)
```

Each `<run>/` directory holds:

| File | What it is |
|---|---|
| `config.json` | The task config the runner read. |
| `input.parquet` | The runner's `csv_to_parquet` output. The C3 cache keys start with the first 8 hex digits of its sha256 (`12269dbe`). |
| `pipeline_configs/<step>.yaml` | Every YAML the runner rendered, verbatim. Steps served from the cache render none. |
| `status.json`, `logs.jsonl` | Verbatim. CLI-started runners leave `status` at `"running"` even after success, so judge by `run.json`'s `rc` and the result CSV. |
| `runner.stdout.txt`, `runner.stderr.txt` | The runner process's own output. |
| `run.json` | Command, the `C3_CACHE_DIR` override, attempts (lock retries), start and end times, wall seconds, rc, and per-step seconds parsed from `logs.jsonl`. |
| `output/result_<exp>.as_written.csv` | The per-experiment partial, byte for byte as the runner wrote it. |
| `output/result_<exp>.sorted.csv` | The same rows in canonical order (see "Comparing"). |
| `output/result.csv`, `output/feature_dictionary.csv` | The dispatcher's fan-in (`_merge.fan_in(task_dir, [exp])`), as written. |
| `output/*.parquet` | C3 and C4 outputs, plus `values_<key>.parquet` for the raster upload. Files of 20 MB or more are listed in `large_files.json` with sha256, rows and columns instead of being copied. |
| `c3_cache/<key>.meta.json` | The cache sidecars the runner wrote. The file name is the legacy-v1 cache key. The cached parquet equals `output/<step>.parquet`; the manifest records this under `c3_cache_entries`. |
| `custom_definition.json` | Upload runs only. The manifest that `create` / `create_raster` returned, as copied into `config.json["custom_variables"]`. |
| `custom_library_files.json` | Upload runs only. sha256 and size of the stored upload (`values.csv`, or `rasters/static.tif`). |
| `upload/values.csv` | The CSV upload run only: the uploaded table, byte for byte (its sha256 is the definition's `sha256`). The raster upload is the catalog's own 432 MB GeoTIFF and is recorded by sha256 only. |

## Runs

| Run | Runner (`app.experiments.<exp>`) | `--variables` | Steps (cache hits marked) | Wall s | Partial rows | Partial order as written |
|---|---|---|---|---|---|---|
| `noise` | noise | `noise` | `c3_noise` 1s, `c4_noise` 4s | 6.0 | 1001 | hash order |
| `temis` | temis | `temis` | `c3_temis` 1s, `c4_temis` 8s | 9.5 | 1001 | hash order |
| `vnl` | vnl | `vnl` | `c3_vnl` 3s, `c4_vnl` 9s | 12.5 | 1001 | hash order |
| `nhd_bluespace` | nhd_bluespace | `nhd_bluespace` | `c3_nhd_bluespace` 64s, `c4_nhd_bluespace` 1s | 65.7 | 1001 | input order |
| `tiger_proximity` | tiger_proximity | `tiger_proximity` | `c3_tiger_roads` 127s, `c4_tiger_roads` 1s | 128.7 | 1001 | input order |
| `fara_tract` | fara_tract | `fara_tract` | `c3_tract_us` 28s, `c4_tract_fara` 14s | 43.5 | 1001 | input order |
| `zcta5_cbp` | zcta5_cbp | `cbp_zcta5` | `c3_zcta5` 28s, `c3_county` 17s, `c4_zcta5_cbp` 1s, `c4_county_cbp` 1s | 47.1 | 1001 | hash order |
| `bg_ndi_wi` | bg_ndi_wi | `ndi,walkability` | `c3_bg` 39s, `c3_bg_2020` 44s, `c4_ndi` 6s, `c4_wi` 1s | 91.0 | 1001 | input order |
| `bg_ndi_wi_rev` | bg_ndi_wi | `walkability,ndi` | `c3_bg` (hit), `c3_bg_2020` (hit), `c4_ndi` 6s, `c4_wi` 1s | 7.3 | 1001 | input order |
| `faqsd` | faqsd | `faqsd` | `c3_tract_us` 27s, `c4_tract_faqsd` 218s | 246.4 | 1001 | input order |
| `custom_raster` | custom | `custom_714c9715` (upload) | `c3_customgrid_3940345e` 1s, `c4_custom_714c9715` 1s | 2.7 | 1001 | hash order |
| `custom_bg_csv` | custom | `custom_18180893` (upload) | `c3_bg` 37s, `c4_custom_18180893` 2s | 39.6 | 1001 | hash order |
| `acag` | acag | `acag` | `c3_acag` 2s, `c4_acag` 913s | 915.3 | 995 | input order |
| `fanin_all` | (fan-in only) | all 11 catalog keys + the raster upload | `_merge.fan_in` over 11 partials | | 1001 (result.csv) | input order |

The acag partial has 995 rows, not 1,001. Episodes 28, 99, 231, 252, 336 and 550 got no ACAG
output, so they are absent from the partial and empty in `result.csv`. All six are near-shore
CONUS points; five of them also have no noise value. This is the partial semantics the
comparison rules below depend on: a missing row is part of the reference.

`fanin_all` is not a runner. It is one task assembled from the partials of all the catalog
runs plus the raster upload, then fanned in exactly as the dispatcher does: experiments in
the order `app.variable_registry.variables_by_experiment` returns, then `_merge.fan_in`. It
covers what one task selecting everything would produce. That includes the column
collision rule: the upload's `l50dba_exi` meets noise's and becomes `l50dba_exi_custom_dup`.

## Render matrix (A0, A2)

The run goldens cover one setting (270 m, 25 m). `tools/render_matrix.py` calls the same
runners' `plan()`, `render_yaml()` and `_cache_key()` in the container, without running the
pipeline or taking the run lock. It covers:

- every catalog runner with every variable subset and order (bg_ndi_wi: `ndi`,
  `walkability`, `ndi,walkability`, `walkability,ndi`);
- uploads on BG, Tract, ZCTA5 and County, static and yearly, and three uploads in one task
  (two share the BG C3 step);
- the raster upload's C3 step;
- each of these × buffer {270, 500, 1000} m × `raster_res_m` {25, 50}.

That makes 126 combinations and 306 YAMLs. `index.json` lists every step (name, template,
C3 or not) and every C3 legacy-v1 cache key, computed over the shared `input.parquet`.

- The Tract, ZCTA5, County and yearly upload definitions are synthesised from the real BG
  one, changing only `boundary`, `join_col`, the id, and `year_col`/`coverage_years`.
- The raster upload's C4 is not rendered, because rendering it reads pixels.
- `test_render_matrix_matches_the_runs` checks that the 270/25 combinations equal what the
  runs rendered: all 27 YAMLs after normalisation, and every cache key.

## How the runs were made

`tools/web_goldens.py run` follows `spacescans-paper/results/experiments/custom_equivalence.py`:

1. Create `/app/data/tasks/tier2-golden-<run>/` with `input.csv` (the cohort, byte for byte),
   `output/`, and `config.json` = `{"experiment": "auto", "buffer": {"shape": "circle",
   "size": 270, "raster_res_m": 25}, "version": 1, "input_file": "input.csv",
   "variables": [...]}` (plus `custom_variables` for uploads).
2. Run `cd /app && /opt/conda/bin/python -m app.experiments.<exp> run <task_dir> --variables <keys>`
   in `spacescans-web-backend-1`.
3. Before each run, wait until no `app.experiments` / `app.dispatcher` / `spacescans run`
   process is alive. If `status.json` then reports "another task acquired the run lock
   first", wait and retry with a fresh task dir.
4. Uploads use the throwaway library owner UID **9997**. Its library
   (`/app/data/custom_exposomes/user-9997`) is deleted first. The two datasets are then
   registered with the paper script's own code, only the UID changed:
   - the noise GeoTIFF `Noise/C3/CONUS_L50dBA_sumDay_exi.tif`, through `create_raster`;
   - walkability 2016 by 2010 BG, read with the catalog's reader and written to CSV, through `create`.

Differences from the paper script, all deliberate:
- **Private cache per run.** Every run gets an empty C3 cache: `docker exec -e
  C3_CACHE_DIR=<task_dir>/golden_c3_cache`, which `app.config.Settings` reads from the
  environment.
  - All C3 steps are computed cold, so every step renders its YAML.
  - The production cache `/app/data/c3_cache` is neither read nor written. Stale or foreign
    entries cannot leak in, and the goldens leave nothing behind.
  - Only `bg_ndi_wi_rev` points at `bg_ndi_wi`'s cache, on purpose: it records the
    cache-hit path.
- **Runner output kept.** The runner's stdout and stderr are kept, not sent to /dev/null.
- **Fan-in added.** After each run the dispatcher's fan-in is applied.

## Regenerating

These steps need the backend container from the frozen image, with the mounts listed in
ENVIRONMENT.md, and the docker CLI on the host. The `run` step holds the global run lock for
about 25–30 minutes. Do not run it while paper benchmarks run (`runtime.py` needs an idle
machine) or while users have tasks queued.

```sh
python3 tests/golden/tools/web_goldens.py run --stage /tmp/golden-stage              # all runs + fanin_all
.venv/bin/python tests/golden/tools/web_goldens.py collect --stage /tmp/golden-stage --out tests/golden/web-0.2.7
python3 tests/golden/tools/web_goldens.py run --stage /tmp/golden-stage-rep2 --suffix=-rep2
.venv/bin/python tests/golden/tools/web_goldens.py compare --a /tmp/golden-stage --b /tmp/golden-stage-rep2 \
    --out tests/golden/web-0.2.7/determinism.json
python3 tests/golden/tools/render_matrix.py --golden tests/golden/web-0.2.7 --out tests/golden/web-0.2.7/render_matrix
python3 tests/golden/tools/inventory.py --web ../spacescans-web --out tests/golden/env
.venv/bin/python -m pytest tests/golden -q
```

- `--only noise,vnl` reruns a subset.
- Task dirs stay in the container's `/app/data/tasks/tier2-golden-*`, and the uploads stay in
  `user-9997`. Delete both when you no longer need them.

## Comparing: rules for parity tests

Use `golden_compare.py`; do not write a separate comparison. A parity test looks like this:

```python
import golden_compare as gc       # tests/conftest.py has imported it; no sys.path change needed

# Requesting golden_dir marks the test golden and skips it when SPACESCANS_GOLDEN_DIR is unset.
# Load the manifest inside the test or a fixture, never at import: without the goldens a
# module-level read fails at collection.

def test_noise_partial_matches_the_web_runner(golden_dir, tmp_path):
    new_csv = ...                               # result_noise.csv written by the new code
    assert gc.compare_result_csv(golden_dir / "noise/output/result_noise.as_written.csv", new_csv,
                                 tol=gc.CROSS_ENV) == []   # SAME_ENV inside the frozen image

def test_noise_yaml_matches(golden_dir, tmp_path):
    run = gc.load_manifest(golden_dir)["runs"]["noise"]
    for y in (golden_dir / "noise/pipeline_configs").glob("*.yaml"):
        new_yaml = ...                          # the new render() output for step y.stem, as text
        assert gc.normalise_yaml(new_yaml, str(tmp_path)) == gc.normalise_yaml(
            y.read_text(), run["remote_task_dir"])
```

A module whose parametrisation needs the manifest at import (as `tests/test_tier2_golden_parity.py`
does) sets `pytestmark = pytest.mark.golden` and first calls
`pytest.skip(gc.golden_unavailable_reason(), allow_module_level=True)` when `gc.golden_dir()` is
None.

1. **Rows sorted.**
   - Per-experiment partials (`result_<exp>.csv`) are compared in canonical order. Rows are
     sorted by `int(episode_id)`, with ties broken by the remaining columns' raw text from
     left to right. The header, the column order and each row's text are left unchanged.
     `canonical_result_csv()` implements this; `*.sorted.csv` is its output.
   - Why: in pass A, the partials of noise, temis, vnl, zcta5_cbp, custom_raster and
     custom_bg_csv were not in input order. `write_partial` keeps the C4 output's row order,
     and between the two passes those orders changed. The other runners happened to write
     input order. Row order is not part of the contract.
   - `result.csv` and `feature_dictionary.csv` follow the input order (a left join on the
     cohort), so they are compared as written.
2. **Keys exact.** These must match exactly:
   - column names and their order;
   - the (pid, episode_id) rows;
   - every non-numeric cell;
   - the missing-value mask (an empty cell is missing).

   Partials hold only episodes that got output, so a missing row is a failure, not a NaN.
3. **Values within a stated tolerance.** A value passes when `|new − golden| ≤ atol + rtol·|golden|`.

   | Where the new code ran | Constant | rtol | atol | Basis |
   |---|---|---|---|---|
   | The frozen image, same data root | `SAME_ENV` | 1e-12 | 1e-12 | Two cold passes of this image differ by at most 1.4e-14 absolute (2.5e-14 relative). The absolute term alone is 70 times that, and all 71 compared files pass (see "Run-to-run variation"). |
   | Another environment (e.g. the macOS uv env of the R package) | `CROSS_ENV` | 1e-9 | 1e-9 | Design gate B for continuous values (rel 1e-9). Per column kind (`Tolerance.for_column`, phase-C decision 1 of 2026-10-13): the weights of a C3 table abs 1e-8 (`weight_atol`; the host's GDAL/rasterio build moves 3 of 8,098 noise grid weights by up to 8.6e-9 while every per-episode sum agrees to 1e-16), distances in metres (`dist_*` columns) abs 1e-6 m (`distance_atol_m`). Keys and missing-value masks stay exact. Anything larger is investigated as an environment difference. |

   On synth_1k, FARA's `LILATracts_1And10`, `LATracts1` and `HUNVFlag` take only the values
   0 and 1, so in practice they are compared exactly. `LowIncomeTracts` and CBP's `r_*`
   columns are continuous.

   **Deliberate difference since 0.3 (manuscript comment C-19).** FARA now leaves a tract
   without a value out of the buffer mean's denominator as well as its numerator, as every
   other source does; these goldens were made with the old rule (a missing value counted as
   zero). Gate A4 (`tests/orchestrate/test_link_a4.py`) therefore holds `fara_tract`'s C4
   table to a recomputation under the new rule and to the golden only where the two rules
   agree, with keys and missing masks exact. On synth_1k no FARA value changes: the FARA
   table's missing rates are almost all in 2019 (71,029 of 71,154 tract-years from 2013 on
   with a missing rate) and the synthetic cohorts end on 2018-12-31. The goldens themselves
   are not regenerated.
   Every comparison here is at the episode level (`output_level="episode"`); the
   participant level, the default since 0.3, is tested by `tests/test_participant_level.py`.
4. **C3/C4 parquet.** Compare with `compare_tables()`: canonical row order, then the same
   per-column rules; pass `c3=True` for a `c3_*.parquet` table, so that at `CROSS_ENV` its
   weights get the 1e-8 absolute bound. Byte equality is not expected: between two cold
   passes of the same image, the C4 tables of several runs differed in row order or in the
   last bits.
5. **Rendered YAML (A0).** Before comparing, replace the task-dir prefix
   `/app/data/tasks/tier2-golden-<run>` and the name suffix `_task_<last 8 chars of the task
   dir name>`; the text must then be byte-identical. A step served from the cache renders no
   YAML (`bg_ndi_wi_rev`).
6. **Cache keys (A2).** These must match exactly:
   - the key strings (the `c3_cache/*.meta.json` file names);
   - the meta fields other than `created_at` and `wall_clock_seconds`. That includes
     bg_ndi_wi recording BG2020 entries as `"boundary": "BG"`.
7. **`input.parquet` (A1).** Byte-identical within the frozen image only. Its bytes embed the
   pandas and pyarrow versions, so another environment gives other bytes and another
   cache-key prefix.
8. **`logs.jsonl`.** Compare the runner-authored lines with `runner_log_lines()`. It drops `ts`
   and the pipeline's own output, which carries timings, and replaces the task dir. The
   lines that remain must be identical. They include:
   - `spawning <step>: <cmd>`
   - `step <step> exit code <rc>`
   - `cache hit: <key> — skipping pipeline run`
   - `cache write: <file>`
   - the raster `materialised …` line
   - the upload runner's `<key>.<col>: <pct>% of episodes linked (<n>/<N>)` line
   - the `merge_partial_low_match_pct` warning records

   Pipeline lines are compared only after normalising paths and timing text (critique #4).

## Run-to-run variation

The 0.2.7 runners were run twice, cold, in the same image on the same cohort and data:
- pass A, 16:43–17:10 EDT (the goldens);
- pass B, 17:10–17:38 EDT (task dirs `tier2-golden-<run>-rep2`).

Every output file was then compared. The full record is in `web-0.2.7/determinism.json`,
written by `web_goldens.py compare`.

| Run | Partial as written | Partial sorted | C3 parquet | C4 parquet | result.csv | Max rel diff | Max abs diff |
|---|---|---|---|---|---|---|---|
| `noise` | differs | identical | identical | row order only | identical | 0 | 0 |
| `temis` | differs | differs | identical | values differ | within SAME_ENV | 7.0e-16 | 5.3e-15 |
| `vnl` | differs | differs | identical | values differ | within SAME_ENV | 5.2e-16 | 1.4e-14 |
| `nhd_bluespace` | identical | identical | identical | identical | identical | 0 | 0 |
| `tiger_proximity` | identical | identical | identical | identical | identical | 0 | 0 |
| `fara_tract` | identical | identical | identical | identical | identical | 0 | 0 |
| `zcta5_cbp` | differs | differs | identical | values differ | within SAME_ENV | 2.1e-15 | 7.1e-15 |
| `bg_ndi_wi` | differs | differs | identical | values differ | within SAME_ENV | 2.6e-15 | 8.9e-16 |
| `bg_ndi_wi_rev` | differs | differs | identical | values differ | within SAME_ENV | 2.6e-15 | 8.9e-16 |
| `faqsd` | identical | identical | identical | identical | identical | 0 | 0 |
| `custom_raster` | differs | identical | identical | row order only | identical | 0 | 0 |
| `custom_bg_csv` | differs | identical | identical | row order only | identical | 0 | 0 |
| `acag` | differs | differs | identical | values differ | within SAME_ENV | 2.5e-14 | 8.9e-15 |

- **C3 weights** were byte-identical in every run.
- **Row order** of the partial and the C4 parquet changed between passes wherever the runner
  writes hash order (noise, vnl, temis, zcta5_cbp and the uploads).
- **Values** differ in the last bits in the outputs of five runners: temis, vnl, zcta5_cbp,
  acag, and bg_ndi_wi (both of its runs).
  - 1.7% to 10% of value cells, and 20% in acag (44% of one acag column);
  - at most 2.5e-14 relative and 1.4e-14 absolute.

  This is consistent with multi-threaded floating-point sums (DuckDB runs the C4 SQL on 14
  threads with no ORDER BY). The cause is inferred, not traced.
- **Consequence:** byte identity of partials, `result.csv` or C4 tables cannot be a gate,
  even old code against old code (critique #4). The gates compare keys exactly and values
  with `SAME_ENV`.
  - All 71 files compared between the passes pass `SAME_ENV`: partials, `result.csv`, and
    C3, C4 and values parquet.
  - None would fail even at atol = 1e-13 alone.

## Findings other tracks must preserve

These are observed in the goldens, not inferred.

- **One input, one key prefix.** Every runner's `input.parquet` is byte-identical on this
  cohort (the zcta5 FIPS variant changes nothing without FIPS columns). Every cache key
  therefore starts with `12269dbe`.
- **Key strings.** Areal tags carry `__r25m`: `BG`, `BG2020`, `ZCTA5`, `COUNTY`,
  `TRACT_FARA`, and BG for uploads. Grid and proximity tags do not: `BG_NOISE`, `BG_VNL1`,
  `BG_TEMIS`, `BG_ACAG`, `BG_NHD`, `BG_TIGER2`, `CUSTOMGRID_3940345e`.
- **Meta sidecars differ by runner.**
  - `raster_res_m` is written only by bg_ndi_wi and zcta5_cbp. fara_tract, faqsd and the
    upload runner omit it, even under `__r25m` keys.
  - bg_ndi_wi records its BG2020 entry as `"boundary": "BG"`.
  - A legacy-v1 store must reproduce both.
- **Shared weights are byte-identical.** Each group below holds byte-identical C3 outputs:
  - `c3_bg`: bg_ndi_wi, custom_bg_csv, and the cache copy in bg_ndi_wi_rev;
  - `c3_tract_us`: fara_tract and faqsd;
  - `c3_noise` and the raster upload's `c3_customgrid_3940345e`.

  See `manifest.json` → `cross_checks`.
- **Render variants per runner.** The upload runner adds `time: {output_grouping: episode}`
  even to its C3 step. bg_ndi_wi's identical `c3_bg` has no `time` block. The output and the
  cache key are the same, but the YAML text is not. A single `render()` must keep this
  variant to pass A0.
- **Upload equivalence (gate C).**
  - The raster upload reproduces catalog noise `l50dba_exi` on 993 of 993 episodes where
    both have a value: 985 identical, max |diff| 2.84e-14.
  - The BG CSV upload reproduces walkability on 1,001 of 1,001 episodes, all identical.
- **FAQSD renames in place.** `c4_tract_faqsd.parquet` already carries `faqsd_o3` and
  `faqsd_pm25`, because the runner rewrites the file in place (faqsd.py:213-216).
- **Request order sets column order.** `bg_ndi_wi_rev` (`walkability,ndi`) writes
  `NatWalkInd` before `ndi`. The C4 steps still run in the fixed order `c4_ndi`, `c4_wi`.

## Known limits

- **One cohort size.** The goldens use synth_1k only, so no step comes near a memory limit
  (critique #3). Gate M needs synth_10k and synth_100k runs.
- **Absolute paths.** Upload definitions hold absolute container paths
  (`/app/data/custom_exposomes/user-9997/<id>/…`).
- **The raster's `grid_hash`.** It comes from rasterio's CRS and transform formatting, so
  another rasterio or GDAL may give another `c3_customgrid_<hash>` step name and cache tag
  (orchestration map, risk 14).
- **Templates and data are mounts, not image content.** Templates come from the read-only
  `/configs` bind mount of the web checkout, and data from `/project`. Their fingerprints are
  in `env/templates.json` and `env/data_inventory.json`. A rerun after those change is a
  different reference.
