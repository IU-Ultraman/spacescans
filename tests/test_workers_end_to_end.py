"""A real step with ``engine.workers: 2`` writes the same weights as with one process.

``spacescans run`` in its own process group (the SubprocessExecutor every link uses),
spawned workers importing the overlap code, progress lines reaching the caller: the
whole path, on two clusters of synthetic block-group-like squares 60 km apart, so the
30 km tiles give the workers more than one chunk.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

pytest.importorskip("geopandas")
pytest.importorskip("exactextract")

import geopandas as gpd  # noqa: E402
from shapely.geometry import box  # noqa: E402

pytestmark = [pytest.mark.geo, pytest.mark.extras, pytest.mark.slow]


def _inputs(tmp: Path) -> tuple[Path, Path]:
    squares, keys = [], []
    for cx in (-86.6, -85.9):                      # two clusters ~60 km apart (Indiana)
        for i in range(8):
            for j in range(4):
                x0, y0 = cx + i * 0.02, 39.70 + j * 0.02
                squares.append(box(x0, y0, x0 + 0.02, y0 + 0.02))
                keys.append(f"18{len(keys):010d}")
    order = np.random.default_rng(3).permutation(len(keys))
    bounds = gpd.GeoDataFrame({"GEOID10": [keys[k] for k in order]},
                              geometry=[squares[k] for k in order], crs="EPSG:4326")
    shp = tmp / "squares.gpkg"
    bounds.to_file(shp, driver="GPKG")
    rng = np.random.default_rng(5)
    n = 40
    cx = np.where(np.arange(n) % 2 == 0, -86.6, -85.9)
    patients = pd.DataFrame({"PATID": [f"P{i}" for i in range(n)], "geoid": np.arange(n),
                             "long": cx + rng.uniform(0.01, 0.15, n),
                             "lat": 39.70 + rng.uniform(0.01, 0.07, n)})
    pq = tmp / "patients.parquet"
    patients.to_parquet(pq, index=False)
    return shp, pq


def _config(tmp: Path, shp: Path, pq: Path, workers: int | None) -> Path:
    cfg = {"name": "c3_squares", "linkage_pattern": "boundary_overlap_fast",
           "geometry_type": "polygon",
           "source": {"file": str(shp), "join_col": "GEOID10"},
           "buffer": {"patient_file": str(pq), "buffer_m": 270, "raster_res_m": 25},
           "engine": {"backend": "duckdb", **({"workers": workers} if workers else {})},
           "output": {"path": str(tmp / f"out_{workers}.parquet"), "format": "parquet"}}
    path = tmp / f"c3_{workers}.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return path


def test_a_step_with_two_workers_writes_the_same_weights(tmp_path):
    from spacescans.orchestrate.events import parse_progress
    from spacescans.orchestrate.execute import SubprocessExecutor

    shp, pq = _inputs(tmp_path)
    out = {}
    for workers in (None, 2):
        lines: list[str] = []
        run = SubprocessExecutor(timeout_s=300).run(
            _config(tmp_path, shp, pq, workers), data_dir=tmp_path, cwd=tmp_path,
            on_line=lines.append)
        assert run.rc == 0, run.tail
        out[workers] = pd.read_parquet(tmp_path / f"out_{workers}.parquet")
        fracs = [f for f in map(parse_progress, lines) if f is not None]
        assert fracs and fracs[-1] == 1.0
        chunks = [int(m.group(1)) for m in map(re.compile(r"in (\d+) chunks on 2 worker").search, lines)
                  if m]
        assert (chunks[0] >= 2) if workers == 2 else not chunks, chunks
    pd.testing.assert_frame_equal(out[None], out[2], check_exact=True)
    assert len(out[None]) >= 40 and out[None]["geoid"].nunique() == 40
