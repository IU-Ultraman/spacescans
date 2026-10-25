"""ACAG's reader returns the same frame, row for row, with any number of workers.

``engine.workers`` reaches the reader through the config acag_multi hands it; the files
are decoded by that many spawned processes and concatenated in file order.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("xarray")
pytest.importorskip("netCDF4")
import xarray as xr  # noqa: E402

from spacescans.plugins.readers.acag import ACAGExposureSource  # noqa: E402

pytestmark = [pytest.mark.nc, pytest.mark.extras]

N_LAT, N_LON = 3, 4
# six 2017 biweeks and one 2016 file, which a link of 2017 skips
BIWEEKS = [("2017001", "2017014"), ("2017015", "2017028"), ("2017029", "2017042"),
           ("2017043", "2017056"), ("2017057", "2017070"), ("2017071", "2017084"),
           ("2016351", "2016364")]


def _write(root: Path) -> None:
    root.mkdir()
    lat = np.array([10.0, 11.0, 12.0])            # ascending, as ACAG ships it
    lon = np.array([0.0, 1.0, 2.0, 3.0])
    for k, (start, end) in enumerate(BIWEEKS):
        data = np.arange(N_LAT * N_LON, dtype="float32").reshape(N_LAT, N_LON) + 100 * k
        if k % 3 == 0:
            data[1, 2] = np.nan                     # grid_id 7 is missing in files 0, 3 and 6
        xr.Dataset({"GWRPM25": (("lat", "lon"), data)}, coords={"lat": lat, "lon": lon}
                   ).to_netcdf(root / f"V5NA05.HybridPM25.xNorthAmerica.{start}-{end}.nc")
    (root / "notes.nc").write_bytes(b"")            # no dates in the name: never opened


def _reader(tmp_path: Path, **config) -> ACAGExposureSource:
    weights = tmp_path / "weights.parquet"
    if not weights.exists():
        pd.DataFrame({"geoid": [1, 1, 2, 3], "grid_id": [1, 7, 7, 12],
                      "weight": [0.5, 0.5, 1.0, 1.0]}).to_parquet(weights, index=False)
    return ACAGExposureSource(SimpleNamespace(source=SimpleNamespace(file=str(weights)),
                                              exposure=SimpleNamespace(file=str(tmp_path / "BiWeekly")),
                                              **config))


@pytest.mark.parametrize("workers", [2, 3])
def test_frames_do_not_depend_on_the_number_of_workers(tmp_path, workers):
    _write(tmp_path / "BiWeekly")
    one = _reader(tmp_path).load_exposure(years=[2017])        # a config without an engine block
    pd.testing.assert_frame_equal(
        one, _reader(tmp_path, engine=SimpleNamespace(workers=workers)).load_exposure(years=[2017]),
        check_exact=True)
    pd.testing.assert_frame_equal(
        one, _reader(tmp_path, engine=SimpleNamespace(workers=None)).load_exposure(years=[2017]),
        check_exact=True)
    assert list(one["start_date"].unique()) == ["2017-01-01", "2017-01-15", "2017-01-29",
                                                "2017-02-12", "2017-02-26", "2017-03-12"]
    assert len(one) == 6 * 3 - 2                  # 3 cells per file, grid_id 7 missing twice
    # north-up row-major ids on a south-first file: grid_id 12 is the SE corner of file k
    se = one[one["grid_id"] == 12]["value"].tolist()
    assert se == [float(100 * k + 3) for k in range(6)]
