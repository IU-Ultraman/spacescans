"""A stand-in for ``spacescans run``, used only by the orchestrate.execute tests.

Invoked as ``python fake_pipeline_cli.py run --data-dir <dir> <config>``, the same
argv tail the executors build. The config is a JSON file (JSON is valid YAML)
saying what to do, in this order:

    {"report": true,            # print "pid <n>", "pgid <n>", "cwd <dir>", "data-dir <dir>",
                                #   "env SPACESCANS_DATA_DIR=<value>", "env <NAME>=<value>"
     "report_env": ["NAME"],    # extra variables for the report
     "ignore_term": true,       # ignore SIGTERM (so only SIGKILL stops it)
     "grandchild": true,        # start a grandchild in the same process group (it ignores
                                #   SIGTERM too when ignore_term is set) and print its pid
     "alloc_mb": 200,           # allocate and touch this many MiB
     "lines": ["text", 0.2, "more", ...],   # print strings (flushed); numbers are sleeps
     "stderr": "text",          # one line on stderr
     "raw": "a\\rb\\r\\n\\nc",    # written as is to stdout, no newline added
     "sleep": 60,               # then sleep
     "rc": 3}                   # exit code (default 0)

Stdlib only.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

_GRANDCHILD = (
    "import signal, sys, time\n"
    "if sys.argv[1] == '1':\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "time.sleep(600)\n"
)


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[0] != "run" or argv[1] != "--data-dir":
        print(f"usage: fake_pipeline_cli.py run --data-dir DIR CONFIG (got {argv})",
              file=sys.stderr)
        return 2
    data_dir, config = argv[2], argv[3]
    with open(config, encoding="utf-8") as f:
        spec = json.load(f)

    if spec.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if spec.get("report"):
        print(f"pid {os.getpid()}")
        print(f"pgid {os.getpgid(0)}")
        print(f"cwd {os.getcwd()}")
        print(f"data-dir {data_dir}")
        for name in ["SPACESCANS_DATA_DIR", *spec.get("report_env", [])]:
            print(f"env {name}={os.environ.get(name, '<unset>')}")
        sys.stdout.flush()
    if spec.get("grandchild"):
        child = subprocess.Popen([sys.executable, "-c", _GRANDCHILD,
                                  "1" if spec.get("ignore_term") else "0"])
        print(f"grandchild {child.pid}", flush=True)
    if spec.get("alloc_mb"):
        block = bytearray(int(spec["alloc_mb"]) * 1024 * 1024)
        for i in range(0, len(block), 4096):
            block[i] = 1
        print(f"allocated {spec['alloc_mb']} MiB", flush=True)
    for item in spec.get("lines", []):
        if isinstance(item, (int, float)):
            time.sleep(item)
        else:
            print(item, flush=True)
    if spec.get("stderr"):
        print(spec["stderr"], file=sys.stderr, flush=True)
    if spec.get("raw"):
        sys.stdout.write(spec["raw"])
        sys.stdout.flush()
    if spec.get("sleep"):
        time.sleep(float(spec["sleep"]))
    return int(spec.get("rc", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
