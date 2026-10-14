# Memory and wall time per source and cohort size (gate M, measured 2026-10-14)

Peak resident set size (GiB) and wall time (s) of one cold run per source, old web runner (`spacescans-web-backend:frozen-2026-10-12`: web 3991c3b + pipeline 0.2.7) against the new `orchestrate.link` run in the same image with the package mounted at `/src`. One run at a time in an ephemeral container of that image, Docker Desktop's VM with 24 GiB (23.4 GiB `MemTotal`), 14 CPUs, buffer 270 m, raster resolution 25 m, cold C3 cache, cohorts `synth_1k` (1,001 episodes), `synth_10k` (10,002) and `synth_100k` (100,000).

Peak = `RUSAGE_CHILDREN` `ru_maxrss` around the runner or orchestrator process: the largest resident set of any process it waited for (the pipeline steps run one at a time). Old-runner numbers marked *paper* are the medians of the cold repeats in the paper's `results/data/runtime.csv` (same image contents, same container memory, same wrapper position; 10k: 3 repeats, 100k: 1 run); the others were re-measured here. New-path step peaks come from the executor's `wait4`. The gate is new <= old + 5% per source and size.

## Peak RSS (GiB)

| Source (variables) | 1k old | 1k new | 10k old | 10k new | 100k old | 100k new |
|---|---|---|---|---|---|---|
| `noise` (noise) | 3.49 | 3.49 | 3.54* | 3.54 | 3.97* |  |
| `temis` (temis) | 4.30* |  | 10.02* | 9.59 | 22.51* |  |
| `vnl` (vnl) | 1.71* |  | 2.20* | 2.12 | 4.43* |  |
| `custom` (custom_raster) |  |  | 0.65 | 0.65 |  |  |
| `custom` (custom_bg_csv) |  |  | 6.48 | 6.48 |  |  |
| `nhd_bluespace` (nhd_bluespace) | 1.11* |  | 1.28* | 1.25 | 1.28* |  |
| `tiger_proximity` (tiger_proximity) | 1.23* |  | 1.47* | 1.47 | 1.72* |  |
| `fara_tract` (fara_tract) | 3.68* |  | 3.78* | 3.78 | 6.57* |  |
| `zcta5_cbp` (cbp_zcta5) | 5.36* |  | 5.38* | 5.38 | 6.67* |  |
| `bg_ndi_wi` (ndi, walkability) | 6.75* |  | 6.85* | 6.86 | 8.01* |  |
| `faqsd` (faqsd) | 4.36* |  | 16.83* | 16.80 | 22.57* |  |
| `acag` (acag) | 0.93* |  | 2.82* | 2.85 | 20.20* |  |

\* paper `runtime.csv` (old runner, not re-measured). An old-runner value at 100k for `temis` and `faqsd` is the peak before the kernel killed the C4 step.

## Wall time (s)

| Source (variables) | 1k old | 1k new | 10k old | 10k new | 100k old | 100k new |
|---|---|---|---|---|---|---|
| `noise` (noise) | 6.0 | 6.5 | 12.6 | 13.4 | 84.9 |  |
| `temis` (temis) | 8.5 |  | 34.6 | 72.6 | 130.3 |  |
| `vnl` (vnl) | 14.2 |  | 21.9 | 22.6 | 96.6 |  |
| `custom` (custom_raster) |  |  | 10.3 | 10.7 |  |  |
| `custom` (custom_bg_csv) |  |  | 110.7 | 114.8 |  |  |
| `nhd_bluespace` (nhd_bluespace) | 31.0 |  | 133.2 | 199.0 | 387.2 |  |
| `tiger_proximity` (tiger_proximity) | 47.0 |  | 89.7 | 243.9 | 406.5 |  |
| `fara_tract` (fara_tract) | 34.7 |  | 124.9 | 132.3 | 582.2 |  |
| `zcta5_cbp` (cbp_zcta5) | 37.9 |  | 162.8 | 185.5 | 556.8 |  |
| `bg_ndi_wi` (ndi, walkability) | 70.9 |  | 217.1 | 254.4 | 606.5 |  |
| `faqsd` (faqsd) | 217.0 |  | 559.0 | 641.7 | 459.0 |  |
| `acag` (acag) | 827.7 |  | 944.1 | 967.9 | 2228.6 |  |

## Gate: new <= old + 5% (peak RSS)

| Size | Source | Old GiB | Old from | New GiB | Orchestrator GiB | Worst step (GiB) | New/old | Old s | New s | Setup s | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1k | `noise` | 3.49 | measured | 3.49 | 0.20 | c4_noise (3.49) | 1.000 | 6.0 | 6.5 | 0.4 | PASS |
| 10k | `noise` | 3.54 | paper runtime.csv (median of 3 cold runs) | 3.54 | 0.23 | c4_noise (3.54) | 1.000 | 12.6 | 13.4 | 0.2 | PASS |
| 10k | `temis` | 10.02 | paper runtime.csv (median of 3 cold runs) | 9.59 | 0.23 | c4_temis (9.59) | 0.958 | 34.6 | 72.6 | 34.5 | PASS |
| 10k | `vnl` | 2.20 | paper runtime.csv (median of 3 cold runs) | 2.12 | 0.22 | c3_vnl (2.12) | 0.964 | 21.9 | 22.6 | 0.5 | PASS |
| 10k | `custom_raster` | 0.65 | measured | 0.65 | 0.39 | c3_customgrid_3940345e (0.65) | 0.999 | 10.3 | 10.7 | 1.2 | PASS |
| 10k | `custom_bg_csv` | 6.48 | measured | 6.48 | 0.22 | c3_bg (6.48) | 1.000 | 110.7 | 114.8 | 2.5 | PASS |
| 10k | `nhd_bluespace` | 1.28 | paper runtime.csv (median of 3 cold runs) | 1.25 | 0.24 | c3_nhd_bluespace (1.25) | 0.976 | 133.2 | 199.0 | 53.8 | PASS |
| 10k | `tiger_proximity` | 1.47 | paper runtime.csv (median of 3 cold runs) | 1.47 | 0.23 | c3_tiger_roads (1.47) | 1.000 | 89.7 | 243.9 | 78.8 | PASS |
| 10k | `fara_tract` | 3.78 | paper runtime.csv (median of 3 cold runs) | 3.78 | 0.26 | c3_tract_us (3.78) | 1.001 | 124.9 | 132.3 | 0.9 | PASS |
| 10k | `zcta5_cbp` | 5.38 | paper runtime.csv (median of 3 cold runs) | 5.38 | 0.25 | c3_zcta5 (5.38) | 1.001 | 162.8 | 185.5 | 0.5 | PASS |
| 10k | `bg_ndi_wi` | 6.85 | paper runtime.csv (median of 3 cold runs) | 6.86 | 0.22 | c3_bg_2020 (6.86) | 1.000 | 217.1 | 254.4 | 0.8 | PASS |
| 10k | `faqsd` | 16.83 | paper runtime.csv (median of 3 cold runs) | 16.80 | 0.22 | c4_tract_faqsd (16.80) | 0.999 | 559.0 | 641.7 | 0.7 | PASS |
| 10k | `acag` | 2.82 | paper runtime.csv (median of 3 cold runs) | 2.85 | 0.26 | c4_acag (2.85) | 1.008 | 944.1 | 967.9 | 9.2 | PASS |

Setup s = from the orchestrator's start to its first `spawning` line: `link`'s data readiness check (`data.status`), the custom-definition check and the cohort conversion. The old runner has no readiness check (the paper's `stage_prep_s` is 0.0 for every run).

Not measured on the new path: 100k `noise`, 100k `temis`, 100k `vnl`, 100k `nhd_bluespace`, 100k `tiger_proximity`, 100k `fara_tract`, 100k `zcta5_cbp`, 100k `bg_ndi_wi`, 100k `faqsd`, 100k `acag`.

## Findings

- Peak RSS: the largest new/old ratio is 1.008 (10k `acag`); the gate allows 1.05. 13 of 13 compared runs pass.
- Wall time: these runs took more than 25% longer on the new path: 10k `temis` 34.6 -> 72.6 s (setup 34.5 s); 10k `nhd_bluespace` 133.2 -> 199.0 s (setup 53.8 s); 10k `tiger_proximity` 89.7 -> 243.9 s (setup 78.8 s). Where the setup time accounts for the difference, the cost is `link`'s readiness check (`data.status`): its layout check lists every file of the dataset and stats it, and on Docker Desktop's bind mount each stat costs about 3.4 ms (TEMIS: 10,259 files; the prebuilt NHD tile cache: 14,857; the TIGER tile cache: 22,620). On the host the same calls take under 0.1 s. A receipt does not avoid it (the receipt check stats every listed file too). The web adapter calls `run_source`, not `link`, so the web does not pay it. The step times themselves vary from run to run as much as this on the old runner too (the paper's 10k cold repeats: `tiger_proximity` 204, 86 and 90 s; `nhd_bluespace` 187, 131 and 133 s), so wall time is shown, not gated.
- Wall time, like for like: where both paths were measured here, back to back in the same kind of container (1k `noise` 6.0 -> 6.5 s, setup 0.4 s; 10k `custom_raster` 10.3 -> 10.7 s, setup 1.2 s; 10k `custom_bg_csv` 110.7 -> 114.8 s, setup 2.5 s), the new path took 3% to 8% longer. The paper's harness takes its old-runner times with nothing else on the machine (`runtime.py`); the new-path runs here shared the host with other work (the heavy lock keeps only heavy runs apart), so the other rows are not a controlled wall-time comparison.

## Per-step peaks on the new path (GiB, executor `wait4`)

| Size | Source | Steps |
|---|---|---|
| 1k | `noise` | `c3_noise` 0.33 GiB, 1.5 s, ran; `c4_noise` 3.49 GiB, 4.3 s, ran |
| 10k | `noise` | `c3_noise` 0.65 GiB, 8.3 s, ran; `c4_noise` 3.54 GiB, 4.4 s, ran |
| 10k | `temis` | `c3_temis` 0.64 GiB, 6.6 s, ran; `c4_temis` 9.59 GiB, 30.9 s, ran |
| 10k | `vnl` | `c3_vnl` 2.12 GiB, 11.3 s, ran; `c4_vnl` 1.78 GiB, 10.6 s, ran |
| 10k | `custom_raster` | `c3_customgrid_3940345e` 0.65 GiB, 8.3 s, ran; `c4_custom_714c9715` 0.34 GiB, 0.6 s, ran |
| 10k | `custom_bg_csv` | `c3_bg` 6.48 GiB, 111.9 s, ran; `c4_custom_18180893` 0.34 GiB, 0.7 s, ran |
| 10k | `nhd_bluespace` | `c3_nhd_bluespace` 1.25 GiB, 143.4 s, ran; `c4_nhd_bluespace` 0.29 GiB, 1.9 s, ran |
| 10k | `tiger_proximity` | `c3_tiger_roads` 1.47 GiB, 164.3 s, ran; `c4_tiger_roads` 0.32 GiB, 1.0 s, ran |
| 10k | `fara_tract` | `c3_tract_us` 3.78 GiB, 90.9 s, ran; `c4_tract_fara` 1.87 GiB, 40.0 s, ran |
| 10k | `zcta5_cbp` | `c3_zcta5` 5.38 GiB, 103.0 s, ran; `c3_county` 1.08 GiB, 78.8 s, ran; `c4_zcta5_cbp` 0.63 GiB, 1.7 s, ran; `c4_county_cbp` 0.56 GiB, 1.0 s, ran |
| 10k | `bg_ndi_wi` | `c3_bg` 6.48 GiB, 120.9 s, ran; `c3_bg_2020` 6.86 GiB, 124.6 s, ran; `c4_ndi` 1.57 GiB, 6.7 s, ran; `c4_wi` 0.38 GiB, 1.1 s, ran |
| 10k | `faqsd` | `c3_tract_us` 3.78 GiB, 104.4 s, ran; `c4_tract_faqsd` 16.80 GiB, 536.1 s, ran |
| 10k | `acag` | `c3_acag` 0.65 GiB, 7.1 s, ran; `c4_acag` 2.85 GiB, 951.1 s, ran |

## Files

- `docs/memory-2026-10-14.csv`: every run and step (old and new; the old-runner totals from the paper where not re-measured).
- `src/spacescans/orchestrate/memory_table.csv`: the per-source table the out-of-memory hint reads (`spacescans.orchestrate.memory`): a `step_failed` with exit code -9/137 then says how much memory that source needed at the nearest measured cohort size.
- `tools/memory_gate.py`, `tools/memory_gate_measure.py`: how the numbers were produced.

## Reproducing

```sh
# the package tree to measure, mounted read-only at /src (here: a snapshot of main)
git archive <rev> src | tar -x -C /scratch/pkg && echo <rev> > /scratch/pkg/REV
python3 tools/memory_gate.py run --stage /scratch/stage --sizes 10k --modes new --src /scratch/pkg/src
python3 tools/memory_gate.py run --stage /scratch/stage --sizes 10k --cases custom_raster,custom_bg_csv --modes old
python3 tools/memory_gate.py run --stage /scratch/stage --sizes 100k --cases noise,vnl,temis,faqsd,fara_tract,bg_ndi_wi,zcta5_cbp,tiger_proximity,nhd_bluespace,acag --modes new --src /scratch/pkg/src
python3 tools/memory_gate.py report --stage /scratch/stage --date 2026-10-14
```

Each run is one `docker run --rm` of the frozen image under the host-wide heavy lock, with the data root and the web templates mounted read-only and a scratch mount at `/app/data` for the task dir, the cache and the cohort copy (deleted after the run). `<stage>/<size>/<case>/<mode>/record.json` holds every number above; `keep/` holds `logs.jsonl`, `status.json`, the rendered configs and the cache sidecars; `data_newer` in the record lists files under the data root newer than the run's stamp (it must be empty).

