"""Stage 2 unit tests — uses a synthetic 1000-row buildings fixture, requires the FL BG shapefile."""
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytestmark = pytest.mark.geo


def _make_fixture_buildings(out_path: Path, n: int = 1000) -> None:
    """Create a 1000-row fixture buildings parquet with synthetic coords inside Leon County FL."""
    rng = np.random.default_rng(0)
    df = pd.DataFrame({
        "building_id": range(n),
        "lon": rng.uniform(-84.30, -84.20, size=n),
        "lat": rng.uniform(30.40, 30.50, size=n),
        "area_m2": rng.uniform(50, 2000, size=n),
        "height_m": rng.uniform(3, 30, size=n),
        "confidence": np.full(n, -1.0),
        "quadkey": ["032023220"] * n,
    })
    df.to_parquet(out_path, index=False)


def _run_stage2(*, buildings, bg_shp, output, n=100, seed=42,
                expected_state="12", expected_county="12073",
                timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run([
        sys.executable,
        "scripts/sample_patients_from_buildings.py",
        "--buildings", str(buildings),
        "--n", str(n),
        "--seed", str(seed),
        "--bg-shapefile", str(bg_shp),
        "--expected-state-fips", expected_state,
        "--expected-county-fips", expected_county,
        "--output", str(output),
    ], capture_output=True, text=True, timeout=timeout)


def test_stage2_smoke(tmp_path):
    fixture = tmp_path / "buildings_fixture.parquet"
    _make_fixture_buildings(fixture, n=1000)

    bg_shp = Path("/Users/xai/Desktop/spacescans-all/spacescans-web/pipeline-data/BG/C3/tiger2010_bg10_states/tl_2010_12_bg10/tl_2010_12_bg10.shp")
    if not bg_shp.exists():
        pytest.skip("FL BG shapefile not present locally")

    output = tmp_path / "patients.parquet"
    result = _run_stage2(buildings=fixture, bg_shp=bg_shp, output=output, n=100)
    assert result.returncode == 0, f"stderr:\n{result.stderr}"

    df = pd.read_parquet(output)
    assert len(df) == 100
    assert list(df.columns) == [
        "pid", "startDate", "endDate", "longitude", "latitude",
        "state_fips", "county_fips", "tract_geoid", "bg_geoid",
    ]
    assert df["pid"].is_unique
    assert df["pid"].iloc[0] == "PID0000001"
    assert df["pid"].iloc[-1] == "PID0000100"
    # All fixture points are inside Leon (FL state 12, county 12073).
    # The validate block raises if anyone landed outside; we assert again here for clarity.
    assert (df["state_fips"] == "12").all()
    assert (df["county_fips"] == "12073").all()
    # Durations 80-100 days inclusive
    dur = (pd.to_datetime(df["endDate"]) - pd.to_datetime(df["startDate"])).dt.days
    assert (dur >= 80).all() and (dur <= 100).all()


def test_stage2_reproducibility(tmp_path):
    """Spec acceptance #2 (relaxed): same seed -> DataFrame-identical output.

    NOTE: 'byte-identical' parquet is not robust across pyarrow versions (compression metadata).
    The semantic guarantee — same input → same patient cohort — is captured by pandas DataFrame
    equality on the loaded parquet.
    """
    fixture = tmp_path / "buildings_fixture.parquet"
    _make_fixture_buildings(fixture, n=1000)

    bg_shp = Path("/Users/xai/Desktop/spacescans-all/spacescans-web/pipeline-data/BG/C3/tiger2010_bg10_states/tl_2010_12_bg10/tl_2010_12_bg10.shp")
    if not bg_shp.exists():
        pytest.skip("FL BG shapefile not present locally")

    out1 = tmp_path / "p1.parquet"
    out2 = tmp_path / "p2.parquet"
    for out in (out1, out2):
        r = _run_stage2(buildings=fixture, bg_shp=bg_shp, output=out, n=100)
        assert r.returncode == 0, r.stderr

    pd.testing.assert_frame_equal(pd.read_parquet(out1), pd.read_parquet(out2))


def test_stage2_rejects_wrong_county(tmp_path):
    """Major #6 mitigation: --expected-county-fips must match the data; otherwise abort.

    All fixture points are inside Leon (12073). Passing --expected-county-fips=12086 (Miami-Dade)
    should make validate fail and the script exit non-zero with a descriptive error.
    """
    fixture = tmp_path / "buildings_fixture.parquet"
    _make_fixture_buildings(fixture, n=1000)

    bg_shp = Path("/Users/xai/Desktop/spacescans-all/spacescans-web/pipeline-data/BG/C3/tiger2010_bg10_states/tl_2010_12_bg10/tl_2010_12_bg10.shp")
    if not bg_shp.exists():
        pytest.skip("FL BG shapefile not present locally")

    out = tmp_path / "patients.parquet"
    r = _run_stage2(buildings=fixture, bg_shp=bg_shp, output=out, n=100, expected_county="12086")
    assert r.returncode != 0, "should have failed with wrong --expected-county-fips"
    assert "not in county 12086" in (r.stderr + r.stdout), f"stderr:\n{r.stderr}\nstdout:\n{r.stdout}"
