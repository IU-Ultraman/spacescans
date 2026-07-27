"""Sprint 2: _adapt_demo_conus should prefer an upstream-supplied
episode_id column over its synthetic range(len(df)) fallback."""
import pandas as pd

from spacescans.linkage.helpers import _adapt_demo_conus


def test_adapter_uses_episode_id_when_present():
    """When the input df has an episode_id column, the adapter copies
    it (cast to int) into the `geoid` output column."""
    df = pd.DataFrame({
        "pid": ["P1", "P1", "P2"],
        "startDate": ["2014-01-01", "2018-01-01", "2017-01-01"],
        "endDate":   ["2017-12-31", "2020-12-31", "2018-06-30"],
        "longitude": [-87.6, -84.3, -95.0],
        "latitude":  [41.9, 30.4, 30.0],
        "episode_id": [0, 1, 2],
    })
    out = _adapt_demo_conus(df)
    # geoid should mirror episode_id
    assert out["geoid"].tolist() == [0, 1, 2]
    # Schema sanity
    assert list(out.columns) == ["PATID", "start", "end", "long", "lat", "geoid"]


def test_adapter_falls_back_to_range_without_episode_id():
    """When episode_id is absent, fallback to synthetic range(len(df))."""
    df = pd.DataFrame({
        "pid": ["P1", "P2"],
        "startDate": ["2017-01-01", "2017-01-01"],
        "endDate":   ["2017-12-31", "2017-12-31"],
        "longitude": [-87.6, -95.0],
        "latitude":  [41.9, 30.0],
    })
    out = _adapt_demo_conus(df)
    assert out["geoid"].tolist() == [0, 1]


def test_adapter_handles_non_consecutive_episode_id():
    """Non-zero-based or non-contiguous episode_ids must be honoured."""
    df = pd.DataFrame({
        "pid": ["P1", "P2"],
        "startDate": ["2017-01-01", "2017-01-01"],
        "endDate":   ["2017-12-31", "2017-12-31"],
        "longitude": [-87.6, -95.0],
        "latitude":  [41.9, 30.0],
        "episode_id": [10, 42],
    })
    out = _adapt_demo_conus(df)
    assert out["geoid"].tolist() == [10, 42]
