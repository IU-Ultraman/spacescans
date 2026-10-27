# Releasing spacescans (Python and R) — 0.3.0 checklist

Prepared 2026-10-16 on branch `tier2/d-release` (phase D, track F). Nothing here has been
published: no tag, no PyPI upload, no r-universe registry, no CRAN submission. Every step below
that changes something outside this repository is the authors' to perform.

The Python package (`spacescans` on PyPI, imported as `spacescans`) and the R package
in `r/` share one version and one tag, `v<version>`. The version is declared in three places,
which must agree: `pyproject.toml` (`version`), `r/DESCRIPTION` (`Version` and
`Config/spacescans/version`). `tests/test_r_package_version.py` checks them in CI, and
`publish.yml` refuses to run unless it was started from a tag that matches all three.

## 1. Decisions and accounts (authors)

- [x] **License (both repositories): MIT**, decided by the authors on 2026-10-16 and applied
  in the package at integration (`tier2/d-integration`): the root `LICENSE` (MIT, "Copyright (c)
  2026 spacescans authors"); `r/LICENSE` in CRAN form (`YEAR: 2026`, `COPYRIGHT HOLDER:
  spacescans authors`) with `License: MIT + file LICENSE` in `r/DESCRIPTION`; `license = "MIT"`
  and `license-files = ["LICENSE"]` in `pyproject.toml` with `setuptools>=77` (PEP 639; the
  wheel's METADATA carries `License-Expression: MIT` and `License-File: LICENSE`, and there is
  no `License ::` classifier, which newer setuptools rejects next to the expression); the
  README, `r/README.md` and the changelog. The R gate (`.github/scripts/r-check-status.sh`) no
  longer allows the license WARNING: any WARNING fails it. The web repository gets its own
  `LICENSE` in its integration branch.
- [ ] **Author metadata.** PyPI shows `authors = [{name = "Regenstrief Institute"}]` and no
  maintainer; the R package lists Xuguang Ai (`aut`, `cre`) and Regenstrief Institute (`aut`).
  Decide whether PyPI should name a maintainer, and the institute's R role (`cph` or `fnd` is
  what CRAN reviewers usually ask for an organization).
- [ ] **PyPI account** with two-factor authentication, to create the trusted publisher (section 3).
- [ ] **GitHub admin rights** on `IU-Ultraman/spacescans`, to create the `pypi` environment.
- [ ] **r-universe**: the r-universe GitHub app installed on the `IU-Ultraman` account, and the
  registry repository `IU-Ultraman/IU-Ultraman.r-universe.dev` (section 5).

Names: on 2026-10-16 `spacescans` and `spacescans-pipeline` were both unregistered on PyPI and on
TestPyPI. On 2026-10-23 the authors chose to publish the package under its own name, `spacescans`
(the distribution had been `spacescans-pipeline`, installed only from GitHub), so no alias is needed.

## 2. Before tagging (on `main`)

- [ ] Phase D merged, and the integration gates of PHASE_D.md section 6 pass.
- [x] **Data mirror upload** (done 2026-10-23: the author uploaded the 7 files at 10:30 EDT; all 7
  downloaded from the share matched the manifest's sha256, and `mirror-check --write-manifest`
  flipped them to `available`). Before, the data manifest marked 7 mirror files `pending`: the
  Neighborhood Deprivation Index panel (`.rds`), the EPA Walkability Index panel (`.Rda`), the
  ZBP and CBP panels (`.Rda`), the FARA table (`varnameCountRemoved.csv`) and the ACAG and TEMIS
  grid templates. Until they are on the mirror, `spacescans data fetch` cannot get `ndi`,
  `walkability`, `cbp_zcta5` or `fara_tract`, and reports the templates as not on the mirror
  (`spacescans data prepare` can build the two templates locally). The 7 files are staged and
  checked in `tier2-artifacts/mirror-upload/`. Upload them, unchanged, to the root of the
  OneDrive share `Documents/spacescans-data`, then run
  `spacescans data mirror-check --write-manifest` and commit the manifest change.
- [ ] CI is green on `main`: `test.yml`, `test-extras.yml` and `r-check.yml` (start `r-check.yml`
  with Run workflow if no push has triggered it on the release commit).
- [ ] **The release commit** (on a short branch, fast-forwarded into `main`):
  - `CHANGELOG.md`: replace "(unreleased)" with the date; `r/NEWS.md`: the same entry for R;
  - README "Install": PyPI becomes the first route (the PyPI page shows the README as it is at
    the tag, so it must not say "not on PyPI yet" there). Replace the paragraph from
    "spacescans is not on PyPI yet." to the PyPI code block with:

    ````markdown
    ```bash
    pip install "spacescans[geo]"
    ```

    The development version installs from GitHub:

    ```bash
    pip install "spacescans[geo] @ git+https://github.com/IU-Ultraman/spacescans.git"
    ```
    ````

  - if the version changes, change all three declarations.
- [ ] The README examples run: in a fresh environment with the release candidate installed and
  a data directory holding NPS Soundscape Noise (`spacescans data fetch noise`), the README's
  "Link" example returns one row per participant (`pid`, `l50dba_exi`, `l50dba_imp`,
  `l50dba_nat`; 3 rows, in order of first appearance), `output_level="episode"` returns 4
  rows with `episode_id`, and the participant values equal the day-weighted means of the
  episode values. On this machine, a run on catalog data takes the heavy lock
  (`tier2-artifacts/.heavy.lock`).

## 3. PyPI Trusted Publishing (once)

- [ ] On pypi.org: Account settings, Publishing, "Add a new pending publisher", GitHub:
  - PyPI project name: `spacescans`
  - Owner: `IU-Ultraman`
  - Repository name: `spacescans`
  - Workflow name: `publish.yml`
  - Environment name: `pypi`

  The first successful run creates the project, owned by the account that added the publisher.
- [ ] Optional, needs admin rights on `IU-Ultraman/spacescans` (an organization owner): Settings,
  Environments, New environment `pypi`: required reviewers (the authors), and deployment
  branches and tags limited to the tag pattern `v*`. Without it, GitHub creates the environment
  unprotected on the first run; the workflow still refuses to
  publish unless it was started from a `v*` tag that matches every declared version.

`publish.yml` needs no token: the publishing job asks for `id-token: write` and PyPI trusts the
workflow run.

## 4. Tag and publish

- [ ] On the release commit: `git tag -a v0.3.0 -m "spacescans 0.3.0"` and
  `git push origin v0.3.0`.
- [ ] GitHub, Actions, **publish**, Run workflow, "Use workflow from": **Tags, v0.3.0**. The
  workflow:
  1. `version-tag`: stops unless the ref is a `v*` tag equal to the three declared versions
     (started from a branch or a mismatched tag, it fails before anything else runs);
  2. the test gates: `test.yml` (Linux and macOS, Python 3.10–3.14), `test-extras.yml`, and
     `r-check.yml` (R CMD check on macOS 14 arm64);
  3. `build-and-publish`, in the environment `pypi` (it waits for a reviewer if one is set):
     `python -m build` with `SOURCE_DATE_EPOCH` = the tag commit's time, `twine check --strict`,
     upload to PyPI, and a GitHub release `v0.3.0` with the wheel and sdist attached and
     generated notes.
- [ ] Check the result in a fresh environment:
  `pip install "spacescans[geo]==0.3.0"`, `spacescans --help`,
  `python -c "import spacescans; print(spacescans.__version__)"` (0.3.0),
  `spacescans quickstart --output-dir /tmp/ss-qs`. The wheel is reproducible: rebuilding at the
  tag with `SOURCE_DATE_EPOCH=$(git log -1 --format=%ct v0.3.0) python -m build` gives the
  same sha256 as the wheel on PyPI.

## 5. The R package

R users of 0.3.0 can install the release from GitHub today:

```r
remotes::install_github("IU-Ultraman/spacescans", subdir = "r", ref = "v0.3.0")
spacescans::ss_install()
```

`remotes` and `pak` record the commit (`RemoteSha`) in the installed package, and
`ss_install()` installs `spacescans` from that commit's source archive on GitHub. An
install from r-universe (below) works the same way once the universe exists.

### r-universe

- [x] **`ss_install()` after an r-universe install** (fixed at integration): r-universe builds
  from git and records only `RemoteUrl`, `RemoteRef` and `RemoteSha` in the packages it builds
  (checked on 2026-10-16 in the DESCRIPTION of an r-universe source package: no `RemoteType`,
  `RemoteUsername` or `RemoteRepo`), and the wheel and `pin.json` are not in git.
  `.ss_github_source()` now reads the owner and repository from a github.com `RemoteUrl` and
  installs `spacescans` from the source archive of `RemoteSha` (without a recorded
  commit, from the tag `v<Config/spacescans/version>`); `r/tests/testthat/test-install.R`
  checks it on synthetic r-universe DESCRIPTION files. Still open for a CRAN version: a fallback
  to PyPI (`spacescans==<Config/spacescans/version>`) for installs without any git
  metadata.
- [ ] Create the public repository `IU-Ultraman/IU-Ultraman.r-universe.dev` with this
  `packages.json`:

  ```json
  [
    {
      "package": "spacescans",
      "url": "https://github.com/IU-Ultraman/spacescans",
      "subdir": "r",
      "branch": "*release"
    }
  ]
  ```

  `"branch": "*release"` builds the most recent GitHub release (the tag `v0.3.0` once the
  publish workflow has made the release); omit it to build the default branch instead.
- [ ] After the first build at `https://iu-ultraman.r-universe.dev`, users install with
  `install.packages("spacescans", repos = c("https://iu-ultraman.r-universe.dev", "https://cloud.r-project.org"))`.

### A release tarball with the wheel inside (optional)

Because the wheel is reproducible, the R package can carry the very file that is on PyPI and on
the GitHub release. At the tag, with a clean tree:

```sh
r/tools/build-wheel.sh --url https://github.com/IU-Ultraman/spacescans/releases/download/v0.3.0/spacescans-0.3.0-py3-none-any.whl
r/tools/lock.sh          # optional: hash-pinned dependencies for macOS arm64
R CMD build r            # spacescans_0.3.0.tar.gz with inst/python/{wheel,pin.json}
```

then attach `spacescans_0.3.0.tar.gz` to the GitHub release. Users install it with
`install.packages("<that URL>", repos = NULL, type = "source")`, and `ss_install()` installs
the bundled wheel after checking its sha256 against `pin.json`. Measured on the 0.3.0 commit:
`build-wheel.sh`'s `uv build` and `publish.yml`'s `python -m build` give byte-identical wheels
when `SOURCE_DATE_EPOCH` is the same commit time (both builds resolve the newest setuptools; a
setuptools release between the two builds could change the bytes, so compare the sha256 before
attaching). The lock should be generated, and checked, on macOS 14 and 15 runners.

### CRAN (later)

`R CMD check --as-cran` on the integrated 0.3.0 (section 7) reports no license WARNING or
NOTE any more (MIT is a FOSS license). What still stands between the package and CRAN:

1. **License.** Resolved: `MIT + file LICENSE`.
2. **Platforms.** `SystemRequirements` names macOS 14 or later on Apple silicon as the supported
   platform. CRAN builds and checks on Linux, Windows and macOS and expects packages to work on
   more than one platform. The Python package runs on Linux too (CI), but the R package has no
   Linux R CI, and Windows is unsupported (the bridge kills process groups, which is POSIX).
3. **Python and uv at run time.** `ss_install()` downloads a managed CPython with uv and the
   Python dependencies. That is acceptable only as an explicit user action, never in examples,
   tests or vignettes; today the examples that need Python are `\dontrun{}` (21 of the 23 Rd
   files with examples; the two others need no Python), the vignette chunks that need Python
   are not evaluated, and bridge tests skip without `SPACESCANS_PYTHON`. CRAN reviewers often
   ask to replace `\dontrun{}` by `\donttest{}` or an `if (interactive())` guard; a CRAN version
   also needs a way to find the Python package without git metadata (the PyPI fallback above).
4. **Files written.** Run directories and the Python environment live under
   `tools::R_user_dir()` (allowed when actively managed); the package deletes run directories
   whose owner process is gone when it loads, in `ss_doctor()` and in `ss_cache_clear()`. In
   the `--as-cran` run nothing of the package's was left in the user directories (they were
   redirected to a scratch folder). Reviewers may still ask about deleting files at load time.
5. **A bundled wheel.** CRAN reviewers may object to an archive of non-R code installed at run
   time; a CRAN tarball should not carry `inst/python/*.whl` and should rely on PyPI.
6. **DESCRIPTION wording.** Resolved at integration: the Title is "Link Time-Referenced
   Geolocation Data to the Spatial and Contextual Exposome", the Description speaks of location
   episodes and participants, and 'Python' is quoted. The Title has 76 characters; CRAN's
   guidance asks for a short title (older versions of R limited it to 65), so a reviewer may ask
   for a shorter one.

## 6. After the release

- [ ] The web application (`spacescans-web`) pins the package by commit in
  `backend/Dockerfile` (`SPACESCANS_REF`); move it to the release commit or to
  `spacescans==0.3.0` from PyPI through that repository's own cutover procedure.
- [ ] The manuscript's software citation (`spacescans2026python`) gets the PyPI URL.
- [ ] Start the next changelog entry.

## 7. Dry run of 0.3.0 (2026-10-16)

Recorded before the rename of 2026-10-23: the file names below are those of the distribution
`spacescans-pipeline` (now `spacescans`), and the alias it mentions no longer exists.

### Python

At commit 8e5b87e (`git archive` of the committed tree; build 1.6.1, twine 7.0.0, CPython
3.12, macOS arm64):

| Artifact | Size | Check |
| --- | --- | --- |
| `spacescans_pipeline-0.3.0-py3-none-any.whl` | 404,293 bytes; 200 files, 1,130,470 bytes uncompressed | `twine check --strict` PASSED |
| `spacescans_pipeline-0.3.0.tar.gz` | 419,336 bytes | `twine check --strict` PASSED |

- The wheel holds the `spacescans` package (bridge 14 files, catalog 34, custom 11, data 22,
  orchestrate 15, linkage 18, plugins 17, resources 23 with the synthetic sample data and
  templates, and the rest) and its metadata; no tests, caches or data beyond the bundled
  synthetic sample. The largest files are `data/_fetch.py` (50 kB) and `data/manifest.json`
  (34 kB).
- The sdist holds `src/`, `pyproject.toml`, `README.md` and the 47 top-level test modules
  (setuptools includes `tests/test*.py` by default; they are public in the repository already,
  and their fixtures are not in the sdist).
- Reproducibility: with `SOURCE_DATE_EPOCH` = the commit time, two `python -m build` runs and
  the `uv build --wheel` that `r/tools/build-wheel.sh` runs gave the same wheel sha256 (checked
  at commit 53e343b). The sdist differs by a few bytes between runs.
- Fresh environments, offline after installing the wheel:
  - CPython 3.12, base install: `import spacescans` (0.3.0), `spacescans --help`,
    `spacescans init-config --out ./configs` (4 templates), `spacescans data requirements noise`,
    `spacescans data status noise` on an empty data directory (reports the 3 missing rasters and
    the fetch command);
  - CPython 3.12 with `[geo]`: `spacescans quickstart --output-dir ./demo-out` wrote
    `demo-out/quickstart_county_weights.parquet` (10 rows: `geoid`, `GEOID10`, `value`) in
    16.5 s; `spacescans.capabilities()` reports 0.3.0;
  - CPython 3.10, base install: `spacescans`, `catalog`, `orchestrate`, `cohort`, `custom`,
    `data` and `bridge` import; the catalog lists 11 variables.

### R

`R CMD build` and `R CMD check --as-cran` of `r/` at commit 1ccaa9d (R 4.6.1, macOS 26.7.1
arm64; R code as on `main` d92f7a3 with `DESCRIPTION` at 0.3.0), with the R user directories
pointed at a scratch folder and without `SPACESCANS_PYTHON` or `NOT_CRAN`, as on CRAN:

- tarball `spacescans_0.3.0.tar.gz`, 110,563 bytes (no bundled wheel);
- `Status: 1 WARNING, 2 NOTEs`: the WARNING is "Non-standard license specification"; the NOTEs
  are the CRAN incoming check (new submission, non-FOSS license) and this machine's HTML Tidy
  being too old to validate the HTML manual (the tool, not the package); examples, vignette
  and PDF manual OK;
- tests: 493 passed, 41 skipped (the bridge tests without Python, the live and install tests,
  one `skip_on_cran`), 0 failed, 67 s.

The same check as the GitHub R gate (`NOT_CRAN=true`, `SPACESCANS_PYTHON` = an environment with
this branch's 0.3.0 Python package; `--no-manual`): `Status: 1 WARNING` (the license),
`.github/scripts/r-check-status.sh` passes, tests 596 passed, 18 skipped (live and install
tests), 0 failed.

### Integrated 0.3.0 (`tier2/d-integration`, 2026-10-16)

Phase D2 merged tracks A, B, E and F and applied the lead's decisions of PHASE_D 7.1 (the
blank-id rule, the participant-level documentation, both feature-distance routes, the MIT
license, the R Title, the PyPI alias, the r-universe install fix). Checked on the integrated
branch (CPython 3.12 on macOS arm64 unless noted):

- **Build** (`git archive` of the branch head, `SOURCE_DATE_EPOCH` = commit time, build 1.6.1,
  twine 7.0.0): `spacescans_pipeline-0.3.0-py3-none-any.whl` about 462 kB (209 files,
  1,306,683 bytes uncompressed; METADATA `License-Expression: MIT`, `License-File: LICENSE`)
  and `spacescans_pipeline-0.3.0.tar.gz` about 485 kB (with `LICENSE` and the 49 top-level
  test modules); the alias `spacescans-0.3.0-py3-none-any.whl` (2,494 bytes, metadata and
  `LICENSE` only) and its sdist. `twine check --strict` PASSED for all four.
- **Fresh environments from the wheels:** the alias wheel installed with `--find-links` to the
  dist folder brings `spacescans==0.3.0` and `spacescans-pipeline==0.3.0`; `import spacescans`
  reports 0.3.0; `spacescans --help` and `init-config` (4 templates) work; with `[geo]`,
  `spacescans quickstart` writes the 10-row weights table.
- **Python suites:** plain 1317 passed, 72 skipped, 1 xfailed, 0 failed; with the opt-in parity
  suites (goldens web-0.2.7, a web clone at 31c5dc3, the custom examples, the paper's R1
  outputs) 1724 passed, 26 skipped, 1 xfailed, 0 failed; GitHub's base job (`BASE_MARKERS`,
  base install, CPython 3.10) 1131 passed, 46 skipped, 1 xfailed, 0 failed; gate A4 on the real
  data root 15 passed (all 13 golden runs without a problem, 23 minutes); the real-data
  participant gate, the real bridge link and step runs and the registered-dataset links 20
  passed; the Linux CI image (`tier2-ci-linux:py312`, amd64, `--init`, all extras,
  `-m "not golden"`) 1309 passed, 67 skipped, 1 xfailed, 0 failed.
- **README example** on the real NPS noise data: 3 participant rows (`pid`, `l50dba_exi`,
  `l50dba_imp`, `l50dba_nat`, first-appearance order), 4 episode rows with `episode_id`, every
  participant value equal to the day-weighted mean of its episode values (1e-9). A registered
  daily county table linked on the real 2010 county boundaries matched an independent
  reference at both levels (1e-9, missing days included).
- **R:** `R CMD check --as-cran` of the built tarball (no `SPACESCANS_PYTHON` or `NOT_CRAN`; R
  user directories in a scratch folder): `Status: 2 NOTEs`, the CRAN incoming "New submission"
  and this machine's HTML Tidy; no license WARNING or NOTE; tests 700 passed, 47 skipped,
  0 failed. The GitHub R gate (`NOT_CRAN=true`, `SPACESCANS_PYTHON` = the integrated package,
  `--no-manual`): `Status: OK`, `r-check-status.sh` passes, tests 803 passed, 24 skipped,
  0 failed. The live suite with real links against the integrated package: 22 tests,
  111 expectations, 0 failed.
