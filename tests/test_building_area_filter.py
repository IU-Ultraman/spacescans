"""Unit tests for area + filter helpers in scripts/_building_utils.py."""
import pytest
from shapely.geometry import Polygon

pytestmark = pytest.mark.geo


def test_polygon_area_m2_for_known_rectangle():
    """A 10m x 10m rectangle at lat=30 should compute to ~100 m^2 after equal-area reproject."""
    from _building_utils import polygon_area_m2

    # Construct a small WGS84 rectangle around -84.28, 30.44.
    # 10 m at lat=30 ≈ 0.0000898 deg lat, 0.0001038 deg lon.
    lon0, lat0 = -84.28, 30.44
    dlon = 0.0001038
    dlat = 0.0000898
    poly = Polygon([
        (lon0, lat0),
        (lon0 + dlon, lat0),
        (lon0 + dlon, lat0 + dlat),
        (lon0, lat0 + dlat),
    ])

    area = polygon_area_m2(poly, source_crs="EPSG:4326", area_crs="EPSG:6346")
    assert 90.0 <= area <= 110.0, f"expected ~100 m^2, got {area:.2f}"


def test_filter_by_area_inclusive_bounds():
    """Area filter must include exactly the polygons whose area falls within [min, max] inclusive."""
    from _building_utils import filter_polygons_by_area

    areas = [25.0, 50.0, 100.0, 1500.0, 2000.0, 2500.0]
    keep = filter_polygons_by_area(areas, area_min=50.0, area_max=2000.0)

    # 50 and 2000 are inclusive; 25 and 2500 are excluded.
    assert keep == [False, True, True, True, True, False]


def test_iter_geojsonl_gz_roundtrip(tmp_path):
    """iter_geojsonl_gz must yield each Feature dict from a gzipped GeoJSONL file."""
    import gzip, json
    from _building_utils import iter_geojsonl_gz

    path = tmp_path / "tiny.csv.gz"
    features = [
        {"type": "Feature",
         "properties": {"height": 3.5, "confidence": -1.0},
         "geometry": {"type": "Polygon",
                      "coordinates": [[[0,0],[0,1],[1,1],[1,0],[0,0]]]}},
        {"type": "Feature",
         "properties": {"height": -1.0, "confidence": -1.0},
         "geometry": {"type": "Polygon",
                      "coordinates": [[[2,2],[2,3],[3,3],[3,2],[2,2]]]}},
    ]
    with gzip.open(path, "wt") as f:
        for ft in features:
            f.write(json.dumps(ft) + "\n")

    result = list(iter_geojsonl_gz(path))
    assert len(result) == 2
    assert all("geometry" in r and "properties" in r for r in result)
    assert result[0]["properties"]["height"] == 3.5
