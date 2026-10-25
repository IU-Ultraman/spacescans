"""Wall time and memory of orchestrate.link() per source, one process vs N workers (docs/workers.md).

Each source runs with workers=None and then workers=N, back to back, cold (no weight
cache), at the episode level (as Table 2 was measured), with the real SubprocessExecutor.
The RSS of this process tree is sampled every 0.5 s (sum over processes and the largest
one). One JSON line per run goes to results.jsonl as soon as the run ends. Step 1 outputs
are compared exactly, the exposures by their largest difference (Step 2 runs in DuckDB,
whose threads may add floats in another order). Prints no cohort values.

  python tools/workers_bench.py <data_dir> <cohort.csv> <out_dir> <N> key [key ...]
"""
import json, os, platform, subprocess, sys, threading, time
from pathlib import Path


def tree_rss(root: int) -> tuple[int, int, int]:
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True).stdout
    rss, kids = {}, {}
    for line in out.splitlines():
        pid, ppid, kib = (int(x) for x in line.split())
        rss[pid] = kib
        kids.setdefault(ppid, []).append(pid)
    total = biggest = n = 0
    stack = [root]
    while stack:
        p = stack.pop()
        if p in rss:
            total += rss[p]; biggest = max(biggest, rss[p]); n += 1
        stack.extend(kids.get(p, []))
    return total * 1024, biggest * 1024, n


class Sampler(threading.Thread):
    def __init__(self, interval: float = 0.5):
        super().__init__(daemon=True)
        self.interval, self.halt = interval, threading.Event()
        self.peak_total = self.peak_max = self.peak_n = 0

    def run(self):
        me = os.getpid()
        while not self.halt.is_set():
            t, m, n = tree_rss(me)
            self.peak_total, self.peak_max = max(self.peak_total, t), max(self.peak_max, m)
            self.peak_n = max(self.peak_n, n)
            self.halt.wait(self.interval)


def swap_used_mb() -> float:
    out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
    used = out.split("used =")[1].split()[0]
    return float(used.rstrip("M"))


def run_one(key, workers, cohort, data_dir, out_dir):
    import spacescans
    from spacescans.orchestrate import link
    sampler = Sampler()
    swap0 = swap_used_mb()
    sampler.start()
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    t0 = time.perf_counter()
    res = link(cohort, [key], data_dir=data_dir, run_dir=out_dir / f"run_{key}_{workers}", cache=None,
               executor="subprocess", workers=workers, output_level="episode", on_error="stop")
    wall = time.perf_counter() - t0
    sampler.halt.set(); sampler.join()
    res.exposures.to_parquet(out_dir / f"exp_{key}_{workers}.parquet", index=False)
    steps = []
    for s in res.steps:
        d = s.to_dict() if hasattr(s, "to_dict") else dict(s)
        steps.append({k: d.get(k) for k in ("name", "seconds", "maxrss_bytes", "rc", "cache_hit", "status")
                      if k in d})
    rec = {"key": key, "workers": workers, "started_local": started, "wall_s": round(wall, 2),
           "steps": steps, "peak_tree_rss_gb": round(sampler.peak_total / 1e9, 3),
           "peak_process_rss_gb": round(sampler.peak_max / 1e9, 3), "peak_processes": sampler.peak_n,
           "swap_used_mb_before": swap0, "swap_used_mb_after": swap_used_mb(),
           "failed": list(res.failed), "rows": int(len(res.exposures)),
           "spacescans": spacescans.__file__, "python": platform.python_version()}
    with open(out_dir / "results.jsonl", "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(json.dumps({k: rec[k] for k in ("key", "workers", "wall_s", "peak_tree_rss_gb",
                                          "peak_process_rss_gb", "failed")}), flush=True)
    return rec


def main():
    import pandas as pd
    data_dir, cohort, out_dir, n = sys.argv[1], sys.argv[2], Path(sys.argv[3]), int(sys.argv[4])
    keys = sys.argv[5:]
    out_dir.mkdir(parents=True, exist_ok=True)
    for key in keys:
        for workers in (None, n):
            run_one(key, workers, cohort, data_dir, out_dir)
        # Step 1 outputs must be bit-identical. Step 2 runs in DuckDB, whose multi-threaded
        # aggregation orders rows and float sums differently from run to run (also between
        # two one-process runs), so the exposures are compared with a tolerance.
        import numpy as np
        c3 = {}
        for f in sorted((out_dir / f"run_{key}_None" / "output").glob("c3_*.parquet")):
            c3[f.name] = pd.read_parquet(f).equals(pd.read_parquet(out_dir / f"run_{key}_{n}" / "output" / f.name))
        a = pd.read_parquet(out_dir / f"exp_{key}_None.parquet")
        b = pd.read_parquet(out_dir / f"exp_{key}_{n}.parquet")
        num = [c for c in a.columns if pd.api.types.is_numeric_dtype(a[c]) and c != "episode_id"]
        maxdiff = max((float(np.nanmax(np.abs(a[c].to_numpy(float) - b[c].to_numpy(float))))
                       for c in num), default=0.0)
        nan_mismatch = int(sum((a[c].isna() != b[c].isna()).sum() for c in num))
        rec = {"key": key, "compare": f"None vs {n}", "c3_identical": c3, "exposures_identical": a.equals(b),
               "exposures_max_abs_diff": maxdiff, "nan_mismatch": nan_mismatch,
               "same_episode_order": bool((a["episode_id"].values == b["episode_id"].values).all()),
               "rows": [len(a), len(b)]}
        with open(out_dir / "results.jsonl", "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)


if __name__ == "__main__":
    main()
