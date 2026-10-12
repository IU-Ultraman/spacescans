# spacescans (R)

R client for the spacescans Python pipeline. Every call runs
`python -m spacescans.bridge` in a separate process and exchanges JSON request
and result files (tables as Parquet); see `docs/bridge-protocol.md` in this
repository. There is no reticulate dependency: a crash or Ctrl-C in Python
never takes the R session down. Imports: processx, jsonlite, nanoparquet.

Phase A (this version): `ss_install()`, `ss_use_python()`, `ss_doctor()`,
`ss_version()`, `ss_config_schema()`, `ss_validate()`, `ss_templates()`,
`ss_read_config()` / `ss_write_config()`, `ss_run()`, `ss_quickstart()`,
`ss_read_result()`. `ss_link()`, `ss_catalog()`, `ss_data_status()` and
`ss_fetch()` are stubs until phase B.

```r
library(spacescans)
ss_install()                 # once: uv venv + the bundled spacescans wheel
ss_doctor()
w <- ss_quickstart()         # bundled C3 demo
cfg <- ss_templates("c4/zbp")
res <- ss_run(cfg, data_dir = "~/ss-data", threads = 4)
```

Supported platform: macOS 14 or later on Apple silicon, Python 3.12.

## Versions

The R package and the Python package carry the same version and are
released with the same tag. `DESCRIPTION` records it twice (`Version` and
`Config/spacescans/version`) together with `Config/spacescans/protocol`.
On the first call in a session the package runs the bridge's `capabilities`
command and refuses a Python package of another version
(`options(spacescans.version_check = "warn")` accepts the same minor
version). It warns when the running code's fingerprint differs from the
bundled wheel's (an editable or modified install).
`tests/test_r_package_version.py` keeps the versions in step in Python CI.

## The bundled wheel

`tools/build-wheel.sh` builds the wheel from `git archive HEAD` (committed
files only) with `SOURCE_DATE_EPOCH` set to the commit time, so a commit
always gives the same bytes, and writes `inst/python/pin.json` (version,
sha256, code fingerprint, commit). It refuses a dirty tree and mismatched
versions. The wheel and pin.json are not committed; the release job runs the
script at the release commit (with `--url` of the wheel attached to the
GitHub release) before `R CMD build r`.

## Development

```sh
r/tools/build-wheel.sh                          # bundle the wheel
R CMD build r && R CMD check --no-manual spacescans_*.tar.gz
```

Tests that need Python run when `SPACESCANS_PYTHON` points at an environment
with spacescans installed (for example the repository's `.venv/bin/python`);
otherwise they skip. The `ss_install()` test also needs
`SPACESCANS_TEST_INSTALL=true` and `SPACESCANS_TEST_WHEEL=<wheel>`.

`R CMD check` reports one WARNING, "Non-standard license specification",
until a licence is chosen.
