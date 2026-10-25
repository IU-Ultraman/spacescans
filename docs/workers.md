# Worker processes (`engine.workers`): wall time and memory (measured 2026-10-25)

One cold run per source and setting on the 100,000-episode synthetic cohort `synth_100k`:
`orchestrate.link()` with the SubprocessExecutor, buffer 270 m, raster resolution 25 m, no
weight cache, episode-level output (as in the paper's Table 2). Each source ran with
`workers=None` (one process) and then `workers=4`, back to back. Branch `parallel-workers`
(5b9f0dc) on the host laptop: Apple M4 Pro (10 performance and 4 efficiency cores), 48 GiB,
macOS 26.7.1, Python 3.12.13, data root of the web deployment, one benchmark at a time.

Peak memory is the largest sum of resident set sizes over the benchmark's process tree (the
steps and their workers), sampled every 0.5 s with `ps`; the largest single process is in the
CSV. Raw numbers: [workers-2026-10-25.csv](workers-2026-10-25.csv).

| Source (variables) | One process (s) | 4 workers (s) | Speedup | Step 1, one → 4 (s) | Peak memory, one → 4 (GB) |
|---|---|---|---|---|---|
| EPA Walkability Index (`walkability`) | 270.8 | 87.0 | 3.1× | 268.1 → 81.8 | 8.85 → 9.36 |
| Neighborhood Deprivation Index (`ndi`) | 538.8 | 172.1 | 3.1× | 529.5 → 162.5 | 9.79 → 10.45 |
| Community Organization Density (`cbp_zcta5`) | 519.0 | 161.3 | 3.2× | 511.9 → 154.6 | 9.01 → 8.89 |
| FARA Food Access (`fara_tract`) | 542.0 | 376.1 | 1.4× | 242.2 → 74.0 | 8.05 → 8.71 |
| ACAG PM2.5 Composition (`acag`) | 1,507.7 | 780.1 | 1.9× | 51.4 → 55.3 | 13.73 → 14.71 |

Step 1 of the polygon sources (`boundary_overlap_fast`: block groups of 2010 and 2020, ZCTAs,
counties, tracts) ran 3.2–3.4 times faster with four workers. ACAG gains in Step 2, where its
2,912 NetCDF files are decoded (1,455.9 → 724.4 s); the rest of that step, the joins in DuckDB,
does not use the workers. FARA's Step 2 (300 s) does not use them either.

**Same results.** Every Step 1 output (`c3_*.parquet`) was identical with one and four workers.
FARA's exposures were identical; for the others the largest difference was 1.8e-12 absolute
and 7e-14 relative. The difference comes from Step 2, which does not use the workers. Step 2
runs in DuckDB, whose threads can add floating-point numbers in a different order and return
rows in a different order. This also happens between two one-process runs.

**Caveats.**
- These are host numbers, not the Linux container of Table 2 (24 GiB, 14 CPUs, pipeline 0.2.7).
  The host was faster for the same one-process runs: ACAG took 1,508 s here and 2,229 s there.
- Swap use grew during the two ACAG runs, by 0.5 GB with one process and 1.2 GB with four
  workers, and macOS RSS leaves out compressed pages. Treat the ACAG timings and memory as
  approximate.
- In the container, ACAG's one-process peak is 20.2 GiB. Four workers add about 1 GB on the
  host, which would still fit in 23.4 GiB, but this has not been measured in the container.

## Reproducing

```sh
python tools/workers_bench.py <data_dir> synth_100k.csv <out_dir> 4 walkability ndi cbp_zcta5 fara_tract acag
```

`<out_dir>/results.jsonl` gets one record per run (wall time, steps, peak memory, swap) and one
comparison per source. The run directories keep the step outputs; the exposures are written to
`exp_<key>_<workers>.parquet`. All of these hold cohort-derived values: delete them afterwards.
