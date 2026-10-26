# Tier-2 notes: the pandas pin and the opt-in test suites

Two things that tier 2 needs and that are easy to get wrong:

- the exact pandas and pyarrow versions that the web's legacy cache keys depend on;
- how to run the test suites that need data outside the repository.

## 1. Pin pandas and pyarrow exactly in the web image

### What the legacy key depends on

The web's C3 cache key (`legacy-v1`) is built as follows (see `catalog.legacy_cache_key`):

```text
<first 8 hex of sha256(input.parquet bytes)>__<TAG>__b<buffer_m>m[__r<raster_res_m>m]
```

`input.parquet` is the cohort that pandas writes through pyarrow. Its bytes record two versions:

- the pandas version, in the Arrow schema metadata (`pandas_version`);
- the pyarrow version, in the Parquet footer (`created_by`).

So the same cohort gets a different key when either version changes. Measured on synth_1k:

| Environment | pandas | pyarrow | `input.parquet` sha8 | Frames |
|---|---|---|---|---|
| frozen web image (goldens) | 3.0.5 | 26.0.0 | `12269dbe` | identical |
| host venv | 3.0.6 | 26.0.0 | `4bae51ff` | identical |

### What follows

- **Web cache.** A web image rebuilt with another pandas or pyarrow misses every C3 entry
  written before, once per cohort. Results stay correct; the weights are only recomputed.
  To keep the existing cache, and the paper's warm-cache benchmarks, valid, the web image must
  pin both packages exactly: `pandas==3.0.5` and `pyarrow==26.0.0`, the versions in
  `tests/golden/env/pip-freeze.txt`. The pin belongs in the web image's requirements, not in
  this package. `pyproject.toml` keeps `pandas >= 2.0`, because an exact pin in a library
  would break its users.
- **Byte gates.** A1 (`cohort.csv_to_parquet` equals the runners' `input.parquet`) and A2
  (legacy keys equal the golden keys) hold byte for byte only under those versions. Run them
  as byte gates in the frozen image `spacescans-web-backend:frozen-2026-10-12`. On another
  host, `tests/test_tier2_golden_parity.py` adapts:
  - A1 compares the frames, and the schema without `pandas_version`.
  - A2 hashes the golden's own `input.parquet`, so it does not depend on the host's pandas.
- **v2 keys** (R and other new callers, design section 2.3) are not affected. Their cohort
  part is `cohort_fp`, a hash of the episode ids and coordinates, not of the file bytes.

## 2. The opt-in test suites

A plain run needs nothing outside the repository. Each opt-in suite skips unless its
variable names a readable directory:

```sh
cd <worktree>
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

| Variable | Points at | Tests |
|---|---|---|
| `SPACESCANS_GOLDEN_DIR` | the goldens, `tier2-artifacts/goldens/web-0.2.7` (13 MB, not in git) | every test marked `golden`: `tests/golden/test_golden_integrity.py`, `tests/test_tier2_golden_parity.py`, and any test that requests the `golden_dir` fixture |
| `SPACESCANS_WEB_REPO` | a spacescans-web checkout | `tests/catalog/` (web probe, recipes) |
| `SPACESCANS_WEB_CONFIGS` | `spacescans-web/configs` | `tests/test_upstream_bridge_support.py` (the web's templates) |
| `SPACESCANS_WEB_BACKEND` | `spacescans-web/backend` | `tests/custom/test_custom_web_parity.py` |
| `SPACESCANS_CUSTOM_EXAMPLES` | `custom_exposome_examples` | `tests/custom/test_custom_web_parity.py` |
| `SPACESCANS_CUSTOM_R1` | the paper's R1 custom outputs | `tests/custom/test_custom_web_parity.py` |
| `SPACESCANS_R1_DIR`, `SPACESCANS_R1_COHORT` | the paper's R1 pipeline outputs and `synth_1k.csv` | `tests/test_web_parity_r1.py` |
| `SPACESCANS_REAL_DATA_DIR` (with `SPACESCANS_GOLDEN_DIR`) | a data root, only read (`spacescans-web/pipeline-data`) | real pipeline runs: `tests/orchestrate/test_exec_real_run.py` (one C3 step through `SubprocessExecutor`), `tests/test_bridge_real_link.py` (a bridge `link` of noise on synth_1k, held to the golden at `CROSS_ENV`) and `tests/orchestrate/test_link_participant_real.py` (the participant level equals the episode level weighted by contributing days, for noise, TIGER and the CBP fallback chain) |
| `SPACESCANS_A4=1` (with the two above) | — | gate A4, `tests/orchestrate/test_link_a4.py`: every golden run through `orchestrate.link` (about 24 minutes). `SPACESCANS_A4_ONLY=noise,vnl` picks runs, `SPACESCANS_A4_OUT` keeps the run directories, `SPACESCANS_A4_TOL=same` and `SPACESCANS_A4_PREFIX` are for the frozen-image check |

Every suite only reads these directories. The real-run suites check afterwards that nothing
in the data root is newer than a stamp taken first. Run them one at a time: they are real
pipeline jobs.

The three `SPACESCANS_WEB_*` suites compare this package with the old web code, web 3991c3b
(pipeline v0.2.7). The goldens come from that code. They read the old runners' internals
(`render_yaml`, `_cache_key`, the custom runner), so they need a checkout of that state.

- **Before the web cutover:** the primary checkout works, since web main (31c5dc3) has the
  same backend.
- **After the cutover:** the web's runners are shims over this package, and the suites fail
  on the missing internals (ImportError `_adapter`, no `render_yaml`). Point the three
  variables at a worktree of the tag `web-pipeline-0.2.7` (web 3991c3b) instead, for example
  `git -C $A/spacescans-web worktree add $A/spacescans-web-3991c3b web-pipeline-0.2.7`.

To run everything on this Mac:

```sh
A=/Users/xai/Desktop/spacescans-all
SPACESCANS_GOLDEN_DIR=$A/tier2-artifacts/goldens/web-0.2.7 \
SPACESCANS_WEB_REPO=$A/spacescans-web \
SPACESCANS_WEB_CONFIGS=$A/spacescans-web/configs \
SPACESCANS_WEB_BACKEND=$A/spacescans-web/backend \
SPACESCANS_CUSTOM_EXAMPLES=$A/custom_exposome_examples \
SPACESCANS_CUSTOM_R1=$A/spacescans-paper/results/data/validation/pipeline_outputs/custom \
SPACESCANS_R1_DIR=$A/spacescans-paper/results/data/validation/pipeline_outputs \
SPACESCANS_R1_COHORT=$A/spacescans-paper/results/data/cohorts/synth_1k.csv \
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Only the golden tests: add `-m golden` to the same command with `SPACESCANS_GOLDEN_DIR` set.

### Rules for these tests

- **Environment isolation.** `tests/conftest.py` removes every `SPACESCANS_*` variable while
  each test runs, so a variable exported in the shell cannot change results. Opt-in suites
  therefore read their variable at import, or in a session-scoped fixture.
- **Golden tests.** Mark them `golden`, or request the `golden_dir` fixture, which adds the
  marker.
  - `tests/conftest.py` skips them, without failing, unless `SPACESCANS_GOLDEN_DIR` names a
    directory with a `manifest.json`.
  - `golden_compare.golden_root()` and `golden_dir()` keep the caller's value inside tests.
  - Never read the manifest at import without first checking `golden_compare.golden_dir()`.
  - `tests/golden/README.md` has the pattern. The copy under `tests/golden/web-0.2.7` is
    incomplete and is never used implicitly.
- **CI** deselects the golden tests (`-m "not golden"` in every job). The other opt-in suites
  skip by themselves.

### R package tests

```sh
cd <worktree>/r
R_LIBS=/Users/xai/Desktop/spacescans-all/spacescans-wt/.Rlib \
SPACESCANS_PYTHON=<worktree>/.venv/bin/python NOT_CRAN=true \
  sh -c 'R CMD build --no-manual . && R CMD check --no-manual spacescans_*.tar.gz'
bash ../.github/scripts/r-check-status.sh spacescans.Rcheck/00check.log
```

- **Without `SPACESCANS_PYTHON`** every test that needs Python skips.
- **The live tests** (`tests/testthat/test-live.R`) run against the real bridge with
  `SPACESCANS_LIVE=1`; `SPACESCANS_LIVE_DATA_DIR=<data root>` adds the status checks on a real
  data root (read only, checked with a stamp) and `SPACESCANS_LIVE_LINK=1` one small real
  `ss_link()`.
- **The `ss_install()` test** also needs `SPACESCANS_TEST_INSTALL=true` and
  `SPACESCANS_TEST_WHEEL=<wheel>`.
- **The pass rule** is the one `.github/scripts/r-check-status.sh` applies: no ERROR, and
  no WARNING except the licence one, which stays until a licence is chosen.
- **In CI**, `.github/workflows/r-check.yml` runs the same steps on macOS 14 arm64 with R 4.6.
  It runs for changes under `r/`, `src/` or `pyproject.toml`, and as part of `publish.yml`'s
  gate.
