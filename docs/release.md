# Releasing spacescans (Python and R) — 0.3.0 checklist

Prepared 2026-10-16 on branch `tier2/d-release` (phase D, track F). Nothing here has been
published: no tag, no PyPI upload, no r-universe registry, no CRAN submission. Every step below
that changes something outside this repository is the authors' to perform.

The Python package (`spacescans-pipeline` on PyPI, imported as `spacescans`) and the R package
in `r/` share one version and one tag, `v<version>`. The version is declared in three places,
which must agree: `pyproject.toml` (`version`), `r/DESCRIPTION` (`Version` and
`Config/spacescans/version`). `tests/test_r_package_version.py` checks them in CI, and
`publish.yml` refuses to run unless it was started from a tag that matches all three.

## 1. Decisions and accounts (authors)

- [ ] **License (both repositories).** Agents do not choose a license, and this branch leaves
  every license field as it was (`r/DESCRIPTION`: "Not yet licensed"; no `license` in
  `pyproject.toml`; no LICENSE file). `tier2-artifacts/docs/PHASE_D.md` (as updated
  2026-10-16 16:45 EDT) records MIT as the authors' decision and says the lead adds the files
  at integration. Applying a license means:
  - a `LICENSE` file at the root of each repository;
  - `r/LICENSE` in CRAN form for MIT (two lines, `YEAR:` and `COPYRIGHT HOLDER:`) and
    `License: MIT + file LICENSE` in `r/DESCRIPTION`;
  - `license = "<SPDX expression>"` in `pyproject.toml`'s `[project]`, with
    `requires = ["setuptools>=77", "wheel"]` in `[build-system]` (setuptools reads the string
    form from 77 on, PEP 639, and then picks up the root `LICENSE` by its default
    `license-files` patterns). Do not add a `License ::` classifier alongside the expression;
    newer setuptools rejects the combination;
  - the README's License section and the changelog's "Not yet decided" line;
  - optionally, removing the license allowance from `.github/scripts/r-check-status.sh`
    (once the license is standard it matches nothing, so it is harmless if left).
  These edits have not been built or checked on this branch.
- [ ] **Author metadata.** PyPI shows `authors = [{name = "Regenstrief Institute"}]` and no
  maintainer; the R package lists Xuguang Ai (`aut`, `cre`) and Regenstrief Institute (`aut`).
  Decide whether PyPI should name a maintainer, and the institute's R role (`cph` or `fnd` is
  what CRAN reviewers usually ask for an organization).
- [ ] **PyPI account** with two-factor authentication, to create the trusted publisher (section 3).
- [ ] **GitHub admin rights** on `IU-Ultraman/spacescans`, to create the `pypi` environment.
- [ ] **r-universe**: the r-universe GitHub app installed on the `IU-Ultraman` account, and the
  registry repository `IU-Ultraman/IU-Ultraman.r-universe.dev` (section 5).

Names on 2026-10-16: `spacescans-pipeline` and `spacescans` are both unregistered on PyPI and
on TestPyPI (`https://pypi.org/pypi/<name>/json` returns 404); there is no CRAN package
`spacescans`; there is no `iu-ultraman` universe and no registry repository yet.

## 2. Before tagging (on `main`)

- [ ] Phase D merged, and the integration gates of PHASE_D.md section 6 pass.
- [ ] **Data mirror upload.** The data manifest still marks 7 mirror files `pending`: the
  Neighborhood Deprivation Index panel (`.rds`), the EPA Walkability Index panel (`.Rda`), the
  ZBP and CBP panels (`.Rda`), the FARA table (`varnameCountRemoved.csv`) and the ACAG and TEMIS
  grid templates. Until they are on the mirror, `spacescans data fetch` cannot get `ndi`,
  `walkability`, `cbp_zcta5` or `fara_tract`, and reports the templates as not on the mirror
  (`spacescans data prepare` can build the two templates locally). The 7 files are staged and
  checked in `tier2-artifacts/mirror-upload/`. Upload them, unchanged, to the root of the
  OneDrive share `Documents/spacescans-pipeline-data`, then run
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
    pip install "spacescans-pipeline[geo]"
    ```

    The development version installs from GitHub:

    ```bash
    pip install "spacescans-pipeline[geo] @ git+https://github.com/IU-Ultraman/spacescans.git"
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
  - PyPI project name: `spacescans-pipeline`
  - Owner: `IU-Ultraman`
  - Repository name: `spacescans`
  - Workflow name: `publish.yml`
  - Environment name: `pypi`

  The first successful run creates the project, owned by the account that added the publisher.
- [ ] On GitHub, `IU-Ultraman/spacescans`, Settings, Environments, New environment `pypi`:
  required reviewers (the authors), and deployment branches and tags limited to the tag pattern
  `v*`. Without the environment, GitHub creates it unprotected on the first run.

`publish.yml` needs no token: the publishing job asks for `id-token: write` and PyPI trusts the
workflow run.

## 4. Tag and publish

- [ ] On the release commit: `git tag -a v0.3.0 -m "spacescans 0.3.0"` and
  `git push origin v0.3.0`.
- [ ] GitHub, Actions, **publish**, Run workflow, "Use workflow from": **Tags, v0.3.0**. The
  workflow:
  1. `version-tag`: stops unless the ref is a `v*` tag equal to the three declared versions
     (started from a branch or a mismatched tag, it fails before anything else runs);
  2. the test gates: `test.yml` (Linux and macOS, Python 3.10–3.13), `test-extras.yml`, and
     `r-check.yml` (R CMD check on macOS 14 arm64);
  3. `build-and-publish`, in the environment `pypi` (it waits for a reviewer if one is set):
     `python -m build` with `SOURCE_DATE_EPOCH` = the tag commit's time, `twine check --strict`,
     upload to PyPI, and a GitHub release `v0.3.0` with the wheel and sdist attached and
     generated notes.
- [ ] Check the result in a fresh environment:
  `pip install "spacescans-pipeline[geo]==0.3.0"`, `spacescans --help`,
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
`ss_install()` installs `spacescans-pipeline` from that commit's source archive on GitHub.

### r-universe

- [ ] **Blocker, R code (`r/R/install.R`):** r-universe builds from git and records only
  `RemoteUrl`, `RemoteRef` and `RemoteSha` in the packages it builds (checked on 2026-10-16 in
  the DESCRIPTION of an r-universe source package: no `RemoteType`, `RemoteUsername` or
  `RemoteRepo`). `.ss_github_source()` needs those three, the wheel and `pin.json` are not in
  git, so after an r-universe install `ss_install()` stops with "No spacescans 0.3.0 wheel is
  bundled with this R package". Before announcing r-universe, make `ss_install()` find the
  Python package for such installs, for example by reading `RemoteUrl`
  (`https://github.com/<owner>/<repo>`) with `RemoteSha` when `RemoteType` is absent, and by
  falling back to PyPI (`spacescans-pipeline==<Config/spacescans/version>`) once the version is
  published there.
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
r/tools/build-wheel.sh --url https://github.com/IU-Ultraman/spacescans/releases/download/v0.3.0/spacescans_pipeline-0.3.0-py3-none-any.whl
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

`R CMD check --as-cran` on 0.3.0 (section 7) reports the license WARNING and NOTE and nothing
else about the package. What still stands between the package and CRAN:

1. **License.** "Not yet licensed" is not a FOSS license: a WARNING and an incoming NOTE now,
   and CRAN accepts only FOSS licenses. Gone once a standard license is in `DESCRIPTION`.
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
6. **DESCRIPTION wording.** CRAN asks for software names in single quotes: 'Python' appears
   unquoted four times in `Description`. The Title and Description say "patient residential
   histories", while the 0.3.0 documentation speaks of participants and location episodes.

## 6. After the release

- [ ] The web application (`spacescans-web`) pins the package by commit in
  `backend/Dockerfile` (`SPACESCANS_REF`); move it to the release commit or to
  `spacescans-pipeline==0.3.0` from PyPI through that repository's own cutover procedure.
- [ ] The manuscript's software citation (`spacescans2026python`) gets the PyPI URL.
- [ ] Start the next changelog entry.

## 7. Dry run of 0.3.0 (2026-10-16)

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
