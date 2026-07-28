# spacescans

**Config-driven environmental-exposure linkage for EHR patient cohorts.**

Link environmental exposures — air quality, greenness, noise, roads, neighborhood
indices, and more — to patient address histories, defined entirely in YAML and
computed on a fast DuckDB engine. Built for reproducible exposome research at scale.

[![Python 3.10+](https://img.shields.io/badge/python-3.10--3.12-blue.svg)](https://www.python.org/)
[![Website](https://img.shields.io/badge/website-spacescans.com-1f6feb.svg)](https://www.spacescans.com)

🌐 **Website:** [www.spacescans.com](https://www.spacescans.com)

---

## Why spacescans

- **Config-driven, not code-driven** — describe each linkage in a YAML file; one engine runs them all.
- **Reusable two-stage design** — build geoid-level weight/exposure tables once (**C3**), then link them to patient episodes as time- and area-weighted exposures (**C4**).
- **Batteries-included exposure readers** — air quality (FAQSD, ACAG, TEMIS), greenness (MODIS NDVI), light & noise (VNL, noise), road & blue-space proximity (TIGER, NHD), neighborhood indices (NDI, walkability, CBP/ZBP), and more.
- **Light core, optional extras** — base install is just pandas + DuckDB; geospatial / R / HDF4 / NetCDF support is opt-in.
- **Fast** — DuckDB aggregation engine with per-tile bulk rasterization.

## Quickstart

```bash
pip install 'spacescans-pipeline[geo]'
spacescans quickstart --output-dir ./demo-out
```

This runs a full **C3 → C4** pipeline on bundled **synthetic** sample data
(~10 fake patients inside a Delaware-shaped bounding box) and writes a Parquet
result to `./demo-out/`. No real or licensed data required — try it in seconds.

## Installation

- **Install name:** `spacescans-pipeline` · **import as:** `spacescans`
- **Requires:** Python 3.10+

```bash
pip install spacescans-pipeline             # core: pandas + DuckDB
pip install 'spacescans-pipeline[geo]'      # + geospatial pipelines
pip install 'spacescans-pipeline[all]'      # everything
```

```bash
# Install the latest from source (before the first PyPI release)
pip install "spacescans-pipeline[geo] @ git+https://github.com/IU-Ultraman/spacescans.git"
```

<details>
<summary><b>Optional extras</b> — what each unlocks (click to expand)</summary>

| Extra | Unlocks | Requires |
| --- | --- | --- |
| (base) | `yearly_areal`, `static_areal`, `cbp_fallback`, `faqsd`, `precomputed_areal`, `precomputed_static` | pandas + duckdb only |
| `[geo]` | `boundary_overlap_fast`, `grid_weights`, `gridded`, `*_proximity` | geopandas / rasterio / shapely / exactextract |
| `[rda]` | any reader for `.Rda` files (NDI / Walkability / CBP-ZBP / FARA / UCR) | pyreadr |
| `[hdf4]` | TEMIS reader | system HDF4 library |
| `[nc]` | ACAG multi-pollutant reader | xarray / netCDF4 |
| `[all]` | everything above | — |

**Native libraries:** `[hdf4]` needs the system HDF4 library
(`apt install libhdf4-dev` / `brew install hdf4` / `conda install -c conda-forge hdf4`).
`[geo]` works on most platforms via wheels; conda/mamba is recommended for production.

</details>

## How it works

The pipeline runs in two stages, both driven by YAML configs and the same DuckDB engine:

1. **C3 — build weights/exposures** (needs `[geo]`): turn raw geospatial inputs into reusable
   `geoid`-level weight or exposure tables.
2. **C4 — link to patients** (base install): consume the C3 tables and attach exposures to
   patient address episodes as time- and area-weighted values.

Because C3 outputs are reusable, you build a weight table once and reuse it across many C4 linkages.

## Linkage patterns

### C3 — weight / exposure building (`[geo]`)

| Pattern | What it does | Typical data |
| --- | --- | --- |
| `boundary_overlap_fast` | Patient 270 m buffer × polygon area-overlap weights (per-tile bulk rasterize) | BG, County, Tract, ZCTA5 |
| `grid_weights` | Patient buffer × raster cell coverage weights | ACAG, Noise, PRISM, TEMIS, VNL, MOD13Q1 |
| `tiger_proximity` | Nearest road distance per (geoid, year) | TIGER roads |
| `nhd_proximity` | Nearest blue-space distance per geoid | NHD |

### C4 — patient linkage (base install)

Pure table operations (DuckDB/pandas) over the C3 weight tables. Patterns differ along two axes:
**how exposure varies over time** × **whether spatial aggregation is still needed**.

| Pattern | Time | Spatial | What it does | Typical data |
| --- | --- | --- | --- | --- |
| `yearly_areal` | Yearly | Area-weighted | Boundary area weights + residence-days per geoid → yearly time × area weighted mean | NDI, UCR, ZBP |
| `static_areal` | Static | Area-weighted | Single static value, area-weighted by residence days | Walkability, Noise |
| `cbp_fallback` | Yearly | Area-weighted | Like `yearly_areal`, but ZBP (ZIP-level) first, falling back to county CBP | County/ZCTA5 business patterns |
| `faqsd` | Daily | Area-weighted | Daily air quality (O₃/PM2.5); episode × daily values with day-level overlap | TRACT FAQSD |
| `precomputed_areal` | Yearly | Precomputed | Already a `geoid × year` table; time-weighted by residence days | TIGER road distance |
| `precomputed_static` | Static | Precomputed | Already a `geoid`-level table; weighted average by episode days | NHD blue-space distance |

> **`*_areal` vs `precomputed_*`:** `*_areal` re-aggregates using the C3 boundary weights;
> `precomputed_*` exposures are already at the geoid level (e.g. TIGER/NHD distances), so the
> spatial step is skipped.
>
> A few C4 patterns need extras beyond base: `gridded` (`[geo]`), `acag_multi` (`[nc]`+`[geo]`),
> `fara_tract` (`[rda]`).

## Running on your own data

```bash
export SPACESCANS_DATA_DIR=/path/to/exposome-data
export SPACESCANS_OUTPUT_DIR=/path/to/results

# Scaffold editable configs, then point their paths at ${SPACESCANS_DATA_DIR}/...
spacescans init-config

# Run the pipelines in dependency order
spacescans run ./configs/c3/county.yaml
spacescans run ./configs/c3/zcta5.yaml
spacescans run ./configs/c4/zbp.yaml            # depends on C3 zcta5
spacescans run ./configs/c4/cbp_fallback.yaml   # depends on C3 county + C4 zbp
```

Paths resolve in three tiers: **CLI flag > `$SPACESCANS_DATA_DIR` > YAML `base_dir:`**,
with `${SPACESCANS_DATA_DIR}` expansion inside YAML paths.

## Project layout

```text
src/spacescans/
├── cli/                 run / quickstart / init-config
├── config_resolution.py 3-tier path resolution (CLI > env > YAML) + ${VAR} expansion
├── pipeline/            config loader + pattern registry + runner
├── models/              pydantic config schema, protocols, operation specs
├── engine/              DuckDB aggregation engine
├── io/                  readers (parquet/csv/.Rda) + writers
├── geometry/            buffers, overlap, grid weights, proximity ([geo])
├── transforms/          filter / recode / derive / date_parse / spatial_fix
├── linkage/             linkage-pattern implementations
├── plugins/             dataset-specific readers (extras-gated)
└── resources/           bundled sample data + template configs
```

`import spacescans` pulls in only base dependencies; optional readers/patterns raise a
friendly `MissingExtraError` telling you exactly which `pip install 'spacescans-pipeline[...]'` to run.

## Development

```bash
pip install -e '.[all,dev]'
pytest                  # full suite (base + any extras present in the env)
pytest -m "not extras"  # base-only tests
pytest -m geo           # geo-extra tests
```

---

Learn more at [www.spacescans.com](https://www.spacescans.com).
