#!/usr/bin/env python3
"""Fingerprint the inputs the goldens depend on that the frozen image does not contain.

The backend container reads its templates from a bind mount of the web checkout
(/configs <- spacescans-web/configs) and its data from another (/project <-
spacescans-web/pipeline-data). Neither is inside the image, so this records both:

* templates.json: sha256 of every file under configs/.
* data_inventory.json: for each top-level dataset directory, the file count, total bytes,
  newest mtime and a sha256 over the sorted "relpath<TAB>size<TAB>mtime_ns" listing (cheap,
  catches added, removed, resized or touched files); plus full sha256 of the small value
  tables and templates the catalog reads directly.

Usage (host, stdlib only):
    python3 tests/golden/tools/inventory.py --web /Users/xai/Desktop/spacescans-all/spacescans-web \
        --out tests/golden/env
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

FULL_HASH = (
    "NDI/C4/ndi_bg_acs5_2008_2012_to_2020_2024_nationwide_summaryfile_xgboost.rds",
    "Walkability/C4/epawalkind_nationwide_2016_2021.Rda",
    "FARA/C4/fara_nationwide_2010_2019_interpolated.Rda",
    "FARA/C4/varnameCountRemoved.csv",
    "Community_Organization_Density/C4/cbp_nationwide_0519.Rda",
    "Community_Organization_Density/C4/zbp_nationwide_0521.Rda",
    "TEMIS/C3/temis_template.tif",
    "TEMIS/C4/converted/manifest.json",
    "ACAG/C3/acag_template.tif",
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def listing(root: Path) -> dict:
    lines, n, total, newest = [], 0, 0, 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            st = p.stat()
            rel = p.relative_to(root).as_posix()
            lines.append(f"{rel}\t{st.st_size}\t{st.st_mtime_ns}")
            n += 1
            total += st.st_size
            newest = max(newest, st.st_mtime_ns)
    lines.sort()
    return {"files": n, "bytes": total,
            "newest_mtime_utc": datetime.fromtimestamp(newest / 1e9, timezone.utc).isoformat()
            if newest else None,
            "listing_sha256": hashlib.sha256("\n".join(lines).encode()).hexdigest()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--web", required=True, help="spacescans-web checkout (configs/, pipeline-data/)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    web, out = Path(args.web), Path(args.out)
    data = web / "pipeline-data"

    templates = {p.relative_to(web / "configs").as_posix(): sha256(p)
                 for p in sorted((web / "configs").rglob("*")) if p.is_file()}
    (out / "templates.json").write_text(json.dumps(
        {"root": "spacescans-web/configs (mounted read-only at /configs)", "files": templates},
        indent=2) + "\n")

    inv = {"root": "spacescans-web/pipeline-data (mounted at /project)",
           "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "directories": {d.name: listing(d) for d in sorted(data.iterdir()) if d.is_dir()},
           "full_sha256": {}}
    for rel in FULL_HASH:
        p = data / rel
        inv["full_sha256"][rel] = ({"bytes": p.stat().st_size, "sha256": sha256(p)}
                                   if p.is_file() else None)
    (out / "data_inventory.json").write_text(json.dumps(inv, indent=2) + "\n")
    print(json.dumps({"templates": len(templates), "directories": len(inv["directories"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
