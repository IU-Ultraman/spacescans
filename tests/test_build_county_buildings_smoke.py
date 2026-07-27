"""End-to-end smoke + reproducibility tests for Stage 1 with a mock 10-building tile."""
import gzip
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

pytestmark = pytest.mark.geo


def _make_mock_tile(path: Path) -> None:
    """Create a 10-building mock tile as gzipped GeoJSONL.

    All buildings are sized 0.0002 x 0.0002 deg ≈ 19.2 x 22.2 m at lat=30 ≈ 426 m^2.
    All fall comfortably in the [50, 2000] m^2 area filter.

    Buildings are placed in central Leon County around (-84.281, 30.438), each offset
    by 0.0005 deg to avoid overlap.
    """
    base_lon, base_lat = -84.285, 30.435
    dlon = dlat = 0.0002  # constant size -> all ~426 m^2 -> all pass area filter
    features = []
    for i in range(10):
        lon0 = base_lon + (i * 0.0005)
        lat0 = base_lat + (i * 0.0005)
        features.append({
            "type": "Feature",
            "properties": {"height": -1.0 if i % 2 == 0 else 5.0 + i, "confidence": -1.0},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [lon0, lat0],
                    [lon0 + dlon, lat0],
                    [lon0 + dlon, lat0 + dlat],
                    [lon0, lat0 + dlat],
                    [lon0, lat0],
                ]]
            }
        })
    with gzip.open(path, "wt") as f:
        for ft in features:
            f.write(json.dumps(ft) + "\n")


def _make_empty_tile(path: Path) -> None:
    """Create an empty gzipped tile (0 features) — used for the non-primary Leon quadkeys
    so download_tiles finds them already cached and skips the file://noop fetch."""
    with gzip.open(path, "wt") as f:
        pass


def _write_mock_index(idx_path: Path, quadkeys: list[str]) -> None:
    """Write a mock index CSV with one row per provided quadkey, US-only."""
    lines = ["Location,QuadKey,Url,Size,UploadDate"]
    for qk in quadkeys:
        lines.append(f"UnitedStates,{qk},file://noop,1KB,2026-02-23")
    idx_path.write_text("\n".join(lines) + "\n")


def _run_stage1(*, index, boundary, tiles_dir, output, force=False) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        "scripts/build_county_buildings.py",
        "--county-fips", "12073",
        "--state-fips", "12",
        "--index", str(index),
        "--boundary", str(boundary),
        "--tiles-dir", str(tiles_dir),
        "--output", str(output),
        "--area-min", "50",
        "--area-max", "2000",
    ]
    if force:
        cmd.append("--force")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180)


def test_stage1_smoke(tmp_path):
    """Stage 1 runs end-to-end on a mock tile, output schema is correct, all 10 buildings survive filter."""
    tiles_dir = tmp_path / "tiles"
    tiles_dir.mkdir()

    boundary = Path("/Users/xai/Desktop/spacescans-project/data_full/County_FL/C3/tl_2010_us_county10/tl_2010_us_county10.shp")
    if not boundary.exists():
        pytest.skip("FL county shapefile not present locally")

    # Compute the ACTUAL Leon quadkeys via mercantile so the mock index covers them.
    import sys; sys.path.insert(0, "scripts")
    from _building_utils import quadkeys_for_bbox
    import geopandas as gpd
    fl = gpd.read_file(boundary).query("STATEFP10 == '12' and COUNTYFP10 == '073'")
    leon_qks = sorted(quadkeys_for_bbox(fl.geometry.iloc[0].bounds, zoom=9))
    # Materialise a 10-building mock tile for the first Leon quadkey, empty tiles for the rest.
    # All ten mock buildings live inside the first quadkey's footprint, so only that one
    # contributes data; the others must still exist on disk so download_tiles skips them.
    primary_qk = leon_qks[0]
    _make_mock_tile(tiles_dir / f"{primary_qk}.csv.gz")
    for qk in leon_qks[1:]:
        _make_empty_tile(tiles_dir / f"{qk}.csv.gz")
    # Mock index covers all Leon quadkeys so resolve_tile_urls doesn't error on the others.
    _write_mock_index(tmp_path / "mock_index.csv", leon_qks)

    output = tmp_path / "buildings_12073.parquet"
    result = _run_stage1(
        index=tmp_path / "mock_index.csv",
        boundary=boundary,
        tiles_dir=tiles_dir,
        output=output,
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}"

    df = pd.read_parquet(output)
    assert set(df.columns) == {"building_id", "lon", "lat", "area_m2", "height_m", "confidence", "quadkey"}
    assert df["building_id"].is_unique
    assert (df["area_m2"] >= 50).all() and (df["area_m2"] <= 2000).all()
    # All 10 mock buildings are sized ~426 m^2 -> all pass area filter; all are inside Leon -> all pass within().
    assert len(df) == 10, f"expected exactly 10 buildings after filter, got {len(df)}"


def test_stage1_reproducible(tmp_path):
    """Spec acceptance #1: same args twice must produce the same DataFrame (DataFrame-identical)."""
    tiles_dir = tmp_path / "tiles"
    tiles_dir.mkdir()

    boundary = Path("/Users/xai/Desktop/spacescans-project/data_full/County_FL/C3/tl_2010_us_county10/tl_2010_us_county10.shp")
    if not boundary.exists():
        pytest.skip("FL county shapefile not present locally")

    import sys; sys.path.insert(0, "scripts")
    from _building_utils import quadkeys_for_bbox
    import geopandas as gpd
    fl = gpd.read_file(boundary).query("STATEFP10 == '12' and COUNTYFP10 == '073'")
    leon_qks = sorted(quadkeys_for_bbox(fl.geometry.iloc[0].bounds, zoom=9))
    _make_mock_tile(tiles_dir / f"{leon_qks[0]}.csv.gz")
    for qk in leon_qks[1:]:
        _make_empty_tile(tiles_dir / f"{qk}.csv.gz")
    _write_mock_index(tmp_path / "mock_index.csv", leon_qks)

    out1 = tmp_path / "p1.parquet"
    out2 = tmp_path / "p2.parquet"
    # Run twice with --force so we don't skip the second run via idempotency.
    for out in (out1, out2):
        r = _run_stage1(
            index=tmp_path / "mock_index.csv",
            boundary=boundary,
            tiles_dir=tiles_dir,
            output=out,
            force=True,
        )
        assert r.returncode == 0, r.stderr

    pd.testing.assert_frame_equal(pd.read_parquet(out1), pd.read_parquet(out2))
