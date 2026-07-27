"""Unit-level regression lock for the d076d4a tile-count fix.

The end-to-end integration test for nhd_proximity requires the 61 GB
NHDPlus HR GDB and a fully provisioned data_full tree, so a math
regression in the bbox->tile-count step would otherwise only be caught
by that heavyweight gate. These tests pin the invariant directly:
``_compute_tile_count`` must always return >= 1, including for
sub-grid-cell cohorts where the pre-fix ``np.arange + xs[:-1]``
collapsed to zero tiles.

See d076d4a "fix(linkage): nhd_proximity emits at least 1 tile for
<0.5deg cohorts" (Sprint 7) and the Sprint 8 I3 follow-up.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from spacescans.linkage.nhd_proximity_linkage import (
    _GRID_DEG,
    _compute_tile_count,
    _global_tile_index,
    _snap_to_grid,
)


@pytest.mark.parametrize(
    "extent_deg, expected",
    [
        # Sub-grid-cell cohorts (the d076d4a regression case): must
        # still emit one tile, not zero.
        (0.05, 1),
        (0.49, 1),
        # Exactly one grid cell wide.
        (0.50, 1),
        # Just over one grid cell — still fits in 2 tiles.
        (0.51, 2),
        # Two grid cells wide — boundary-aligned.
        (1.00, 2),
        # Demo-conus-scale: 1.5deg spans 3 tiles.
        (1.50, 3),
        # 1.01deg is the case called out in the d076d4a follow-up
        # report: ceil(1.01/0.5)=ceil(2.02)=3.
        (1.01, 3),
        # Zero-extent cohort (single point in lat or lon) still
        # collapses to exactly 1 tile rather than 0.
        (0.0, 1),
    ],
)
def test_compute_tile_count_invariant(extent_deg: float, expected: int) -> None:
    assert _compute_tile_count(extent_deg) == expected


def test_compute_tile_count_handles_non_finite_extent() -> None:
    # An empty cohort yields NaN total_bounds — we must still produce a
    # valid 1-tile grid rather than crashing or returning 0.
    assert _compute_tile_count(float("nan")) == 1
    assert _compute_tile_count(np.nan) == 1


def test_compute_tile_count_respects_custom_grid_deg() -> None:
    # Sanity check that the helper is parameterized by grid_deg and
    # not hard-coded against the module constant.
    assert _compute_tile_count(1.0, grid_deg=0.25) == 4
    assert _compute_tile_count(0.1, grid_deg=0.25) == 1


def test_compute_tile_count_module_grid_deg_unchanged() -> None:
    # Lock the NHD tile resolution against accidental retuning — the
    # cohort-independent feature cache is keyed by this constant.
    assert _GRID_DEG == 0.5


def test_pre_fix_arange_would_have_dropped_sub_grid_cohorts() -> None:
    # Documents WHY the helper exists. The pre-fix expression was:
    #     xs = np.arange(xmin, xmax + _GRID_DEG * 0.5, _GRID_DEG)
    #     tiles = [... for xx in xs[:-1] for yy in ys[:-1]]
    # For a bbox extent < _GRID_DEG/2 (e.g. a few patients within a
    # ~0.2deg-wide neighborhood) the arange call collapsed to a single
    # boundary value, so xs[:-1] was empty and no tiles were created.
    # Every patient ended up with NaN tile_id and the downstream
    # astype(int) crashed. The new helper guarantees >= 1.
    xmin, xmax = 0.0, 0.2
    pre_fix_xs = np.arange(xmin, xmax + _GRID_DEG * 0.5, _GRID_DEG)
    assert len(pre_fix_xs[:-1]) == 0  # pre-fix bug reproduction
    assert _compute_tile_count(xmax - xmin) >= 1  # post-fix invariant
    # Spot-check the numeric value: ceil(0.2/0.5) = 1.
    assert _compute_tile_count(xmax - xmin) == math.ceil(0.2 / 0.5)


# ---------------------------------------------------------------------------
# Feature-cache key must be GEOGRAPHIC, not a cohort-relative tile index.
#
# Regression lock for the cache-poisoning bug: the cache was keyed by
# ``tile_{tile_id}`` where tile_id indexes a per-cohort bbox grid, so the
# same filename meant different ground per cohort — a 500-patient cohort
# happily read a 100k-cohort's cached tiles (offset by ~0.6-1.1°), matching
# points to coastlines hundreds of km away (California > Idaho for
# distance-to-coast). The fix keys the cache by the tile's global 0.5°
# lattice index, derived by floor-snapping the tile's SW corner.
# ---------------------------------------------------------------------------
def test_global_tile_index_is_geographic_and_deterministic() -> None:
    # Same SW corner → same index, always.
    assert _global_tile_index(-124.0, 25.5) == _global_tile_index(-124.0, 25.5)
    # Snapped SW corners are multiples of 0.5 → exact integer lattice indices.
    assert _global_tile_index(-124.0, 25.5) == (-248, 51)
    # Adjacent tiles differ (no collision between neighbouring ground).
    assert _global_tile_index(-124.0, 25.5) != _global_tile_index(-123.5, 25.5)
    assert _global_tile_index(-124.0, 25.5) != _global_tile_index(-124.0, 26.0)


def test_point_maps_to_cohort_independent_global_tile() -> None:
    # The two real cohorts that collided: 500-task (xmin ≈ -124.011) and
    # 100k demo (xmin ≈ -124.634). A point both contain must map to the SAME
    # tile regardless of either cohort's bbox, because the containing tile's
    # SW corner is floor(point/grid)*grid — a function of the POINT only.
    lon, lat = -123.8, 26.1
    sw_lon, sw_lat = _snap_to_grid(lon), _snap_to_grid(lat)
    assert (sw_lon, sw_lat) == (-124.0, 26.0)          # lattice-aligned, cohort-free
    assert _global_tile_index(sw_lon, sw_lat) == (-248, 52)
    # Both cohort origins snap onto the same global lattice (multiples of 0.5),
    # so their tile boundaries coincide with the point's tile.
    for xmin in (-124.0113, -124.6344):
        assert (_snap_to_grid(xmin) / _GRID_DEG) % 1 == 0
