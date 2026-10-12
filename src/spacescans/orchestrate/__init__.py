"""Orchestration shared by the web app and the R bridge.

Phase A provides the merge step and the pre-run coverage estimate. Step
rendering, the C3 weight cache, execution, ``run_source`` and ``link``
arrive in phase B. Like :mod:`spacescans.cohort`, everything exported here
imports with the base install (pandas only).
"""
from __future__ import annotations

from spacescans.orchestrate.coverage import (
    CONUS_BBOX,
    compute_coverage,
    read_coverage_csv,
)
from spacescans.orchestrate.merge import (
    KEYS,
    LOW_MATCH_EVENT,
    LOW_MATCH_PCT,
    MatchStats,
    episode_keys,
    fan_in,
    fan_in_from_dir,
    match_stats,
    merge_partial,
    partial_csv_name,
    partial_from_dir,
    select_output,
)

__all__ = [
    "CONUS_BBOX",
    "KEYS",
    "LOW_MATCH_EVENT",
    "LOW_MATCH_PCT",
    "MatchStats",
    "compute_coverage",
    "episode_keys",
    "fan_in",
    "fan_in_from_dir",
    "match_stats",
    "merge_partial",
    "partial_csv_name",
    "partial_from_dir",
    "read_coverage_csv",
    "select_output",
]
