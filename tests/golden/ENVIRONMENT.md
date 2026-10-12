# Environment of the golden references (tier 2, P0)

The goldens in `web-0.2.7/` come from the running web backend container
`spacescans-web-backend-1`. Before any golden was produced, the container was frozen as an
image (critique #1: the backend image pins nothing, so a rebuild does not give this
environment back). All times are US Eastern (EDT).

## Frozen image

| | |
|---|---|
| Command | `docker commit -m "tier2 P0 freeze of the running backend (web 3991c3b, pipeline v0.2.7)" spacescans-web-backend-1 spacescans-web-backend:frozen-2026-10-12` |
| Frozen at | 2026-10-12 16:32:16 EDT (the commit took 7 s; no runner was active) |
| Image ID | `sha256:94b192a9fce12b4ef1c18e1a81a82ccf84e364bb397737f8108d068f7b9fa810` |
| Digest (RepoDigests) | `spacescans-web-backend@sha256:94b192a9fce12b4ef1c18e1a81a82ccf84e364bb397737f8108d068f7b9fa810` (local image store; never pushed) |
| Layers | 20 (the source image's 19 plus the container's writable layer `sha256:ea4f93e2…`) |
| Platform | linux/arm64, Docker Engine 29.2.1 on Apple Silicon (Docker VM) |
| Source image | `spacescans-web-backend:latest`, `sha256:8b219bc1b06776ca076fda1532fe329bb5e752ad16655b9cfe0bf54e8b5abac9`, built 2026-10-09 16:58:27 EDT |
| Container | `bbb70ec855f4…`, started 2026-10-09 16:58:45 EDT from that image, never restarted |

Full record: `env/image.json`.

**What the writable layer holds.** `docker diff` listed 6,196 entries, made up of
`__pycache__` files, pytest leftovers under `/tmp` (someone ran backend tests inside the
container), and the mount points. No source file in `/app` or in `site-packages` was changed
after the image was built. The two checks below confirm this.

**Not in the image.** `docker commit` does not capture volumes or bind mounts. The goldens
depend on three of them:

| Mount | Source | Mode | Pinned by |
|---|---|---|---|
| `/configs` (`SPACESCANS_CONFIG_TEMPLATES_DIR`) | host `spacescans-web/configs` | ro | web commit 3991c3b (clean tree); sha256 of all 28 files in `env/templates.json` |
| `/project` (`SPACESCANS_DATA_DIR`) | host `spacescans-web/pipeline-data` | rw | `env/data_inventory.json` (per-directory listing hashes, plus full sha256 of the value tables and templates the catalog reads) |
| `/app/data` | volume `spacescans-web_backend-data` | rw | the goldens use private task dirs and private C3 caches inside it; see README.md |

**Caveat: secret in the image config.** `docker commit` copies the container's runtime
environment into the image config. That environment includes a `SECRET_KEY` supplied by
docker compose; its value is not recorded here. Keep the image local. Before sharing it
(`docker save`, a registry), overwrite that variable, for example with
`docker commit --change 'ENV SECRET_KEY=…'` from a container started from it.

**Rollback** (critique #1): re-tag this image, never rebuild it.
`docker tag spacescans-web-backend:frozen-2026-10-12 spacescans-web-backend:latest`, then
`docker compose up -d --no-build backend`.

## Web application

`git -C spacescans-web log -1` (`env/web_commit.txt`):

```
commit 3991c3bc26ff2d198cad250a3820cc97fb851a13
Author: xuguang-ai <xuguang.ai@outlook.com>
Date:   Fri Oct 9 16:54:51 2026 -0400

    Build the backend against pipeline v0.2.7
```

- The checkout was clean: `git status --porcelain` printed nothing. It is on `main`.
- The image was built 4 minutes after this commit. The container's `/app` (with
  `__pycache__` and `data/` excluded) is byte-identical to the checkout's `backend/`:
  `diff -rq` over `app/`, plus `cmp` of `requirements.txt` and `Dockerfile`.

## Pipeline

| | |
|---|---|
| `spacescans.__version__` | `0.2.7` |
| Distribution | `spacescans-pipeline 0.2.7` from `git+https://github.com/IU-Ultraman/spacescans.git@v0.2.7` |
| Commit (`direct_url.json`) | `a31370fbee79874ec29c32b8fc732bb83ec28d6c` (tag v0.2.7, "Release 0.2.7") |
| Installed tree | byte-identical to `src/spacescans/` at a31370f (`diff -rq`, `__pycache__` excluded) |
| Extras | `[geo,rda,hdf4,nc]` (backend/Dockerfile:49-51) |

## Python and libraries

- **Python:** 3.12.13 (conda-forge), `/opt/conda/bin/python`, Linux 6.12.72-linuxkit aarch64, glibc 2.36.
- **Hardware:** 14 CPUs with no cgroup CPU quota (`cpu.max` = `max`); MemTotal 24,571,496 kB; no cgroup memory limit.
- **DuckDB defaults:** threads 14, `memory_limit` 18.7 GiB.

| Package | Version | | Native library | Version |
|---|---|---|---|---|
| pandas | 3.0.5 | | GDAL (rasterio; conda libgdal) | 3.12.3 |
| numpy | 2.5.1 | | GDAL (pyogrio wheel) | 3.12.4 |
| pyarrow | 26.0.0 | | PROJ (rasterio; conda proj) | 9.7.1 |
| duckdb | 1.5.6 | | PROJ (pyproj wheel) | 9.8.1 |
| shapely | 2.2.0 | | GEOS | 3.14.1 (CAPI 1.20.5) |
| geopandas | 1.2.0 | | HDF4 | 4.2.15 |
| rasterio | 1.5.0 | | netCDF-C | 4.10.0 |
| exactextract | 0.3.0 | | | |
| pyogrio | 0.13.0 | | | |
| pyproj | 3.8.0 | | | |
| pydantic | 2.10.3 | | | |
| pyyaml | 6.0.3 | | | |
| pyreadr | 0.5.7 | | | |
| pyhdf | 0.11.7 | | | |
| netCDF4 / xarray | 1.7.4 / 2026.9.0 | | | |

Two pairs of native libraries differ inside one environment, which matters for
cross-environment parity:
- **GDAL:** rasterio links conda's GDAL 3.12.3, while pyogrio's wheel bundles GDAL 3.12.4.
- **PROJ:** rasterio links conda's PROJ 9.7.1, while pyproj's wheel bundles PROJ 9.8.1.

Files:
- `env/pip-freeze.txt`: `/opt/conda/bin/python -m pip freeze`, 65 lines.
- `env/pip-freeze-all.txt`: the same with `--all`.
- `env/conda-explicit.txt`: `micromamba env export -n base --explicit`, 197 packages with URLs and md5. `env/conda-list.txt` lists the same packages.
- `env/versions.json`: the table above, as the container reported it.
- `env/image.json`: image and container identity, mounts, and the names (not values) of the configured environment variables.
- `env/templates.json`: sha256 of every template under `configs/`.
- `env/data_inventory.json`: fingerprint of the data root, taken 2026-10-12.
