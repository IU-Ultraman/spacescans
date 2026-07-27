"""Unit tests for quadkey helpers in scripts/_building_utils.py."""
import pytest

pytestmark = pytest.mark.geo  # mercantile lives in [geo] extra


def test_quadkeys_for_leon_county_bbox():
    """A bbox around Leon County, FL must resolve to exactly the 2 expected zoom-9 tiles."""
    from _building_utils import quadkeys_for_bbox

    # Leon County FL bbox (approximate, from TIGER county shapefile)
    leon_bbox = (-84.376, 30.279, -84.069, 30.692)
    qks = quadkeys_for_bbox(leon_bbox, zoom=9)

    # Leon spans exactly 2 zoom-9 tiles per the design doc investigation.
    assert isinstance(qks, set)
    assert len(qks) == 2, f"expected 2 zoom-9 tiles, got {len(qks)}: {qks}"
    # All US quadkeys are zero-padded to length 9.
    assert all(len(qk) == 9 for qk in qks), f"all quadkeys must be 9 chars: {qks}"
    # All start with the FL prefix.
    assert all(qk.startswith('0320') for qk in qks), f"FL quadkeys start with 0320: {qks}"


def test_quadkeys_for_single_point_returns_one_tile():
    """A degenerate (point) bbox returns the single covering tile."""
    from _building_utils import quadkeys_for_bbox

    # Single point in central Leon (Tallahassee state capitol approx.)
    point_bbox = (-84.281, 30.438, -84.281, 30.438)
    qks = quadkeys_for_bbox(point_bbox, zoom=9)

    assert len(qks) == 1
    assert all(len(qk) == 9 for qk in qks)
