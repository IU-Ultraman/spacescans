# Benchmarks

Runtime and peak memory of the 11 catalog sources on a synthetic cohort of 100,000 location
episodes, with the cohort, its generator, and the scripts that time and summarise the runs.

| File | Content |
| --- | --- |
| `cohorts/synth_100k.csv` | The synthetic cohort: 100,000 location episodes of 51,015 participants across the conterminous United States (`pid`, `startDate`, `endDate`, `longitude`, `latitude`). |
| `cohorts/synth_summary.csv` | The generator's parameters and the realised shares (participants with more than one address, move distances, gaps) of the 1k, 10k and 100k cohorts. |
| `synth_cohort.py` | The seeded generator. Rerun on the 2010 census tract boundaries, it reproduces the cohorts byte for byte. |
| `runtime.py` | Times each source inside the backend container of [spacescans-web](https://github.com/IU-Ultraman/spacescans-web): wall-clock time, peak memory, and time by step, for an initial run and for a run reusing the cached spatial weights. |
| `results/runtime_server.csv` | The runs on a server node with AMD EPYC 7742 processors and 504 GB of memory, with four worker processes for the spatial computation, one initial run and one cached run per source; `results/runtime_server_machine.csv` describes the node. |
| `runtime_summary.py`, `runtime_table.py` | The summary per source and mode, the summary metrics, and the per-source table (`results/runtime_server_summary.csv`, `_metrics.csv`, `_table.csv`). |
| `catalog_sources.csv` | Spatial data type, domain, number of output variables and an example measure per source, for the table. |

Rebuild the summaries and the table from the runs:

```bash
python3 benchmarks/runtime_summary.py
python3 benchmarks/runtime_table.py
```

Regenerate the cohorts (needs the 2010 census tract boundaries, `spacescans data fetch --dataset
tract_boundaries`, in the data directory):

```bash
SPACESCANS_DATA_DIR=~/spacescans-data python benchmarks/synth_cohort.py
```

Time the sources on another machine (needs the spacescans-web backend container running, with
the catalog data in place; the 100k runs take several hours):

```bash
python3 benchmarks/runtime.py --sizes 100k --repeats 1 --workers 4 \
    --out benchmarks/results/runtime_mymachine.csv
python3 benchmarks/runtime_summary.py --src benchmarks/results/runtime_mymachine.csv
python3 benchmarks/runtime_table.py --src benchmarks/results/runtime_mymachine_summary.csv
```
