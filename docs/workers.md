# Worker processes (`engine.workers`): wall time and memory

Two measurements on the same laptop and cohort: 0.3.1 added workers for the polygon overlap
and ACAG's NetCDF files, and 0.3.2 for the grid weights, the NHD and TIGER searches and TEMIS's
HDF4 files.

## 0.3.1: polygon overlap and ACAG's NetCDF files (2026-10-25)

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

## 0.3.2: grid weights, NHD and TIGER (2026-10-25)

Same cohort, settings and laptop as above, branch `parallel-workers` at c0bdcf5. Each source
ran three times in a row: in one process, with four workers, and in one process again. A
source's first run also brings its files into the operating system's file cache, which speeds
up the runs after it: NHD's Step 1 took 257.7 s the first time and 174.0 s the third, TIGER's
345.8 s and 252.5 s, while noise and VIIRS changed little. So the four-worker run is compared
with the third run, which found the files at least as warm. The 0.3.1 table compares the first
run with the second, and its speedups may include some of this effect. Raw numbers:
[workers-0.3.2-2026-10-25.csv](workers-0.3.2-2026-10-25.csv).

| Source (variables) | One process, first (s) | 4 workers (s) | One process, again (s) | Speedup | Step 1, again → 4 (s) | Peak memory, again → 4 (GB) |
|---|---|---|---|---|---|---|
| NPS Soundscape Noise (`noise`) | 71.9 | 26.7 | 66.3 | 2.5× | 60.6 → 21.1 | 5.39 → 5.31 |
| VIIRS Nighttime Lights (`vnl`) | 64.4 | 26.4 | 63.9 | 2.4× | 58.0 → 20.6 | 5.34 → 9.12 |
| NHD Bluespace (`nhd_bluespace`) | 265.5 | 58.4 | 181.9 | 3.1× | 174.0 → 50.4 | 2.01 → 4.90 |
| TIGER Road Distance (`tiger_proximity`) | 349.3 | 83.1 | 256.2 | 3.1× | 252.5 → 79.7 | 2.52 → 7.64 |

Step 1 ran 2.81 (VIIRS), 2.88 (noise), 3.17 (TIGER) and 3.45 (NHD) times faster with four
workers. Step 2 of these sources takes 3 to 8 s and does not use the workers. Peak memory rose
by up to 5 GB; each TIGER worker, for example, reads the county polygons and the roads of its
year.

**ACAG and TEMIS.** ACAG's Step 1 is grid weights too. Run on its own with the same cohort
(`spacescans run` of its rendered config), it took 52.5 s in one process, 18.1 s with four
workers and 52.7 s in one process again (2.91 times faster). TEMIS's Step 1 is grid weights as
well, 6.2 s in one process and 4.6 s with four workers at 10,000 episodes. TEMIS decodes its
daily files in workers only on its HDF4 path, which runs when no converted (parquet) copy
covers the request, for example when the data directory is read-only. One feature's 2,555
daily files (uvddc, 2013-2019, for the 3,212 cells of a 10,000-episode cohort) took 33.5 s in
one process, 8.2 s with four workers and 17.1 s in one process again.

**VIIRS files stay in one process.** A version that also read VIIRS's seven annual files in
workers saved nothing at 100,000 episodes (Step 2 took 6.0 s either way), so it was left out.
The VIIRS row above was measured without it; the other sources do not use that code.

**Same results.** Every Step 1 output was identical in the three runs, and so were ACAG's
Step 1 weights (234,181 rows) and TEMIS's HDF4 rows. NHD's and TIGER's exposures were
identical too. Noise's and VIIRS's exposures differed by at most 1.7e-13 absolute (8e-16
relative), no more than their two one-process runs differed from each other: the difference
comes from Step 2 in DuckDB.

**Caveats.** These are host numbers, as for 0.3.1, not the Linux container of Table 2. Swap
held 9.6 GB before the runs and did not grow during them.

## Reproducing

```sh
# 0.3.1 table
python tools/workers_bench.py <data_dir> synth_100k.csv <out_dir> 4 walkability ndi cbp_zcta5 fara_tract acag
# 0.3.2 table
python tools/workers_bench.py --again <data_dir> synth_100k.csv <out_dir> 4 noise vnl nhd_bluespace tiger_proximity
```

`<out_dir>/results.jsonl` gets one record per run (wall time, steps, peak memory, swap) and one
comparison per source. The run directories keep the step outputs; the exposures are written to
`exp_<key>_<label>.parquet`, where the label is `None` (one process), the number of workers, or
`again`. All of these hold cohort-derived values: delete them afterwards. Noise, NHD and TIGER
were measured with a copy of the script that differed only in its run labels.
